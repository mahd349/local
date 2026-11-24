"""Offline test for Learn Mode and Takeover Mode."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit

from .browser import Browser
from .config import get_logger
from .errors import AgentError
from .learn import Recorder, replay, save_macro
from .notify import Notifier
from .store import Store
from .takeover import execute

log = get_logger("learntest")

_PAGE = """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<title>learn test</title></head><body>
<input id="q" name="query" placeholder="جست‌وجو">
<input id="p" name="pass" type="password">
<button id="send" onclick="send()">ارسال</button>
<div id="result">-</div>
<script>
function send() {
  const q = document.getElementById('q').value;
  const p = document.getElementById('p').value;
  document.getElementById('result').textContent = 'ok:' + q + ':' + p.length;
}
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug(fmt, args)

    def do_GET(self):  # noqa: N802
        body = _PAGE.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/html; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class _Quiet(Notifier):
    def __init__(self):
        super().__init__(interactive=False)
        self.events: list[tuple[str, dict]] = []

    def emit(self, kind: str, payload: dict | None = None) -> None:
        self.events.append((kind, payload or {}))

    def approval(self, kind: str, summary: str, payload: dict | None = None,
                 timeout_sec: float | None = None) -> bool:
        return True


def run() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    store = Store()
    notifier = _Quiet()
    print("تست آفلاین Learn و Takeover")
    try:
        with Browser(store, notifier) as browser:
            page = browser.new_page()
            recorder = Recorder(store, notifier)
            recorder.attach(page)
            page.goto(base, wait_until="domcontentloaded")
            page.fill("#q", "salam")
            page.fill("#p", "secret1")
            page.click("#send")
            page.wait_for_timeout(300)
            recorder.drain(browser.pages())
            events = recorder.events

            kinds = [e.get("t") for e in events]
            check("ناوبری، تایپ و کلیک ضبط شدند",
                  "goto" in kinds and "fill" in kinds and "click" in kinds,
                  str(kinds))
            secret_events = [e for e in events if e.get("secret")]
            check("مقدار پسورد هرگز ضبط نشد",
                  bool(secret_events) and all(e.get("value") is None for e in secret_events))

            macro = save_macro(store, "learn-test-macro", events)
            check("ماکرو ذخیره و پارامترها جدا شدند", len(macro["params"]) >= 2,
                  ", ".join(p["name"] for p in macro["params"]))
            check("پارامتر محرمانه نمونه ندارد",
                  all(p["sample"] is None for p in macro["params"] if p["secret"]))

            values = {p["name"]: ("secret22" if p["secret"] else "dobare")
                      for p in macro["params"]}
            page.goto("about:blank")
            report = replay(store, notifier, browser, "learn-test-macro", values)
            active = browser.active_page()
            check("بازپخش ماکرو با مقدار جدید کار کرد",
                  report["status"] == "ok" and active.inner_text("#result") == "ok:dobare:8",
                  f"{report['status']} | {active.inner_text('#result')} | {report['error'][:120]}")

            check("takeover: go", execute(browser, store, notifier, f"go {base}") == base)
            execute(browser, store, notifier, "fill ph=جست‌وجو = salam2")
            execute(browser, store, notifier, "click text=ارسال")
            out = execute(browser, store, notifier, "read css=#result")
            check("takeover: fill/click/read", out == "ok:salam2:0", out)
            check("takeover: title", "learn test" in execute(browser, store, notifier, "title"))
            shot = execute(browser, store, notifier, "shot learntest")
            check("takeover: shot روی D:", shot.startswith("D:\\"), shot)
            raised = False
            try:
                execute(browser, store, notifier, "nosuchcommand")
            except AgentError:
                raised = True
            check("takeover: دستور ناشناس رد شد", raised)
    except (OSError, RuntimeError, AgentError) as exc:
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
