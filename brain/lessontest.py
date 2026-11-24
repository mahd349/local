"""Offline test of the lesson loop: no browser, no network."""
from __future__ import annotations

import shutil

from agent.config import CODE_ROOT, get_logger

from . import knowledge, lesson, probe

log = get_logger("lessontest")

GOOD = "def double(n):\n    return n * 2\n\nassert double(3) == 6\n"
BAD = "def broken(n):\n    return n + 1\n\nassert broken(3) == 99\n"


class _Fake:
    """Stands in for the chat worker."""

    def __init__(self, blocks):
        self.blocks = blocks
        self.calls = 0
        self.prompt = ""

    def run_task(self, task, out_dir=None, write=True, max_parts=None):
        self.calls += 1
        self.prompt = task
        return {"parts": [{"index": 1}], "blocks": self.blocks, "written": [], "done": True}


def run() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    print("تست آفلاین درس‌گیر")
    root = CODE_ROOT / "agent_lesson_test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    old_topics, old_progress = lesson.TOPICS_PATH, lesson.PROGRESS_PATH
    lesson.TOPICS_PATH = root / "topics.json"
    lesson.PROGRESS_PATH = root / "progress.json"
    db = knowledge.connect(root / "lesson_test.db")
    try:
        listed = lesson.topics()
        check("برنامهٔ درسی ساخته شد", len(listed) >= 20, str(len(listed)))
        first = lesson.next_topic()
        check("موضوع بعدی انتخاب شد", first == listed[0], str(first))

        text = lesson.prompt_for("json")
        check("موضوع در درخواست آمد", "json" in text)
        check("قانون assert در درخواست آمد", "assert" in text)

        session = _Fake([{"name": "a.py", "code": GOOD}, {"name": "b.py", "code": BAD}])
        stats = lesson.run_lesson(session, db, first, parts=1,
                                  budget=probe.Budget(10, 60))
        check("درس یک‌بار پرسیده شد", session.calls == 1, str(session.calls))
        check("مثال سالم تأیید شد", stats["verified"] == 1, str(stats))
        check("مثال غلط رد شد", stats["rejected"] == 1, str(stats))
        check("الگو ذخیره شد", stats["units"] >= 1, str(stats["units"]))
        check("دانش تأییدشده ثبت شد", knowledge.stats(db)["verified"] >= 1,
              str(knowledge.stats(db)))
        check("پیشرفت جلو رفت", lesson.next_topic() != first, str(lesson.next_topic()))

        tight = lesson.absorb(db, "test", [{"code": GOOD}, {"code": GOOD}],
                              budget=probe.Budget(1, 60))
        check("سهمیهٔ کم بدون کرش رعایت شد", tight["blocks"] <= 1, str(tight))

        lesson._save(lesson.PROGRESS_PATH, {})
        check("پاک کردن پیشرفت کار کرد", lesson.next_topic() == first)
    finally:
        db.close()
        lesson.TOPICS_PATH, lesson.PROGRESS_PATH = old_topics, old_progress
        shutil.rmtree(root, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print("\nنتیجه:", "همه تست‌ها پاس شد" if not failed else f"{len(failed)} تست شکست خورد")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(run())