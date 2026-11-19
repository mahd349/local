"""Offline test of local reply writing: retrieval, typed slots, honesty, feedback."""
from __future__ import annotations

import shutil

from agent.config import CODE_ROOT, get_logger

from . import nlg, usage

log = get_logger("farsi.nlgtest")


def _decision(kind, route="", title="", text="", values=None, argv=None, missing=None):
    return {"kind": kind, "route": route, "title": title, "text": text,
            "values": values or {}, "argv": argv or [], "missing": missing or []}


def run() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    print("تست آفلاین نوشتن پاسخ")
    root = CODE_ROOT / "farsi_nlg_test"
    shutil.rmtree(root, ignore_errors=True)
    root.mkdir(parents=True, exist_ok=True)
    db = nlg.prepare(root / "nlg.db")
    udb = usage.connect(root / "usage.db")
    try:
        check("قالب‌های بذر ساخته شدند", nlg.stats(db)["templates"] >= 10,
              str(nlg.stats(db)))

        action = _decision("action", "learn.python", "یادگیری پایتون",
                           argv=["python", "-m", "brain", "lesson", "next"])
        answer = nlg.reply(db, udb, action)
        check("برای دستور پاسخ نوشته شد", bool(answer["text"]), answer["key"])
        check("جایگاه خالی در پاسخ نماند", "{" not in answer["text"], answer["text"][:60])

        broken = nlg.upsert(db, {"key": "test.needs_path", "intent": "scan.code",
                                 "text": "مسیر: {path}",
                                 "slots": [{"name": "path", "type": "path"}],
                                 "weight": 0.9})
        pool = nlg.candidates(db, udb, "scan.code", {"title": "اسکن"})
        check("قالب با جایگاه خالی رد شد",
              broken not in [item["key"] for item in pool], str(len(pool)))
        typed = nlg.candidates(db, udb, "scan.code", {"path": "not-a-path"})
        check("جایگاه نوع‌دار مقدار نامعتبر را رد کرد",
              broken not in [item["key"] for item in typed])
        good = nlg.candidates(db, udb, "scan.code", {"path": "D:/Python"})
        check("مسیر معتبر پذیرفته شد", broken in [item["key"] for item in good])

        feeling = nlg.reply(db, udb, _decision("conversation", "talk.feeling",
                                              "گفت‌وگوی احساسی", text="دلگیرم"))
        check("دلگیرم پاسخ گفت‌وگویی گرفت",
              feeling["intent"] == "talk.feeling"
              and feeling["key"].startswith("feel.")
              and "انجام می‌دهم" not in feeling["text"],
              feeling["key"] + " " + feeling["text"][:40])

        why = _decision("conversation", "talk.explain", "پرسش توضیحی",
                        text="چرا آسمان آبی است")
        unknown = nlg.reply(db, udb, why)
        check("بدون واقعیت حدس نزد",
              not unknown["grounded"] and "یاد نگرفته" in unknown["text"],
              unknown["text"][:50])
        nlg.remember_fact(db, "چرا آسمان آبی است", "پراکندگی نور در جو.")
        known = nlg.reply(db, udb, why)
        check("با واقعیت ذخیره‌شده پاسخ مستند داد",
              known["grounded"] and "پراکندگی" in known["text"], known["text"][:50])

        near = nlg.reply(db, udb, _decision("unknown", "build.program", "ساخت برنامه"))
        check("حالت نامطمئن تأیید خواست", "درست است" in near["text"], near["text"][:50])
        check("حالت نامطمئن وعدهٔ اجرا نداد",
              near["key"].startswith("unknown.") and "انجام می‌دهم" not in near["text"],
              near["key"])

        key = nlg.teach(db, "talk.greeting", "سلام رفیق، بگو چه کار کنیم.")
        taught = nlg.reply(db, udb, _decision("conversation", "talk.greeting", "احوالپرسی"))
        check("قالب آموزش‌داده‌شده استفاده شد", taught["key"] == key, taught["key"])

        up = nlg.feedback(db, key, True)
        down = nlg.feedback(db, key, False)
        check("بازخورد وزن را جابه‌جا کرد", up > 0.6 and down < up, f"{up} -> {down}")

        first = nlg.reply(db, udb, _decision("conversation", "talk.greeting"), vary=True)
        second = nlg.reply(db, udb, _decision("conversation", "talk.greeting"), vary=True)
        check("چرخش تنوع ایجاد کرد", first["text"] != second["text"],
              first["key"] + " / " + second["key"])

        check("پیکرهٔ کوچک رتبه‌بندی روانی را خاموش کرد",
              any("پیکره کوچک" in note for note in second["notes"]),
              str(second["notes"]))

        check("فراموشی قالب را حذف کرد", nlg.forget(db, broken)
              and nlg.stats(db)["templates"] >= 10)
        check("آمار واقعیت‌ها را داد", nlg.stats(db)["facts"] == 1, str(nlg.stats(db)))
    finally:
        udb.close()
        db.close()
        shutil.rmtree(root, ignore_errors=True)

    failed = [name for name, ok, _ in results if not ok]
    print("\nنتیجه:", "همه تست‌ها پاس شد" if not failed else f"{len(failed)} تست شکست خورد")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(run())