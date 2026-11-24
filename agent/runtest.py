"""Offline test of the run / detect / repair loop."""

from __future__ import annotations

from .code_runner import fix_loop, run_python
from .config import CODE_ROOT, get_logger
from .errors import AgentError
from .notify import Notifier
from .store import Store

log = get_logger("runtest")


class _Counting(Notifier):
    def __init__(self):
        super().__init__(interactive=False)
        self.approvals = 0
        self.events = []

    def emit(self, kind, payload=None):
        self.events.append((kind, payload or {}))

    def approval(self, kind, summary, payload=None, timeout_sec=None):
        self.approvals += 1
        return True


class _Fixer:
    """Stands in for the chat worker: rewrites the file, good or bad."""

    def __init__(self, target, good):
        self.target = target
        self.good = good
        self.calls = 0

    def run_task(self, task, out_dir=None, **kwargs):
        self.calls += 1
        code = "print('fixed')\n" if self.good else "raise ValueError('still broken')\n"
        self.target.write_text(code, encoding="utf-8")
        return {"written": [str(self.target)], "parts": [], "blocks": [], "done": True}


def run() -> int:
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    root = CODE_ROOT / "agent_run_test"
    root.mkdir(parents=True, exist_ok=True)
    store, notifier = Store(), _Counting()
    print("تست آفلاین اجرا و تعمیر")
    try:
        good = root / "good.py"
        good.write_text("print('hello')\n", encoding="utf-8")
        result = run_python(good, store, notifier, approve=False)
        check("اسکریپت سالم ok شد", result["status"] == "ok" and "hello" in result["stdout"],
              result["status"])

        bad = root / "bad.py"
        bad.write_text("def f(:\n", encoding="utf-8")
        result = run_python(bad, store, notifier, approve=False)
        check("خطای نگارشی error شد و متن خطا گرفته شد",
              result["status"] == "error" and "SyntaxError" in result["stderr"])

        slow = root / "slow.py"
        slow.write_text("while True:\n    pass\n", encoding="utf-8")
        result = run_python(slow, store, notifier, timeout=2, approve=False)
        check("حلقهٔ بی‌پایان timeout شد", result["status"] == "timeout", result["status"])

        gui = root / "gui.py"
        gui.write_text(
            "import tkinter\n"
            "root = tkinter.Tk()\n"
            "root.title('AgentGuiProbe')\n"
            "root.geometry('140x90+0+0')\n"
            "root.after(60000, root.destroy)\n"
            "root.mainloop()\n", encoding="utf-8")
        result = run_python(gui, store, notifier, timeout=30, grace=3, approve=False)
        check("برنامهٔ پنجره‌دار gui_ok شد", result["status"] == "gui_ok", result["status"])

        once = root / "once.py"
        once.write_text("print(1)\n", encoding="utf-8")
        before = notifier.approvals
        run_python(once, store, notifier)
        run_python(once, store, notifier)
        check("تأیید فقط یک‌بار گرفته شد", notifier.approvals == before + 1,
              f"{notifier.approvals - before} بار")
        once.write_text("print(2)\n", encoding="utf-8")
        run_python(once, store, notifier)
        check("فایل تغییرکرده دوباره تأیید خواست", notifier.approvals == before + 2)

        repair = root / "repair.py"
        repair.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        fixer = _Fixer(repair, good=True)
        report = fix_loop(repair, fixer, store, notifier, max_loops=3)
        check("حلقهٔ تعمیر فایل را درست کرد",
              report["status"] == "ok" and fixer.calls == 1, report["status"])

        stubborn = root / "stubborn.py"
        stubborn.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
        fixer = _Fixer(stubborn, good=False)
        report = fix_loop(stubborn, fixer, store, notifier, max_loops=2)
        check("سقف حلقهٔ تعمیر رعایت شد",
              report["status"] == "failed" and len(report["attempts"]) == 3,
              f"{len(report['attempts'])} تلاش")

        outside = False
        try:
            run_python("C:/Windows/notepad.exe", store, notifier, approve=False)
        except (AgentError, PermissionError):
            outside = True
        check("اجرای بیرون سندباکس رد شد", outside)
    except (OSError, RuntimeError, AgentError) as exc:
        check("اجرای بدون خطا", False, str(exc))
    finally:
        store.close()

    failed = [name for name, ok, _ in results if not ok]
    print("\nنتیجه:", "همه تست‌ها پاس شد" if not failed else f"{len(failed)} تست شکست خورد")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(run())