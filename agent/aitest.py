"""Offline test of the chat worker against a fake streaming chat page."""

from __future__ import annotations

import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .ai import ChatSession
from .browser import Browser
from .codeblocks import to_files
from .config import CODE_ROOT, get_logger
from .errors import AgentError
from .notify import Notifier
from .store import Store

log = get_logger("aitest")

_PAGE = """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<title>fake chat</title></head><body>
<div id="log"></div>
<textarea id="prompt"></textarea>
<button id="send" type="submit" onclick="send()">Send</button>
<button id="stop" style="display:none">Stop</button>
<script>
const LOOP = new URLSearchParams(location.search).has('loop');
const P1 = [['t','بخش اول:'],['c','demo/a.py',"print('a')\\n"],['t','برای ادامه بگو «ادامه».']];
const P2 = [['t','بخش دوم:'],['c','demo/b.py',"print('b')\\n"],['t','اتمام کد']];
function send() {
  const box = document.getElementById('prompt');
  const text = box.value; box.value = '';
  const log = document.getElementById('log');
  const u = document.createElement('div'); u.className = 'user'; u.textContent = text;
  log.appendChild(u);
  const a = document.createElement('div'); a.className = 'assistant'; log.appendChild(a);
  document.getElementById('stop').style.display = 'block';
  const items = LOOP ? P1 : (text.trim() === 'ادامه' ? P2 : P1);
  let i = 0;
  const timer = setInterval(() => {
    if (i >= items.length) {
      clearInterval(timer);
      document.getElementById('stop').style.display = 'none';
      return;
    }
    const [kind, one, two] = items[i++];
    if (kind === 't') {
      const p = document.createElement('p'); p.textContent = one; a.appendChild(p);
    } else {
      const h = document.createElement('p'); h.textContent = '### FILE: ' + one; a.appendChild(h);
      const pre = document.createElement('pre');
      const code = document.createElement('code'); code.textContent = two;
      pre.appendChild(code); a.appendChild(pre);
    }
  }, 120);
}
</script></body></html>"""

_SELECTORS = {
    "input": [{"css": "#prompt"}],
    "send": [{"css": "#send"}],
    "stop": [{"css": "#stop"}],
    "assistant": [{"css": ".assistant"}],
}


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
        self.events = []

    def emit(self, kind, payload=None):
        self.events.append((kind, payload or {}))

    def approval(self, kind, summary, payload=None, timeout_sec=None):
        return True


def run() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    store, notifier = Store(), _Quiet()
    out_dir = CODE_ROOT / "agent_ai_test"
    print("تست آفلاین کارگر چت")
    try:
        rejected = False
        try:
            to_files([{"name": "C:/Windows/x.txt", "code": "x"}], out_dir)
        except AgentError:
            rejected = True
        check("مسیر بیرون سندباکس رد شد", rejected)

        with Browser(store, notifier) as browser:
            session = ChatSession(browser, store, notifier, selectors=_SELECTORS)
            session.open(base)
            report = session.run_task("یک پروژهٔ نمونه بساز", out_dir=str(out_dir))
            check("دو بخش پاسخ گرفته شد", len(report["parts"]) == 2, str(report["parts"]))
            users = session.page.locator(".user")
            second = users.nth(1).inner_text() if users.count() > 1 else ""
            check("ادامه به‌درستی ارسال شد", "ادامه" in second, second[:40])
            check("پایان با «اتمام کد» تشخیص داده شد", report["done"])
            names = sorted(b["name"] for b in report["blocks"])
            check("نام فایل‌ها از قرارداد FILE استخراج شد",
                  names == ["demo/a.py", "demo/b.py"], str(names))
            check("فایل‌ها در سندباکس نوشته شدند",
                  (out_dir / "demo" / "a.py").exists() and (out_dir / "demo" / "b.py").exists())

            looping = ChatSession(browser, store, notifier, selectors=_SELECTORS)
            looping.open(base + "?loop=1")
            capped = looping.run_task("حلقهٔ بی‌پایان", out_dir=str(out_dir), max_parts=2)
            check("سقف بخش‌ها جلوی حلقهٔ بی‌پایان را گرفت",
                  len(capped["parts"]) == 2 and not capped["done"])
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