"""Offline test of the validation harness. No browser, no network."""
from __future__ import annotations

import json

from . import knowledge, probe, pystruct, verify
from agent.config import STATE_DIR, get_logger

log = get_logger("verifytest")

ADD = "def add(a, b):\n    return a + b\n"
NAMED = "def add(a, b):\n    total = a + b\n    return total\n"


def run() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    print("تست آفلاین اعتبارسنجی")
    db_path = STATE_DIR / "verify_test.db"
    db_path.unlink(missing_ok=True)
    db = knowledge.connect(db_path)
    try:
        simple = probe.run_snippet("print('salam')")
        check("اجرای ساده ok شد",
              simple["status"] == "ok" and "salam" in simple["stdout"], simple["status"])

        broken = probe.run_snippet("raise ValueError('x')")
        check("خطای کد error شد",
              broken["status"] == "error" and "ValueError" in broken["stderr"], broken["status"])

        loop = probe.run_snippet("while True:\n    pass\n", timeout=1.0)
        check("حلقهٔ بی‌پایان timeout شد", loop["status"] == "timeout", loop["status"])

        net = probe.run_snippet("import socket")
        check("import شبکه متوقف شد", net["status"] == "blocked", net["status"])

        proc = probe.run_snippet("import subprocess")
        check("import پردازش متوقف شد", proc["status"] == "blocked", proc["status"])

        outside = STATE_DIR / "probe_guard_sample.txt"
        outside.unlink(missing_ok=True)
        write_out = probe.run_snippet("open(" + json.dumps(str(outside)) + ", 'w').write('x')")
        check("نوشتن بیرون از پوشهٔ کار متوقف شد",
              write_out["status"] == "blocked" and not outside.exists(), write_out["status"])

        write_in = probe.run_snippet("open('t.txt', 'w').write('ok')\nprint('done')")
        check("نوشتن داخل پوشهٔ کار مجاز بود", write_in["status"] == "ok", write_in["status"])

        renamed = verify.rename_locals(NAMED)
        check("نام عمومی و پارامترها حفظ شد", "def add(a, b)" in renamed, renamed[:40])
        check("نام محلی تغییر کرد", bool(renamed) and "total" not in renamed, "")
        run_renamed = probe.run_snippet(renamed + "\nassert add(2, 3) == 5\n")
        check("کد تغییرنام‌یافته اجرا شد", run_renamed["status"] == "ok", run_renamed["status"])

        variants = verify.mutants("def f(x):\n    return x + 1\n", 4)
        check("جهش تولید شد", len(variants) >= 1, str(len(variants)))
        check("کد نامعتبر جهش نداد", verify.mutants("def f(:\n", 3) == [], "")

        prelude = verify.imports_of("import json\nfrom math import sqrt\n\ndef f():\n    return sqrt(4)\n")
        check("خطوط import جدا شدند",
              "import json" in prelude and "from math import sqrt" in prelude,
              prelude.replace("\n", " | "))

        unit = pystruct.parse(ADD)[0].as_dict()
        knowledge.upsert_unit(db, unit, "python", "test/add.py")

        good = verify.verify_unit(db, unit, ["assert add(2,3)==5", "assert add(-1,1)==0"],
                                  budget=probe.Budget(30, 60), mutation_limit=3)
        check("واحد سالم تأیید شد", good["status"] == "ok", good["status"] + " " + good["detail"][:60])
        check("دگردیسی هم‌رفتار بود", good["metamorphic"] is True, str(good["metamorphic"]))
        check("امتیاز جهش مثبت شد", (good["mutation"] or 0) > 0, str(good["mutation"]))
        row = knowledge.get(db, unit["signature"])
        check("تأیید در پایگاه دانش ثبت شد", bool(row) and row["verified"] == 1, "")

        weak = verify.verify_unit(db, unit, ["assert isinstance(add(1,2), int)"],
                                  budget=probe.Budget(30, 60), mutation_limit=3)
        check("آزمون ضعیف امتیاز جهش صفر گرفت",
              weak["status"] == "ok" and (weak["mutation"] or 0) == 0, str(weak["mutation"]))
        check("اعتماد بالا داده نشد", weak["confidence"] != "high", weak["confidence"])

        bad = verify.verify_unit(db, unit, ["assert add(1,1)==3"],
                                 budget=probe.Budget(30, 60), mutation_limit=3)
        check("آزمون ناموفق error ثبت کرد", bad["status"] == "error", bad["status"])

        tight = verify.verify_unit(db, unit, ["assert add(1,1)==2"],
                                   budget=probe.Budget(1, 60), mutation_limit=3)
        check("سهمیهٔ کم حالت partial داد", tight["status"] == "partial", tight["status"])
    finally:
        db.close()
        db_path.unlink(missing_ok=True)

    failed = [name for name, ok, _ in results if not ok]
    print("\nنتیجه:", "همه تست‌ها پاس شد" if not failed else f"{len(failed)} تست شکست خورد")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(run())