"""Offline test of project integration: insertion, preservation, backup, hooks."""
from __future__ import annotations

import ast

from agent.config import CODE_ROOT, get_logger
from agent.errors import AgentError

from . import capability, probe
from .integrate import merge, safe_identifier, top_names
from .planner import build_plan

log = get_logger("integratetest")

REQUEST = "متن را هش کند و ذخیره کند"
PARAMS = {"path": "out.txt", "value": "salam"}
TARGET = (
    '"""نمونهٔ موجود."""' + "\n"
    "import json" + "\n"
    "" + "\n"
    "SETTINGS = {'name': 'demo'}" + "\n"
    "" + "\n"
    "def existing(value):" + "\n"
    "    return json.dumps({'v': value}, ensure_ascii=False)" + "\n"
    "" + "\n"
    "def main():" + "\n"
    "    print(existing(1))" + "\n"
)


def run() -> int:
    results: list[tuple[str, bool, str]] = []

    def check(name: str, ok: bool, detail: str = "") -> None:
        results.append((name, ok, detail))
        print(("  ✓ " if ok else "  ! ") + name + (f" — {detail}" if detail else ""))

    print("تست آفلاین افزودن به پروژه")
    root = CODE_ROOT / "agent_integrate_test"
    root.mkdir(parents=True, exist_ok=True)
    db = capability.prepare()
    try:
        caps = capability.all_caps(db)
        plan = build_plan(REQUEST, caps, PARAMS)
        check("نقشه بدون کمبود ساخته شد", plan.ok, str(plan.gaps))
        check("نام نامعتبر پاک‌سازی شد",
              safe_identifier("2 bad-name!") == "agent_feature",
              safe_identifier("2 bad-name!"))

        target = root / "app.py"
        target.write_text(TARGET, encoding="utf-8")
        before_names = top_names(ast.parse(TARGET))

        dry = merge(target, build_plan(REQUEST, caps, PARAMS), dry_run=True)
        check("حالت dry-run چیزی ننوشت",
              not dry["written"] and target.read_text(encoding="utf-8") == TARGET)

        report = merge(target, build_plan(REQUEST, caps, PARAMS))
        merged = target.read_text(encoding="utf-8")
        check("فایل نوشته شد و پشتیبان گرفته شد",
              report["written"] and bool(report["backup"]), report["check"])
        check("تابع ورودی اضافه شد", f"def {report['entry']}()" in merged, report["entry"])
        check("نام‌های قبلی حفظ شدند",
              before_names <= top_names(ast.parse(merged)),
              str(sorted(before_names)))
        check("خطوط اصلی دست‌نخورده ماندند",
              all(line in merged for line in TARGET.splitlines()))
        try:
            ast.parse(merged)
            syntax = True
        except SyntaxError:
            syntax = False
        check("نتیجه نحو معتبر دارد", syntax)

        executed = probe.run_snippet(merged + f"\nprint({report['entry']}())\n",
                                     timeout=8.0, label="integrate")
        check("فایل ادغام‌شده اجرا شد", executed["status"] == "ok",
              executed["status"] + " " + (executed["detail"] or "")[:60])

        clash = root / "clash.py"
        clash.write_text(TARGET + "\ndef cap_literal(value):\n    return value\n",
                         encoding="utf-8")
        clash_report = merge(clash, build_plan(REQUEST, caps, PARAMS))
        check("نام تکراری بازنویسی نشد",
              any("گرفته شده بود" in note for note in clash_report["notes"]),
              str(clash_report["notes"])[:70])

        hooked = root / "hooked.py"
        hooked.write_text(TARGET, encoding="utf-8")
        hook_report = merge(hooked, build_plan(REQUEST, caps, PARAMS), hook="main")
        hook_text = hooked.read_text(encoding="utf-8")
        check("فراخوانی داخل main درج شد",
              f"    {hook_report['entry']}()" in hook_text, hook_report["entry"])
        try:
            ast.parse(hook_text)
            hook_syntax = True
        except SyntaxError:
            hook_syntax = False
        check("نتیجهٔ hook نحو معتبر دارد", hook_syntax)

        missing = root / "missing.py"
        missing.write_text(TARGET, encoding="utf-8")
        raised = False
        try:
            merge(missing, build_plan(REQUEST, caps, PARAMS), hook="no_such_func")
        except AgentError:
            raised = True
        check("hook روی تابع نبودن رد شد",
              raised and missing.read_text(encoding="utf-8") == TARGET)

        broken = root / "broken.py"
        broken.write_text("def f(:\n", encoding="utf-8")
        raised = False
        try:
            merge(broken, build_plan(REQUEST, caps, PARAMS))
        except AgentError:
            raised = True
        check("فایل مقصدِ خراب رد شد",
              raised and broken.read_text(encoding="utf-8") == "def f(:\n")

        outside = False
        try:
            merge("C:/Windows/agent_should_fail.py", build_plan(REQUEST, caps, PARAMS))
        except (AgentError, PermissionError):
            outside = True
        check("مسیر بیرون سندباکس رد شد", outside)

        crlf = root / "crlf.py"
        crlf.write_bytes(b"\xef\xbb\xbf" + TARGET.replace("\n", "\r\n").encode("utf-8"))
        merge(crlf, build_plan(REQUEST, caps, PARAMS))
        raw = crlf.read_bytes()
        check("BOM و CRLF حفظ شدند",
              raw.startswith(b"\xef\xbb\xbf") and b"\r\n" in raw)

        tail = root / "tail.py"
        tail.write_text(TARGET.rstrip("\n"), encoding="utf-8")
        merge(tail, build_plan(REQUEST, caps, PARAMS))
        try:
            ast.parse(tail.read_text(encoding="utf-8"))
            tail_syntax = True
        except SyntaxError:
            tail_syntax = False
        check("فایل بدون newline پایانی درست ادغام شد", tail_syntax)
    finally:
        db.close()

    failed = [name for name, ok, _ in results if not ok]
    print("\nنتیجه:", "همه تست‌ها پاس شد" if not failed else f"{len(failed)} تست شکست خورد")
    return 0 if not failed else 1


if __name__ == "__main__":
    raise SystemExit(run())