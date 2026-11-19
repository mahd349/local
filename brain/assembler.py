"""Assemble a plan into one runnable file, then check it before handing it over."""
from __future__ import annotations

import argparse
import ast
import copy
import json
import re
from pathlib import Path

from . import capability, probe
from agent.config import LIB_DIR, STATE_DIR, assert_writable, get_logger
from agent.errors import AgentError
from .planner import build_plan

log = get_logger("assembler")

OUT_DIR = STATE_DIR / "generated"
HEADER = '"""ساختهٔ ایجنت از قابلیت‌های تأییدشده."""\nfrom __future__ import annotations\n'


def func_name(body: str) -> str:
    try:
        tree = ast.parse(body)
    except SyntaxError as exc:
        raise AgentError(f"بدنهٔ قابلیت نامعتبر: {exc}") from exc
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    raise AgentError("بدنهٔ قابلیت باید یک تابع تعریف کند")


def render(plan) -> str:
    blocks, seen = [], set()
    for step in plan.steps:
        step["func"] = func_name(step["body"])
        if step["func"] not in seen:
            seen.add(step["func"])
            blocks.append(step["body"].rstrip())
    lines = [HEADER.rstrip(), ""]
    if plan.packages:
        # Standalone runs need the D: library folder on the search path.
        from agent.config import LIB_DIR
        lines += [
            "import sys as _sys",
            "from pathlib import Path as _Path",
            f'_LIB = _Path(r"{LIB_DIR}")',
            "if _LIB.is_dir() and str(_LIB) not in _sys.path:",
            "    _sys.path.insert(0, str(_LIB))",
            "",
        ]
    lines += blocks
    lines.append("")
    lines.append("def main():")
    if not plan.steps:
        lines.append("    print('نقشه خالی است')")
        lines.append("    return")
    last = None
    for step in plan.steps:
        call = f"{step['func']}({', '.join(step['args'])})"
        if step["outputs"]:
            lines.append(f"    {step['outputs'][0]} = {call}")
            last = step["outputs"][0]
        else:
            lines.append(f"    {call}")
    if last:
        lines.append(f"    print({last})")
    lines.append("")
    lines.append('if __name__ == "__main__":')
    lines.append("    main()")
    return "\n".join(lines) + "\n"


def build(plan, out_path: str | Path | None = None, run_check: bool = True) -> dict:
    code = render(plan)
    target = assert_writable(Path(out_path) if out_path else OUT_DIR / "program.py")
    try:
        compile(code, str(target), "exec")
    except SyntaxError as exc:
        raise AgentError(f"کد ساخته‌شده نامعتبر: {exc}") from exc

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    gui = any("gui" in step["effects"] for step in plan.steps)
    check, detail = "syntax-ok", ""
    if run_check and plan.steps and not gui and not plan.packages:
        # Run the real program; the sandbox may also write to our output folder.
        report = probe.run_snippet(code, timeout=8.0, label="assemble",
                                   allow_write=(OUT_DIR,))
        check = report["status"]
        detail = (report["detail"] or report["stderr"])[-200:]
    elif gui:
        check = "syntax-ok (gui: اجرای دستی)"
    elif plan.packages:
        check = "syntax-ok (نیاز به نصب)"

    written = False
    if check == "ok" or check.startswith("syntax-ok"):
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(code, encoding="utf-8")
        written = True
    return {"path": str(target), "code": code, "check": check,
            "detail": detail, "written": written}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.assembler", description="ساخت برنامه")
    parser.add_argument("request")
    parser.add_argument("--out", default=None)
    parser.add_argument("--param", action="append", default=[], dest="params")
    args = parser.parse_args(argv)
    params = {}
    for item in args.params:
        if "=" in item:
            key, value = item.split("=", 1)
            params[key] = value
    db = capability.prepare()
    try:
        plan = build_plan(args.request, capability.all_caps(db), params)
        if not plan.steps:
            print("نقشه‌ای ساخته نشد:", ", ".join(plan.gaps))
            return 1
        report = build(plan, out_path=args.out)
        print("مسیر:", report["path"])
        print("بررسی:", report["check"], report["detail"])
        if plan.packages:
            print("برای اجرای کامل باید نصب شود:", ", ".join(plan.packages))
        return 0
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())