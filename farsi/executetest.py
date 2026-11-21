"""Offline test of the panel run path: approval, one-shot ids, weight feedback.

No browser is used: a stand-in page answers the same evaluate calls the watcher
makes, so the whole path is deterministic and needs no network.
"""
from __future__ import annotations

import shutil
import sys

from agent.config import CODE_ROOT, get_logger
from agent.errors import AgentError

from . import execute, intent, watch

log = get_logger("farsi.executetest")


class _Page:
    """Answers the watcher's evaluate calls without a real browser."""

    def __init__(self, text: str = ""):
        self.text = text
        self.outbox: list[dict] = []
        self.shown: list[dict] = []
        self.results: list[dict] = []

    def evaluate(self, script: str, arg=None):
        if "!!window.__farsiPanel" in script:
            return True
        if "outbox.slice" in script:
            items = self.outbox[:]
            self.outbox.clear()
            return items
        if ".show(i)" in script:
            self.shown.append(arg)
        elif ".result(r)" in script:
            self.results.append(arg)
        elif ".status(s)" in script:
            pass
        else:
            return self.text
        return None


class _Notifier:
    def __init__(self, grant: bool = True):
        self.grant = grant
        self.asked = 0

    def check_stop(self):
        pass

    def approval(self, kind, summary, payload=None, timeout_sec=None):
        self.asked += 1
        return self.grant


def run() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    print("تست آفلاین اجرای پیشنهاد")
    root = CODE_ROOT / "farsi_execute_test"
    shutil.rmtree(root, ignore_errors=True)
    (root / "box").mkdir(parents=True, exist_ok=True)
    paths = {"intent": root / "intent.db", "usage": root / "usage.db",
             "nlg": root / "nlg.db", "corpus": root / "corpus.db"}
    watcher = None

    def weight() -> float:
        return next(r["weight"] for r in intent.routes(watcher.idb)
                    if r["key"] == "learn.python")

    def offer(token: str) -> None:
        watcher._pending[token] = {
            "route": "learn.python",
            "argv": [sys.executable, "-m", "brain", "list"],
            "text": "برو پایتون یاد بگیر",
        }
        page.outbox.append({"type": "run", "id": token})

    try:
        rejected = False
        try:
            execute.check_argv([sys.executable, "-c", "print(1)"])
        except AgentError:
            rejected = True
        check("دستور بیرون از فهرست رد شد", rejected)

        good = execute.run_argv([sys.executable, "-m", "brain", "list"])
        check("دستور سالم اجرا شد", good["ok"], good["status"])
        bad = execute.run_argv([sys.executable, "-m", "brain", "nosuch"])
        check("دستور خطادار error شد",
              not bad["ok"] and bad["status"] == "error", bad["status"])
        slow = execute.run_argv(
            [sys.executable, "-m", "brain", "ingest", "watch",
             "--folder", str(root / "box"), "--interval", "2"], timeout=1.5)
        check("زمان اجرا محدود شد", slow["status"] == "timeout", slow["status"])

        calls: list[list] = []
        notifier = _Notifier(grant=True)
        watcher = watch.Watcher(
            None, None, notifier, paths=paths,
            runner=lambda argv: calls.append(argv) or
            {"ok": True, "status": "ok", "detail": "done"})
        page = _Page()
        start_weight = weight()
        offer("tok1")
        watcher.step(page)
        check("با تأیید یک‌بار اجرا شد", len(calls) == 1, str(len(calls)))
        check("دستور از خودِ واچر آمد نه صفحه",
              bool(calls) and calls[0][2] == "brain", str(calls[:1])[:70])
        check("نتیجه به پنل برگشت",
              bool(page.results) and page.results[-1]["ok"],
              str(page.results[-1:])[:70])
        up_weight = weight()
        check("موفقیت وزن مسیر را بالا برد", up_weight > start_weight,
              f"{start_weight} -> {up_weight}")

        page.outbox.append({"type": "run", "id": "tok1"})
        watcher.step(page)
        check("شناسهٔ مصرف‌شده دوباره اجرا نشد", len(calls) == 1, str(len(calls)))
        page.outbox.append({"type": "run", "id": "made-up"})
        watcher.step(page)
        check("شناسهٔ ناشناس هیچ کاری نکرد", len(calls) == 1, str(len(calls)))

        notifier.grant = False
        offer("tok2")
        watcher.step(page)
        check("بدون تأیید اجرا نشد", len(calls) == 1, str(len(calls)))
        check("رد کردن به پنل گزارش شد",
              bool(page.results) and page.results[-1]["status"] == "denied",
              str(page.results[-1:])[:70])
        check("رد کردن وزن را کم نکرد", weight() == up_weight, str(weight()))

        notifier.grant = True
        watcher.runner = lambda argv: {"ok": False, "status": "error", "detail": "boom"}
        offer("tok3")
        watcher.step(page)
        check("شکست اجرا وزن را کم کرد", weight() < up_weight,
              f"{up_weight} -> {weight()}")

        watcher._pending["tok4"] = {"route": "learn.python",
                                    "argv": [sys.executable, "-m", "brain", "list"],
                                    "text": "x"}
        page.outbox.append({"type": "dismiss", "id": "tok4"})
        watcher.step(page)
        check("بستن چیزی اجرا نکرد",
              len(calls) == 1 and "tok4" not in watcher._pending, str(len(calls)))
    finally:
        if watcher is not None:
            watcher.close()
        shutil.rmtree(root, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print("\nنتیجه:", "همه تست‌ها پاس شد" if not failed else f"{len(failed)} تست شکست خورد")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(run())