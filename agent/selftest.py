"""Offline proof that the engine works: no internet, no captcha, no login.

A local page is served whose button id changes between two variants, so
the self-healing resolver must fall back and relearn.
"""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

from playwright.sync_api import Error as PlaywrightError

from . import guard, locator
from .browser import Browser
from .config import get_logger
from .errors import BlockedByGuard
from .netcap import NetworkCapture
from .notify import Notifier
from .store import Store

log = get_logger("selftest")

_PAGE = """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<title>agent selftest</title></head><body>
<input id="q" name="query" placeholder="جست‌وجو">
<button id="{btn_id}" onclick="send()">ارسال</button>
<div id="result">-</div>
<script>
function send() {{
  const v = document.getElementById('q').value;
  fetch('/api/ping?q=' + encodeURIComponent(v))
    .then(r => r.json())
    .then(j => {{ document.getElementById('result').textContent = 'ok:' + j.echo; }});
}}
</script></body></html>"""

_CAPTCHA_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<title>Access Verification</title></head><body>
<h1>Access Verification</h1>
<p>Please complete the operation to verify that you are a real person</p>
<p>Drag to complete the puzzle</p></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug(fmt, args)

    def _send(self, body: bytes, mime: str) -> None:
        self.send_response(200)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path == "/api/ping":
            echo = parse_qs(parts.query).get("q", [""])[0]
            self._send(('{"echo": "%s"}' % echo.replace('"', "")).encode("utf-8"), "application/json")
        elif parts.path == "/captcha":
            self._send(_CAPTCHA_PAGE.encode("utf-8"), "text/html")
        else:
            variant = parse_qs(parts.query).get("v", ["1"])[0]
            btn_id = "send-btn" if variant == "1" else "btn-9f3ac71e"
            self._send(_PAGE.format(btn_id=btn_id).encode("utf-8"), "text/html")


def run() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    store = Store()
    notifier = Notifier(interactive=False)
    capture = NetworkCapture(store, notifier)
    print("تست آفلاین موتور مرورگر")
    try:
        with Browser(store, notifier) as browser:
            page = browser.new_page(base + "/?v=1")
            capture.attach(page)
            check("صفحهٔ آزمایشی باز شد", "selftest" in page.title())

            guard.ensure_clear(page, notifier, wait=False)
            check("صفحهٔ سالم به‌اشتباه کپچا تشخیص داده نشد", True)

            target_button = {"text": "ارسال"}
            locator.fill(page, {"placeholder": "جست‌وجو"}, "salam", store)
            locator.click(page, target_button, store)
            page.wait_for_function(
                "() => document.getElementById('result').textContent.startsWith('ok:')",
                timeout=8000,
            )
            check("کلیک و تایپ کار کرد", page.inner_text("#result") == "ok:salam")

            origin = locator.origin_of(page)
            key = locator.target_key(target_button)
            learned_first = store.get_selectors(origin, key)
            check("selector یاد گرفته شد", bool(learned_first), ", ".join(learned_first))

            page.goto(base + "/?v=2", wait_until="domcontentloaded")
            locator.fill(page, {"placeholder": "جست‌وجو"}, "dobare", store)
            locator.click(page, target_button, store)
            page.wait_for_function(
                "() => document.getElementById('result').textContent.startsWith('ok:')",
                timeout=8000,
            )
            check("با تغییر id دکمه، خودترمیمی کار کرد",
                  page.inner_text("#result") == "ok:dobare")
            learned_second = store.get_selectors(origin, key)
            check("حافظهٔ selector به‌روز شد", learned_second != learned_first,
                  ", ".join(learned_second))

            stale = "button#does-not-exist"
            store.save_selectors(origin, key, [stale] + learned_second)
            for _ in range(3):
                store.note_candidate(origin, key, stale, hit=False)
            pruned = store.prune_selectors(origin, key)
            check("حافظهٔ کهنه بعد از ۳ شکست هرس شد", stale not in pruned)

            page.wait_for_timeout(600)
            ping = [e for e in capture.entries if "/api/ping" in e["url"]]
            check("شنود شبکه درخواست را ثبت کرد", bool(ping) and ping[-1]["status"] == 200,
                  f"{len(ping)} درخواست")

            page.goto(base + "/captcha", wait_until="domcontentloaded")
            detected = guard.detect(page)
            check("کپچای نمونه تشخیص داده شد",
                  detected is not None and detected["kind"] == "captcha",
                  str(detected))
            raised = False
            try:
                guard.ensure_clear(page, notifier, wait=False)
            except BlockedByGuard:
                raised = True
            check("نگهبان کار را متوقف می‌کند", raised)
            shot = browser.screenshot("selftest")
            check("اسکرین‌شات روی D: ذخیره شد", shot is not None, str(shot))
            capture.detach()
    except (PlaywrightError, OSError, RuntimeError) as exc:
        check("اجرای بدون خطا", False, str(exc))
    finally:
        server.shutdown()
        server.server_close()
        store.close()

    failed = [name for name, ok, _ in results if not ok]
    print("\nنتیجه:", "همه تست‌ها پاس شد" if not failed else f"{len(failed)} تست شکست خورد")
    return 0 if not failed else 1

if __name__ == "__main__":
    raise SystemExit(run())
