"""Offline test of bulk scanning and the AI inbox."""
from __future__ import annotations

import json
import shutil

from agent.config import CODE_ROOT, get_logger

from . import ingest, knowledge

log = get_logger("ingesttest")

ADD = "def add(a, b):\n    return a + b\n"
MD = "متن آموزشی\n```python\ndef mul(a, b):\n    return a * b\n```\n"
CHAT = {"messages": [{"role": "assistant",
                      "content": "### FILE: x.py\n```python\ndef sub(a, b):\n"
                                 "    return a - b\n```\nاتمام کد"}]}


def run() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    print("تست آفلاین اسکن و پوشهٔ ورودی")
    root = CODE_ROOT / "agent_ingest_test"
    shutil.rmtree(root, ignore_errors=True)
    (root / "src" / "__pycache__").mkdir(parents=True, exist_ok=True)
    box = root / "inbox"
    box.mkdir(parents=True, exist_ok=True)
    db_path = root / "ingest_test.db"
    try:
        (root / "src" / "a.py").write_text(ADD, encoding="utf-8")
        (root / "src" / "b.txt").write_text("not python", encoding="utf-8")
        (root / "src" / "__pycache__" / "c.py").write_text(ADD, encoding="utf-8")

        found = list(ingest.iter_python_files([str(root / "src")]))
        check("فقط فایل‌های پایتون پیدا شدند", len(found) == 1,
              str([p.name for p in found]))

        first = ingest.scan([str(root / "src")], db_path=db_path)
        check("اسکن الگو یاد گرفت", first["learned"] >= 1, str(first))

        ingest.scan([str(root / "src")], db_path=db_path)
        db = knowledge.connect(db_path)
        total = knowledge.stats(db)["units"]
        db.close()
        check("اسکن دوباره رکورد تکراری نساخت", total == first["learned"], str(total))

        big = root / "src" / "big.py"
        big.write_text("# pad\n" * 70000, encoding="utf-8")
        capped = ingest.scan([str(root / "src")], db_path=db_path)
        check("فایل بزرگ رد شد", capped["skipped"] >= 1, str(capped))
        big.unlink(missing_ok=True)

        limited = ingest.scan([str(root / "src")], limit=1, db_path=db_path)
        check("سقف تعداد فایل رعایت شد", limited["files"] == 1, str(limited))

        smoked = ingest.scan([str(root / "src")], verify_count=3, db_path=db_path)
        check("بررسی پایه انجام شد", smoked["checked"] >= 1, str(smoked))
        db = knowledge.connect(db_path)
        info = knowledge.stats(db)
        db.close()
        check("الگوی سالم تأیید شد", info["verified"] >= 1, str(info))

        (box / "one.py").write_text(ADD, encoding="utf-8")
        (box / "two.md").write_text(MD, encoding="utf-8")
        (box / "chat.json").write_text(json.dumps(CHAT, ensure_ascii=False), encoding="utf-8")
        (box / "ignore.bin").write_text("x", encoding="utf-8")

        processed = ingest.inbox(folder=box, db_path=db_path)
        check("سه فایل ورودی پردازش شدند", processed["files"] == 3, str(processed))
        check("بلوک‌های کد استخراج شدند", processed["blocks"] >= 3, str(processed))
        check("فایل نامربوط نادیده گرفته شد", processed["ignored"] == 1, str(processed))
        check("فایل‌ها به _done منتقل شدند",
              len(list((box / "_done").iterdir())) == 3, "")

        db = knowledge.connect(db_path)
        names = {row["name"] for row in db.execute("SELECT name FROM units").fetchall()}
        db.close()
        check("توابع هر سه منبع یاد گرفته شدند",
              {"add", "mul", "sub"} <= names, str(sorted(names)))

        report = ingest.stats(db_path=db_path, folder=box)
        check("آمار حجم و صف را داد",
              "mb" in report and report["inbox_pending"] == 1, str(report))
    finally:
        shutil.rmtree(root, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print("\nنتیجه:", "همه تست‌ها پاس شد" if not failed else f"{len(failed)} تست شکست خورد")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(run())