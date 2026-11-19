"""Merge a planned feature into an existing Python file by inserting whole lines.

Nothing is deleted or reformatted. The edit is accepted only if the original
text can be recovered exactly, both versions parse, and every pre-existing
top-level name survives. A backup is written before touching the target.
"""
from __future__ import annotations

import argparse
import ast
import json
import keyword
import re
import shutil
import time
from pathlib import Path

from agent.config import LIB_DIR, REPORT_DIR, STATE_DIR, assert_writable, get_logger
from agent.errors import AgentError

from . import capability, probe
from .planner import build_plan

log = get_logger("integrate")
BACKUP_DIR = STATE_DIR / "backups"


def _parse(source: str, label: str) -> ast.Module:
    try:
        return ast.parse(source)
    except SyntaxError as exc:
        raise AgentError(f"{label} نحو معتبر ندارد (خط {exc.lineno}): {exc.msg}") from exc


def func_name(body: str) -> str:
    for node in _parse(body, "بدنهٔ قابلیت").body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            return node.name
    raise AgentError("بدنهٔ قابلیت باید یک تابع تعریف کند")


def top_names(tree: ast.Module) -> set[str]:
    names: set[str] = set()
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            names.update(t.id for t in node.targets if isinstance(t, ast.Name))
        elif isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, (ast.Import, ast.ImportFrom)):
            names.update(a.asname or a.name.split(".")[0] for a in node.names)
    return names


def safe_identifier(text: str, fallback: str = "agent_feature") -> str:
    cleaned = re.sub(r"\W+", "_", str(text).strip(), flags=re.UNICODE).strip("_")
    if not cleaned or cleaned[0].isdigit() or keyword.iskeyword(cleaned):
        cleaned = fallback
    return cleaned[:60]


def _unique(name: str, taken: set[str]) -> str:
    if name not in taken:
        return name
    index = 2
    while f"{name}_{index}" in taken:
        index += 1
    return f"{name}_{index}"


class _Rename(ast.NodeTransformer):
    def __init__(self, old: str, new: str):
        self.old, self.new = old, new

    def visit_Name(self, node):
        if node.id == self.old:
            node.id = self.new
        return node

    def visit_FunctionDef(self, node):
        if node.name == self.old:
            node.name = self.new
        self.generic_visit(node)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef


def _renamed_body(body: str, old: str, new: str) -> str:
    if old == new:
        return body.rstrip()
    return ast.unparse(_Rename(old, new).visit(_parse(body, "بدنهٔ قابلیت")))


def _header_end(tree: ast.Module, lines: list[str]) -> int:
    """Insert after the docstring and the leading imports, never inside a body."""
    index = 0
    for position, node in enumerate(tree.body):
        if position == 0 and isinstance(node, ast.Expr) \
                and isinstance(node.value, ast.Constant) \
                and isinstance(node.value.value, str):
            index = node.end_lineno
            continue
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            index = node.end_lineno
            continue
        break
    if index == 0:
        while index < len(lines) and (lines[index].startswith("#") or not lines[index].strip()):
            index += 1
    return index


def _hook_point(tree: ast.Module, lines: list[str], hook: str) -> tuple[int, str, bool]:
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)) and node.name == hook:
            last = node.body[-1]
            first_line = lines[node.body[0].lineno - 1]
            indent = first_line[:len(first_line) - len(first_line.lstrip())]
            return last.end_lineno, indent, isinstance(last, ast.Return)
    raise AgentError(f"تابع «{hook}» در فایل مقصد پیدا نشد")


def feature_block(plan, feature: str, taken: set[str]) -> tuple[list[str], list[str], list[str]]:
    """Return (lines, added_names, notes). Clashing function names get a new name."""
    notes: list[str] = []
    mapping: dict[str, str] = {}
    bodies: list[str] = []
    for step in plan.steps:
        original = func_name(step["body"])
        if original not in mapping:
            chosen = _unique(original, taken | set(mapping.values()))
            mapping[original] = chosen
            if chosen != original:
                notes.append(f"نام «{original}» گرفته شده بود؛ «{chosen}» شد")
            bodies.append(_renamed_body(step["body"], original, chosen))
        step["func"] = mapping[original]

    entry = _unique(feature, taken | set(mapping.values()))
    if entry != feature:
        notes.append(f"نام ورودی «{feature}» گرفته شده بود؛ «{entry}» شد")

    block: list[str] = ["", "# --- افزودهٔ ایجنت (آغاز) ---"]
    for body in bodies:
        block.extend(body.splitlines())
        block.append("")
    block.append(f"def {entry}():")
    block.append('    """قابلیت افزوده‌شده توسط ایجنت."""')
    if plan.packages:
        block.extend([
            "    import sys as _agent_sys",
            "    from pathlib import Path as _agent_path",
            f'    _agent_lib = _agent_path(r"{LIB_DIR}")',
            "    if _agent_lib.is_dir() and str(_agent_lib) not in _agent_sys.path:",
            "        _agent_sys.path.insert(0, str(_agent_lib))",
        ])
    last = None
    for step in plan.steps:
        call = f"{step['func']}({', '.join(step['args'])})"
        if step["outputs"]:
            block.append(f"    {step['outputs'][0]} = {call}")
            last = step["outputs"][0]
        else:
            block.append(f"    {call}")
    if last:
        block.append(f"    return {last}")
    block.extend(["", "# --- افزودهٔ ایجنت (پایان) ---", ""])
    return block, list(mapping.values()) + [entry], notes


def _apply(lines: list[str], index: int, block: list[str], eol: str,
           inserted: set[int]) -> tuple[list[str], set[int]]:
    payload = [text + eol for text in block]
    if index >= len(lines) and lines and not lines[-1].endswith(("\n", "\r")):
        payload[0] = eol + payload[0]
    merged = lines[:index] + payload + lines[index:]
    shifted = {i + len(payload) if i >= index else i for i in inserted}
    shifted.update(range(index, index + len(payload)))
    return merged, shifted


def merge(target: str | Path, plan, feature: str = "agent_feature",
          hook: str | None = None, dry_run: bool = False) -> dict:
    path = assert_writable(Path(target))
    if not path.exists():
        raise AgentError(f"فایل مقصد پیدا نشد: {path}")
    if not plan.steps:
        raise AgentError("نقشه خالی است؛ چیزی برای افزودن نیست")

    raw = path.read_bytes()
    bom = raw.startswith(b"\xef\xbb\xbf")
    original = raw.decode("utf-8-sig")
    tree = _parse(original, "فایل مقصد")
    before = top_names(tree)
    lines = original.splitlines(keepends=True)
    eol = "\r\n" if "\r\n" in original else "\n"

    feature = safe_identifier(feature)
    block, added, notes = feature_block(plan, feature, before)
    entry = added[-1]
    at = _header_end(tree, lines)

    hook_at = hook_indent = None
    if hook:
        hook_at, hook_indent, after_return = _hook_point(tree, lines, hook)
        if after_return:
            notes.append(f"«{hook}» با return تمام می‌شود؛ فراخوانی جدید اجرا نخواهد شد")

    merged_lines, inserted = list(lines), set()
    if hook_at is not None:
        merged_lines, inserted = _apply(merged_lines, hook_at,
                                        [f"{hook_indent}{entry}()"], eol, inserted)
    merged_lines, inserted = _apply(merged_lines, at, block, eol, inserted)
    merged = "".join(merged_lines)

    recovered = "".join(line for i, line in enumerate(merged_lines) if i not in inserted)
    if recovered != original:
        raise AgentError("متن اصلی دقیقاً بازسازی نشد؛ فایل دست‌نخورده ماند")
    after_tree = _parse(merged, "نتیجهٔ ادغام")
    lost = sorted(before - top_names(after_tree))
    if lost:
        raise AgentError("نام‌های قبلی از بین رفتند: " + ", ".join(lost))

    report = {"target": str(path), "entry": entry, "added": added,
              "inserted_lines": len(inserted), "hook": hook or "",
              "notes": notes, "written": False, "backup": "", "check": "dry-run"}
    if dry_run:
        return report

    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = assert_writable(BACKUP_DIR / f"{path.stem}_{int(time.time())}{path.suffix}")
    shutil.copy2(path, backup)
    path.write_bytes((("\ufeff" if bom else "") + merged).encode("utf-8"))
    report.update(written=True, backup=str(backup), check="syntax-ok")
    return report


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain.integrate",
                                     description="افزودن قابلیت به فایل موجود")
    parser.add_argument("target")
    parser.add_argument("request")
    parser.add_argument("--feature", default="agent_feature")
    parser.add_argument("--hook", default=None, help="نام تابعی که فراخوانی به آن اضافه شود")
    parser.add_argument("--param", action="append", default=[], dest="params")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    params = {}
    for item in args.params:
        if "=" not in item:
            raise AgentError("--param باید name=value باشد")
        key, value = item.split("=", 1)
        params[key] = value

    db = capability.prepare()
    try:
        plan = build_plan(args.request, capability.all_caps(db), params)
        if plan.gaps:
            print("کمبود:", ", ".join(plan.gaps))
        for step in plan.steps:
            print("  •", step["key"], step["title"])
        report = merge(args.target, plan, args.feature, args.hook, args.dry_run)
        print("تابع ورودی:", report["entry"])
        print("خطوط درج‌شده:", report["inserted_lines"])
        for note in report["notes"]:
            print("  نکته:", note)
        print("وضعیت:", report["check"])
        if report["written"]:
            print("پشتیبان:", report["backup"])
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        stamp = assert_writable(REPORT_DIR / f"integrate_{int(time.time())}.json")
        stamp.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        return 0
    except (AgentError, OSError, ValueError) as exc:
        print("توقف:", exc)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())