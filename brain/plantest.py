"""Offline test of capability cards, planning, type wiring and assembly."""
from __future__ import annotations
import hashlib
import json

from . import assembler, capability, probe
from agent.config import STATE_DIR, get_logger
from agent.errors import AgentError
from .planner import build_plan, compatible, match_goals, normalize

log = get_logger("plantest")

HASH_REQ = "برنامه ای بساز که متن را هش کند و ذخیره کند"
GUI_REQ = "متن را هش کند و در پنجره نمایش دهد"
QR_REQ = "برنامه ای بساز که کیو آر کد بسازه و عکس رو نشون بده و یه لینک هم بده"


def run() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    print("تست آفلاین قابلیت، نقشه و ساخت")
    db_path = STATE_DIR / "caps_test.db"
    db_path.unlink(missing_ok=True)
    db = capability.connect(db_path)
    try:
        capability.seed(db)
        caps = capability.all_caps(db)
        check("کارت‌های پایه بارگذاری شدند", len(caps) >= 8, str(len(caps)))

        check("نرمال‌سازی فارسی درست کار می‌کند",
              normalize("كيو\u200cآر كتابي") == "کیوآر کتابی",
              normalize("كيو\u200cآر كتابي"))
        check("سازگاری نوع دقیق است", compatible("str", "text")
              and compatible("path", "path") and not compatible("path", "str"))

        keys = match_goals(caps, HASH_REQ)
        check("هدف‌ها از درخواست فارسی استخراج شدند",
              "text.sha256" in keys and "text.write" in keys, str(keys))

        plan = build_plan(HASH_REQ, caps, {"path": "out.txt", "value": "salam"})
        check("نقشه بدون کمبود ساخته شد", plan.ok, str(plan.gaps))
        check("پیش‌نیاز متن خودکار تأمین شد",
              any(s["key"] == "text.literal" for s in plan.steps),
              str([s["key"] for s in plan.steps]))
        check("وابستگی خارجی لازم نبود", plan.packages == [], str(plan.packages))

        code = assembler.render(plan)
        try:
            compile(code, "<plan>", "exec")
            syntax = True
        except SyntaxError:
            syntax = False
        check("کد ترکیبی از نظر نگارشی سالم است", syntax)

        keys_used = [s["key"] for s in plan.steps]
        check("قدم تکراری ساخته نشد",
              len(keys_used) == len(set(keys_used)), str(keys_used))

        expected = hashlib.sha256("salam".encode("utf-8")).hexdigest()
        report = probe.run_snippet(code + "\nprint(open('out.txt').read())\n",
                                   timeout=8.0, label="plantest")
        check("برنامهٔ ساخته‌شده اجرا شد و هش درست را نوشت",
              report["status"] == "ok" and expected in report["stdout"],
              report["status"] + " " + (report["detail"] or "")[:80])

        gui_plan = build_plan(GUI_REQ, caps, {"value": "salam"})
        check("نقشهٔ پنجره‌دار قابلیت نمایش متن را برداشت",
              any(s["key"] == "gui.show_text" for s in gui_plan.steps),
              str([s["key"] for s in gui_plan.steps]))

        qr_plan = build_plan(QR_REQ, caps)
        check("درخواست QR همان قابلیت را برداشت",
              any(s["key"] == "qr.make" for s in qr_plan.steps),
              str([s["key"] for s in qr_plan.steps]))
        check("نمایش عکس انتخاب شد نه نمایش متن",
              any(s["key"] == "gui.show_image" for s in qr_plan.steps)
              and not any(s["key"] == "gui.show_text" for s in qr_plan.steps))
        check("لینک هم به زنجیره وصل شد",
              any(s["key"] == "file.link" for s in qr_plan.steps))
        check("نیاز به نصب صادقانه گزارش شد", "qrcode" in qr_plan.packages,
              str(qr_plan.packages))
        check("قابلیت بدون خروجی کمبود نشد", qr_plan.gaps == [], str(qr_plan.gaps))

        empty = build_plan("یک چیز کاملاً بی ربط", caps)
        check("درخواست بی‌هدف با کمبود گزارش شد",
              not empty.ok and "no-goal" in empty.gaps, str(empty.gaps))

        capability.seed(db)
        check("بارگذاری دوباره رکورد تکراری نساخت",
              len(capability.all_caps(db)) == len(caps))

        folder = STATE_DIR / "caps_test_json"
        folder.mkdir(parents=True, exist_ok=True)
        (folder / "extra.json").write_text(json.dumps({
            "key": "text.upper", "title": "بزرگ کردن حروف",
            "goals": ["بزرگ", "upper"],
            "inputs": [{"name": "text", "type": "str"}],
            "outputs": [{"name": "text", "type": "str"}],
            "body": "def cap_upper(text):\n    return str(text).upper()\n",
        }, ensure_ascii=False), encoding="utf-8")
        added = capability.load_dir(db, folder)
        check("کارت جدید از فایل JSON خوانده شد",
              added == 1 and capability.get(db, "text.upper") is not None, str(added))

        weight = capability.record(db, "text.upper", True)
        check("بازخورد اجرا وزن قابلیت را بالا برد", weight > 0.5, str(weight))
    finally:
        db.close()
        db_path.unlink(missing_ok=True)

    failed = [name for name, ok, _ in results if not ok]
    print("\nنتیجه:", "همه تست‌ها پاس شد" if not failed else f"{len(failed)} تست شکست خورد")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(run())