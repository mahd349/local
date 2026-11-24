"""Offline Mission Mode test: DSL, retry, sandbox, PDF, downloads, reports."""

from __future__ import annotations

import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

from .browser import Browser
from .config import CODE_ROOT, DOWNLOAD_DIR, get_logger
from .dsl import build_mission, next_due, parse_duration
from .notify import Notifier
from .runner import Runner
from .store import Store

log = get_logger("missiontest")

_PAGE = """<!doctype html><html lang="fa" dir="rtl"><head><meta charset="utf-8">
<title>mission test</title></head><body>
<input id="q" name="query" placeholder="جست‌وجو">
<button id="send" onclick="send()">ارسال</button>
<button id="late-btn" onclick="late()">دیر</button>
<div id="result">-</div>
<div id="late"></div>
<a id="dl" href="/file.txt" download>دانلود</a>
<script>
function send() {
  const v = document.getElementById('q').value;
  fetch('/api/ping?q=' + encodeURIComponent(v)).then(r => r.json()).then(j => {
    document.getElementById('result').textContent = 'ok:' + j.echo;
  });
}
function late() {
  setTimeout(() => { document.getElementById('late').textContent = 'دیرآمد'; }, 3000);
}
</script></body></html>"""


class _Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt, *args):
        log.debug(fmt, args)

    def _send(self, body: bytes, mime: str, extra: dict | None = None) -> None:
        self.send_response(200)
        self.send_header("Content-Type", mime + "; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        parts = urlsplit(self.path)
        if parts.path == "/api/ping":
            echo = parse_qs(parts.query).get("q", [""])[0]
            self._send(('{"echo": "%s"}' % echo.replace('"', "")).encode("utf-8"), "application/json")
        elif parts.path == "/file.txt":
            self._send(b"agent mission download\n", "text/plain",
                       {"Content-Disposition": 'attachment; filename="agent-test.txt"'})
        else:
            self._send(_PAGE.encode("utf-8"), "text/html")


class _AutoNotifier(Notifier):
    """Test double: records events and answers approvals deterministically."""

    def __init__(self, grant: bool = True):
        super().__init__(interactive=False)
        self.grant = grant
        self.events: list[tuple[str, dict]] = []
        self.approvals: list[tuple[str, str]] = []

    def emit(self, kind: str, payload: dict | None = None) -> None:
        self.events.append((kind, payload or {}))

    def approval(self, kind: str, summary: str, payload: dict | None = None,
                 timeout_sec: float | None = None) -> bool:
        self.approvals.append((kind, summary))
        return self.grant


def run() -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    server.daemon_threads = True
    threading.Thread(target=server.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{server.server_address[1]}"
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    check("parse_duration درست کار می‌کند",
          parse_duration("500ms") == 0.5 and parse_duration("2m") == 120)
    now = time.time()
    check("زمان‌بندی every اولین اجرا را فوری می‌خواهد", next_due({"every": 60}, None, now) == now)
    check("زمان‌بندی at در آینده است", next_due({"at": (8, 30)}, None, now) > now)

    store = Store()
    notifier = _AutoNotifier(grant=True)
    out_dir = CODE_ROOT / "agent_mission_test"
    dest = DOWNLOAD_DIR / "agent-test.txt"
    if dest.exists():
        dest.unlink()
    print("تست آفلاین Mission Mode")
    try:
        with Browser(store, notifier) as browser:
            runner = Runner(store, notifier, browser=browser)
            mission = build_mission({
                "name": "offline-mission",
                "allow_hosts": ["127.0.0.1"],
                "retry": {"attempts": 4, "delay": "400ms"},
                "steps": [
                    {"goto": base + "/"},
                    {"fill": {"target": {"placeholder": "جست‌وجو"}, "value": "salam"}},
                    {"click": {"text": "ارسال"}},
                    {"assert_text": {"target": {"css": "#result"}, "equals": "ok:salam"}},
                    {"extract": {"target": {"css": "#result"}, "save_as": "title"}},
                    {"echo": "عنوان: {{title}}"},
                    {"click": {"text": "دیر", "exact": True}},
                    {"wait_for": {"text": "دیرآمد", "timeout": "400ms",
                                  "attempts": 6, "delay": "300ms"}},
                    {"make_dir": str(out_dir)},
                    {"write_file": {"path": str(out_dir / "hello.py"),
                                    "content": "print('salam')\n"}},
                    {"pdf": {"path": str(out_dir / "salam.pdf"),
                             "html": '<!doctype html><html lang="fa" dir="rtl"><meta charset="utf-8">'
                                     '<body style="font-family:Tahoma"><h1>سلام</h1></body></html>'}},
                    {"download": {"target": {"text": "دانلود"}, "dest": str(dest)}},
                    {"screenshot": "mission"},
                ],
            }, source="inline")
            report = runner.run_mission(mission)
            check("مأموریت کامل اجرا شد", report["status"] == "ok", report["error"][:200])
            late = next((s for s in report["steps"] if s["action"] == "wait_for"), {})
            check("retry مرحلهٔ دیررس را نجات داد", late.get("attempts", 0) >= 2,
                  f"attempts={late.get('attempts')}")
            check("متغیر استخراج و جای‌گذاری شد",
                  any(kind == "hud" and "ok:salam" in str(payload.get("text", ""))
                      for kind, payload in notifier.events))
            check("فایل در سندباکس ساخته شد", (out_dir / "hello.py").exists())
            pdf_path = out_dir / "salam.pdf"
            check("PDF فارسی ساخته شد",
                  pdf_path.exists() and pdf_path.read_bytes()[:4] == b"%PDF", str(pdf_path))
            check("دانلود با تأیید به مقصد منتقل شد", dest.exists(), str(dest))
            download_step = next((s for s in report["steps"] if s["action"] == "download"), {})
            check("دانلود در تلاش اول موفق شد", download_step.get("attempts") == 1,
                  f"attempts={download_step.get('attempts')}")
            check("گزارش JSON نوشته شد", Path(report["report_path"]).exists())

            bad = build_mission({
                "name": "outside-jail",
                "retry": {"attempts": 1, "delay": "100ms"},
                "steps": [{"write_file": {"path": "C:/Windows/agent_should_fail.txt",
                                          "content": "x"}}],
            }, source="inline")
            bad_report = Runner(store, notifier, browser=browser).run_mission(bad)
            check("نوشتن بیرون سندباکس رد شد", bad_report["status"] == "failed",
                  bad_report["error"][:120])

            deny_notifier = _AutoNotifier(grant=True)
            deny = build_mission({
                "name": "deny-download",
                "downloads": "deny",
                "steps": [{"goto": base + "/"},
                          {"download": {"target": {"text": "دانلود"}, "dest": str(dest)}}],
            }, source="inline")
            dest.unlink(missing_ok=True)
            deny_report = Runner(store, deny_notifier, browser=browser).run_mission(deny)
            check("حالت deny دانلود را به مقصد نمی‌برد",
                  deny_report["status"] == "ok" and not dest.exists())
    except (OSError, RuntimeError) as exc:
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
