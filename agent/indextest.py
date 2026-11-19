"""Offline test of the file index: text, code, PDF, exclusions, incremental."""

from __future__ import annotations

from pathlib import Path

from .browser import Browser
from .config import CODE_ROOT, get_logger
from .errors import AgentError
from .fileops import html_to_pdf
from .indexer import build, connect, fixed_drives, search
from .notify import Notifier
from .store import Store

log = get_logger("indextest")


def run() -> int:
    results = []

    def check(name, ok, detail=""):
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    root = CODE_ROOT / "agent_index_test"
    root.mkdir(parents=True, exist_ok=True)
    (root / "note.txt").write_text("AgentTextProbe یک متن آزمایشی فارسی", encoding="utf-8")
    (root / "AgentCodeProbe.py").write_text("print('hello')\n", encoding="utf-8")

    print("تست آفلاین ایندکس")
    store, notifier = Store(), Notifier(interactive=False)
    try:
        db = connect()
        db.close()
        check("FTS5 در دسترس است", True)
        check("درایو C در فهرست ریشه‌ها نیست",
              all(not drive.upper().startswith("C:") for drive in fixed_drives()),
              ", ".join(fixed_drives()))

        with Browser(store, notifier) as browser:
            html_to_pdf(browser,
                        '<!doctype html><meta charset="utf-8"><body><p>AgentPdfProbe</p></body>',
                        str(root / "probe.pdf"))

        first = build([str(root)], content=True)
        check("فایل‌ها ایندکس شدند", first["indexed"] >= 3, str(first))
        check("محتوای متن فارسی پیدا شد",
              any("note.txt" in hit["path"] for hit in search("AgentTextProbe")))
        check("نام فایل کد پیدا شد",
              any("AgentCodeProbe.py" in hit["path"] for hit in search("AgentCodeProbe")))
        pdf_hits = search("AgentPdfProbe")
        check("متن PDF استخراج و پیدا شد",
              any(hit["path"].endswith("probe.pdf") for hit in pdf_hits), str(pdf_hits[:1]))

        second = build([str(root)], content=True)
        check("اجرای دوم فایل‌های unchanged را رد کرد",
              second["indexed"] == 0 and second["skipped"] >= 3, str(second))
    except (OSError, RuntimeError, AgentError) as exc:
        check("اجرای بدون خطا", False, str(exc))
    finally:
        store.close()

    failed = [name for name, ok, _ in results if not ok]
    print("\nنتیجه:", "همه تست‌ها پاس شد" if not failed else f"{len(failed)} تست شکست خورد")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(run())