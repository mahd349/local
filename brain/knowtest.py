"""Offline test of the structural core and the knowledge store."""

from __future__ import annotations

from . import knowledge, pystruct
from agent.config import STATE_DIR, get_logger

log = get_logger("knowtest")

SRC_A = ("import json\n"
         "def load_cfg(p):\n"
         "    with open(p, encoding='utf-8') as f:\n"
         "        return json.load(f)\n")
SRC_RENAMED = ("import json\n"
               "def read_settings(path):\n"
               "    with open(path, encoding='utf-8') as handle:\n"
               "        return json.load(handle)\n")
SRC_PATH_1 = "def f():\n    return open('D:/a/x.json')\n"
SRC_PATH_2 = "def f():\n    return open('E:/b/y.json')\n"
SRC_TRY = ("import json\n"
           "def load_cfg(p):\n"
           "    try:\n"
           "        with open(p) as f:\n"
           "            return json.load(f)\n"
           "    except FileNotFoundError:\n"
           "        return {}\n")
SRC_PERSIAN = "def show():\n    print('سلام دنیا')\n"
SRC_EVAL = "def f(p):\n    return eval(p)\n"
SRC_SHELL = "import subprocess\ndef f(c):\n    subprocess.run(c, shell=True)\n"
SRC_CLASS = "class A:\n    def m(self, x):\n        return x + 1\n"
SRC_WRITE = "def w(p):\n    open(p, 'w').close()\n"
SRC_BAD = "def f(:\n"


def run() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    print("تست آفلاین هستهٔ ساختاری و دانش")
    db_path = STATE_DIR / "knowledge_test.db"
    db_path.unlink(missing_ok=True)
    db = knowledge.connect(db_path)
    try:
        units_a = pystruct.parse(SRC_A)
        check("تابع استخراج شد",
              len(units_a) == 1 and units_a[0].kind == "function", units_a[0].name)
        sig_a = units_a[0].signature
        sig_renamed = pystruct.parse(SRC_RENAMED)[0].signature
        check("تغییر نام‌ها امضا را عوض نکرد", sig_a == sig_renamed)

        u1, u2 = pystruct.parse(SRC_PATH_1)[0], pystruct.parse(SRC_PATH_2)[0]
        check("تغییر مقدار مسیر امضا را عوض نکرد", u1.signature == u2.signature)
        check("نقش اسلات PATH تشخیص داده شد",
              any(slot["role"] == "PATH" for slot in u1.slots),
              str([s["role"] for s in u1.slots]))
        check("تغییر ساختار امضا را عوض کرد",
              sig_a != pystruct.parse(SRC_TRY)[0].signature)

        persian = pystruct.parse(SRC_PERSIAN)[0]
        check("متن فارسی حفظ شد و تگ خورد",
              "سلام دنیا" in persian.source and "persian-text" in persian.tags)
        check("پرچم مشورتی eval شناسایی شد", "eval" in pystruct.parse(SRC_EVAL)[0].flags)
        check("پرچم shell=True شناسایی شد",
              "shell-true" in pystruct.parse(SRC_SHELL)[0].flags)
        check("کد خراب بدون کرش لیست خالی داد", pystruct.parse(SRC_BAD) == [])
        kinds = [(u.kind, u.name) for u in pystruct.parse(SRC_CLASS)]
        check("کلاس و متد استخراج شدند",
              ("class", "A") in kinds and ("method", "m") in kinds, str(kinds))

        knowledge.learn_source(db, "python", "test/a.py", SRC_A)
        knowledge.learn_source(db, "python", "test/a.py", SRC_A)
        check("یادگیری دوباره رکورد تکراری نساخت", knowledge.stats(db)["units"] == 1,
              str(knowledge.stats(db)))

        weight_up = knowledge.record_outcome(db, sig_a, "ok", "probe passed")
        row = knowledge.get(db, sig_a)
        check("موفقیت وزن را بالا برد و تأیید کرد",
              weight_up > knowledge.START_WEIGHT and row["verified"] == 1, str(weight_up))
        weight_down = knowledge.record_outcome(db, sig_a, "error", "boom")
        check("شکست وزن را پایین آورد", weight_down < weight_up, str(weight_down))

        sig_write = knowledge.learn_source(db, "python", "test/w.py", SRC_WRITE)[0]
        top = knowledge.find(db, tag="file-io", limit=5)
        check("بازیابی با تگ، الگوی تأییدشده را اول داد",
              bool(top) and top[0]["signature"] == sig_a, str(len(top)))

        for _ in range(4):
            knowledge.record_outcome(db, sig_write, "error", "broken")
        removed = knowledge.prune(db, min_weight=0.1)
        check("هرس الگوی ضعیفِ تأییدنشده را حذف کرد",
              removed == 1 and knowledge.get(db, sig_write) is None, str(removed))
        check("زبان غیرپایتون فعلاً رد می‌شود (آمادهٔ P4)",
              knowledge.learn_source(db, "javascript", "x.js", "let a=1;") == [])
    finally:
        db.close()
        db_path.unlink(missing_ok=True)

    failed = [name for name, ok, _ in results if not ok]
    print("\nنتیجه:", "همه تست‌ها پاس شد" if not failed else f"{len(failed)} تست شکست خورد")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(run())