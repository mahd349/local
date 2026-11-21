"""Offline test of the in-page suggestion panel: no real chat site is used."""
from __future__ import annotations

import shutil
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from playwright.sync_api import Error as PlaywrightError

from agent.config import CODE_ROOT, get_logger
from agent.errors import AgentError
from agent.notify import Notifier
from agent.store import Store

from . import corpus, watch

log = get_logger("farsi.watchtest")

_PAGE = """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<title>fake chat</title></head><body>
<div id="log"></div>
<button id="say" onclick="say()">حرف بزن</button>
<script>
let n = 0;
function say() {
  n += 1;
  const d = document.createElement('div');
  d.className = 'assistant';
  d.textContent = n === 1 ? 'برو پایتون یاد بگیر' : ('برو پایتون یاد بگیر ' + n);
  document.getElementById('log').appendChild(d);
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
    """Test double: silent events, fixed approvals, no emergency-stop coupling."""

    def __init__(self):
        super().__init__(interactive=False)

    def emit(self, kind, payload=None):
        pass

    def approval(self, kind, summary, payload=None, timeout_sec=None):
        return True

    def check_stop(self):
        pass


def run() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}/"

    root = CODE_ROOT / "farsi_watch_test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    paths = {"intent": root / "intent.db", "usage": root / "usage.db",
             "nlg": root / "nlg.db", "corpus": root / "corpus.db"}
    learn_was = corpus.learning_enabled()
    suggest_was = watch.suggestions_enabled()
    store = Store()
    watcher = None
    print("تست آفلاین پنل پیشنهاد")
    try:
        from agent.browser import Browser

        with Browser(store, _Quiet()) as browser:
            page = browser.new_page(base)
            watcher = watch.Watcher(browser, store, _Quiet(), paths=paths)
            watcher.attach(page)
            check("پنل داخل صفحه ساخته شد",
                  bool(page.evaluate("() => !!window.__farsiPanel")))
            check("پنل در Shadow DOM است تا با سایت تداخل نکند",
                  bool(page.evaluate(
                      "() => {const h = document.getElementById('farsi-panel-host');"
                      " return !!(h && h.shadowRoot);}")))

            watcher.step(page)
            check("پیش از متن تازه پیشنهادی نبود", watcher._pushed == 0,
                  str(watcher._pushed))

            page.click("#say")
            watcher._last_push = 0.0
            watcher.step(page)
            check("پس از متن تازه پیشنهاد آمد", watcher._pushed == 1,
                  str(watcher._pushed))
            shown = page.evaluate("() => window.__farsiPanel.current")
            check("پیشنهاد روی پنل نشست", bool(shown), str(shown))
            command = page.evaluate(
                "() => document.getElementById('farsi-panel-host')"
                ".shadowRoot.querySelector('.cmd').textContent")
            check("دستور داخل پنل درست است", "brain lesson next" in command, command)

            watcher._last_text = ""
            watcher._last_push = 0.0
            watcher.step(page)
            check("متن تکراری پیشنهاد دوباره نساخت", watcher._pushed == 1,
                  str(watcher._pushed))

            page.evaluate("() => window.__farsiPanel.outbox.push"
                          "({type:'dismiss', id: window.__farsiPanel.current})")
            watcher.step(page)
            check("بستن، پیشنهاد را از صف حذف کرد", not watcher._pending,
                  str(len(watcher._pending)))

            page.evaluate("() => window.__farsiPanel.outbox.push"
                          "({type:'run', id:'made-up-id'})")
            watcher.step(page)
            check("شناسهٔ ناشناس هیچ کاری نکرد", not watcher._pending)

            watch.set_suggestions(False)
            watcher._seen.clear()
            watcher._cooldown.clear()
            watcher._last_text = ""
            watcher._last_push = 0.0
            page.click("#say")
            watcher.step(page)
            check("کلید خاموش پیشنهاد را متوقف کرد", watcher._pushed == 1,
                  str(watcher._pushed))
            watch.set_suggestions(True)

            corpus.set_learning(False)
            before = corpus.stats(watcher.cdb)["docs"]
            page.click("#say")
            watcher._last_text = ""
            watcher.step(page)
            check("یادگیری خاموش چیزی ذخیره نکرد",
                  corpus.stats(watcher.cdb)["docs"] == before, str(before))

            corpus.set_learning(True)
            page.click("#say")
            watcher._last_text = ""
            watcher.step(page)
            after = corpus.stats(watcher.cdb)["docs"]
            check("یادگیری روشن متن را ذخیره کرد", after > before,
                  f"{before} -> {after}")

            page.reload(wait_until="domcontentloaded")
            watcher.step(page)
            check("پس از رفتن به صفحهٔ دیگر پنل برگشت",
                  bool(page.evaluate("() => !!window.__farsiPanel")))
    except (OSError, RuntimeError, AgentError, PlaywrightError) as exc:
        check("اجرای بدون خطا", False, str(exc))
    finally:
        if watcher is not None:
            watcher.close()
        store.close()
        corpus.set_learning(learn_was)
        watch.set_suggestions(suggest_was)
        server.shutdown()
        server.server_close()
        shutil.rmtree(root, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print("\nنتیجه:", "همه تست‌ها پاس شد" if not failed else f"{len(failed)} تست شکست خورد")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(run())