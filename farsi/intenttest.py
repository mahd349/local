"""Offline test of the intent layer: evidence, safety, teaching, feedback."""
from __future__ import annotations

import shutil

from agent.config import CODE_ROOT, get_logger

from . import corpus, intent, usage

log = get_logger("farsi.intenttest")


class _Deny:
    def approval(self, kind, summary, payload=None, timeout_sec=None):
        return False


def run() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    print("تست آفلاین لایهٔ قصد")
    root = CODE_ROOT / "farsi_intent_test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    db = intent.prepare(root / "intent.db")
    udb = usage.connect(root / "usage.db")
    was_on = corpus.learning_enabled()
    try:
        listed = intent.routes(db)
        check("مسیرهای بذر ساخته شدند", len(listed) >= 9, str(len(listed)))
        check("نمونه‌ها ثبت شدند",
              sum(len(r["examples"]) for r in listed) >= 25,
              str(sum(len(r["examples"]) for r in listed)))

        learn = intent.classify("برو پایتون یاد بگیر", db, udb)
        check("درخواست پایتون action شد",
              learn["kind"] == "action" and learn["route"] == "learn.python",
              f"{learn['kind']} {learn['score']}")
        check("دستور درست ساخته شد",
              learn["argv"][1:] == ["-m", "brain", "lesson", "next"],
              " ".join(learn["argv"][1:]))

        sad = intent.classify("دلگیرم", db, udb)
        check("دلگیرم گفت‌وگو شد و اجرا نشد",
              sad["kind"] == "conversation" and not sad["argv"], sad["kind"])

        hello = intent.classify("سلام", db, udb)
        check("احوالپرسی گفت‌وگو شد",
              hello["kind"] == "conversation" and hello["route"] == "talk.greeting",
              hello["route"] or "-")

        odd = intent.classify("یک چیز کاملا بی ربط", db, udb)
        check("متن بی‌ربط هیچ ابزاری را اجرا نکرد",
              odd["kind"] == "conversation" and not odd["argv"], str(odd["score"]))

        near = intent.classify("پایتون", db, udb)
        check("نزدیکِ نامطمئن unknown شد", near["kind"] == "unknown",
              f"{near['kind']} {near['score']}")

        scan = intent.classify(r"کدها را اسکن کن D:\Python\learn_src", db, udb)
        check("مسیر از متن استخراج شد",
              scan["values"].get("path", "").lower().endswith("learn_src"),
              scan["values"].get("path", "-"))
        no_slot = intent.classify("کدها را اسکن کن", db, udb)
        check("نبود ورودی لازم گزارش شد",
              no_slot["missing"] == ["path"] and not no_slot["argv"],
              str(no_slot["missing"]))

        qr = intent.classify("یه برنامه بساز که کد QR بسازه", db, udb)
        check("درخواست با توضیح اضافه action شد",
              qr["kind"] == "action" and qr["route"] == "build.program",
              f"{qr['kind']} {qr['score']}")

        intent.teach(db, "learn.python", "پایتون")
        taught = intent.classify("پایتون", db, udb)
        check("نمونهٔ جدید تشخیص را عوض کرد",
              taught["kind"] == "action" and taught["route"] == "learn.python",
              f"{taught['kind']} {taught['score']}")

        before = next(r["weight"] for r in intent.routes(db) if r["key"] == "fix.code")
        after = intent.feedback(db, "fix.code", "نمونه", False)
        check("بازخورد منفی وزن را کم کرد", after < before, f"{before} -> {after}")

        blocked = intent.run("برو پایتون یاد بگیر", db, udb, _Deny(),
                             runner=lambda argv: 0)
        check("بدون تأیید چیزی اجرا نشد", blocked["status"] == "denied",
              blocked["status"])

        calls: list[list[str]] = []

        class _Allow:
            def approval(self, kind, summary, payload=None, timeout_sec=None):
                return True

        allowed = intent.run("برو پایتون یاد بگیر", db, udb, _Allow(),
                             runner=lambda argv: calls.append(argv) or 0)
        check("با تأیید اجرا شد", allowed["status"] == "ok" and len(calls) == 1,
              allowed["status"])
    finally:
        udb.close()
        db.close()
        corpus.set_learning(was_on)
        shutil.rmtree(root, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print("\nنتیجه:", "همه تست‌ها پاس شد" if not failed else f"{len(failed)} تست شکست خورد")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(run())