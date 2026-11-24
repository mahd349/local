"""Validate a learned unit: run it, test it, rename it, mutate it, then weight it.

The public interface of a unit is preserved during renaming, so the same tests
stay valid. Mutation score is advisory: it shows how much the tests really catch.
"""
from __future__ import annotations

import argparse
import ast
import builtins
import copy
import json
import textwrap
import time
from pathlib import Path

from . import knowledge, probe, pystruct
from agent.config import REPORT_DIR, assert_writable, get_logger
from agent.errors import AgentError

log = get_logger("verify")

_SWAP_CMP = {ast.Lt: ast.LtE, ast.LtE: ast.Lt, ast.Gt: ast.GtE,
             ast.GtE: ast.Gt, ast.Eq: ast.NotEq, ast.NotEq: ast.Eq}
_SWAP_BIN = {ast.Add: ast.Sub, ast.Sub: ast.Add, ast.Mult: ast.Div, ast.Div: ast.Mult}


def imports_of(source: str) -> str:
    """Top-level import lines, used as a prelude for standalone runs."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return ""
    lines = []
    for node in tree.body:
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            segment = ast.get_source_segment(source, node)
            if segment:
                lines.append(segment)
    return "\n".join(lines)


def build_source(prelude: str, unit_source: str, test_code: str = "") -> str:
    parts = []
    for chunk in (prelude, unit_source, test_code):
        text = textwrap.dedent(str(chunk or "")).strip("\n")
        if text.strip():
            parts.append(text)
    return "\n\n".join(parts) + "\n"


def _imported_names(tree: ast.AST) -> set[str]:
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                names.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                names.add(alias.asname or alias.name)
    return names


def _arg_items(arguments: ast.arguments) -> list:
    items = list(arguments.posonlyargs) + list(arguments.args) + list(arguments.kwonlyargs)
    if arguments.vararg:
        items.append(arguments.vararg)
    if arguments.kwarg:
        items.append(arguments.kwarg)
    return items


class _Rename(ast.NodeTransformer):
    def __init__(self, keep: set[str]):
        self.keep = keep
        self.map: dict[str, str] = {}

    def _alias(self, name: str) -> str:
        if name in self.keep or name.startswith("__"):
            return name
        return self.map.setdefault(name, f"v{len(self.map) + 1}")

    def visit_Name(self, node):
        node.id = self._alias(node.id)
        return node

    def visit_arg(self, node):
        node.arg = self._alias(node.arg)
        node.annotation = None
        return node

    def visit_FunctionDef(self, node):
        node.name = self._alias(node.name)
        node.returns = None
        node.type_comment = None
        self.generic_visit(node)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        node.name = self._alias(node.name)
        self.generic_visit(node)
        return node

    def visit_Global(self, node):
        node.names = [self._alias(item) for item in node.names]
        return node

    visit_Nonlocal = visit_Global


def rename_locals(source: str) -> str:
    """Rename internal names only; the public name and parameters stay intact."""
    try:
        tree = ast.parse(textwrap.dedent(source))
    except (SyntaxError, ValueError):
        return ""
    keep = set(dir(builtins)) | _imported_names(tree)
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            keep.add(node.name)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                keep.update(item.arg for item in _arg_items(node.args))
    renamed = _Rename(keep).visit(tree)
    try:
        return ast.unparse(renamed)
    except (ValueError, TypeError):
        return ""


def _eligible(node: ast.AST) -> bool:
    if isinstance(node, ast.Compare):
        return len(node.ops) == 1 and type(node.ops[0]) in _SWAP_CMP
    if isinstance(node, ast.BinOp):
        return type(node.op) in _SWAP_BIN
    if isinstance(node, ast.Constant):
        return isinstance(node.value, (bool, int))
    return False


def _apply(node: ast.AST) -> None:
    if isinstance(node, ast.Compare):
        node.ops = [_SWAP_CMP[type(node.ops[0])]()]
    elif isinstance(node, ast.BinOp):
        node.op = _SWAP_BIN[type(node.op)]()
    elif isinstance(node, ast.Constant):
        node.value = (not node.value) if isinstance(node.value, bool) else node.value + 1


def mutants(source: str, limit: int = 6) -> list[tuple[str, str]]:
    """Small, still-valid variants used to measure how strong the tests are."""
    try:
        tree = ast.parse(textwrap.dedent(source))
    except (SyntaxError, ValueError):
        return []
    total = sum(1 for node in ast.walk(tree) if _eligible(node))
    wanted = min(total, max(0, int(limit)))
    out: list[tuple[str, str]] = []
    for index in range(wanted):
        clone = copy.deepcopy(tree)
        seen = 0
        label = f"mut{index + 1}"
        for node in ast.walk(clone):
            if not _eligible(node):
                continue
            if seen == index:
                label = f"mut{index + 1}:{type(node).__name__}"
                _apply(node)
                break
            seen += 1
        try:
            out.append((label, ast.unparse(clone)))
        except (ValueError, TypeError):
            continue
    return out


def _run(source: str, budget, timeout, label):
    try:
        return probe.run_snippet(source, timeout=timeout, budget=budget, label=label)
    except AgentError:
        return None


def _finish(db, result: dict, started: float) -> dict:
    result["duration"] = round(time.time() - started, 3)
    if result["status"] in ("ok", "error"):
        detail = json.dumps({
            "tests": len(result["tests"]),
            "metamorphic": result["metamorphic"],
            "mutation": result["mutation"],
            "mutants": result["mutants"],
            "confidence": result["confidence"],
            "runs": result["runs"],
        }, ensure_ascii=False)
        try:
            knowledge.record_outcome(db, result["signature"], result["status"],
                                     detail, result["duration"])
        except KeyError:
            result["detail"] = (result["detail"] + " | signature not in store").strip()
    return result


def verify_unit(db, unit: dict, tests: list[str], *, prelude: str = "",
                budget: probe.Budget | None = None, mutation_limit: int = 6,
                timeout: float | None = None) -> dict:
    if not probe.RUNNER.exists():
        raise AgentError(f"فایل اجراکننده پیدا نشد: {probe.RUNNER}")

    source = unit["source"]
    tests = [str(item) for item in (tests or []) if str(item).strip()]
    started = time.time()
    result = {
        "name": unit.get("name", "<unit>"), "signature": unit["signature"],
        "smoke": "", "tests": [], "metamorphic": None, "mutation": None,
        "mutants": 0, "killed": 0, "status": "error", "confidence": "low",
        "partial": False, "runs": 0, "detail": "",
    }

    smoke = _run(build_source(prelude, source), budget, timeout, "smoke")
    if smoke is None:
        result["partial"] = True
        result["status"] = "partial"
        return _finish(db, result, started)
    result["runs"] += 1
    result["smoke"] = smoke["status"]
    if smoke["status"] != "ok":
        result["detail"] = (smoke["detail"] or smoke["stderr"])[-300:]
        return _finish(db, result, started)

    baseline: list[bool] = []
    for index, test in enumerate(tests, start=1):
        report = _run(build_source(prelude, source, test), budget, timeout, f"t{index}")
        if report is None:
            result["partial"] = True
            break
        result["runs"] += 1
        passed = report["status"] == "ok"
        baseline.append(passed)
        result["tests"].append({"index": index, "status": report["status"],
                                "detail": (report["detail"] or "")[:160]})

    complete = len(baseline) == len(tests)

    if tests and complete and not result["partial"]:
        renamed = rename_locals(source)
        if renamed:
            same = True
            for index, test in enumerate(tests, start=1):
                report = _run(build_source(prelude, renamed, test), budget, timeout, f"m{index}")
                if report is None:
                    result["partial"] = True
                    same = None
                    break
                result["runs"] += 1
                if (report["status"] == "ok") != baseline[index - 1]:
                    same = False
            result["metamorphic"] = same

    if tests and complete and all(baseline) and not result["partial"] and mutation_limit > 0:
        killed = 0
        for _label, variant in mutants(source, mutation_limit):
            caught = False
            for test in tests:
                report = _run(build_source(prelude, variant, test), budget, timeout, "mut")
                if report is None:
                    result["partial"] = True
                    break
                result["runs"] += 1
                if report["status"] != "ok":
                    caught = True
                    break
            if result["partial"]:
                break
            result["mutants"] += 1
            killed += 1 if caught else 0
        result["killed"] = killed
        if result["mutants"]:
            result["mutation"] = round(killed / result["mutants"], 2)

    passed_all = (not tests) or (complete and all(baseline))
    ok = result["smoke"] == "ok" and passed_all and result["metamorphic"] is not False

    if result["partial"]:
        result["status"] = "partial"
    elif ok:
        result["status"] = "ok"
        if tests and result["metamorphic"] and (result["mutation"] or 0) >= 0.6:
            result["confidence"] = "high"
        elif tests:
            result["confidence"] = "medium"
    else:
        result["status"] = "error"
        failed = next((t for t in result["tests"] if t["status"] != "ok"), None)
        if failed:
            result["detail"] = failed["detail"]
    return _finish(db, result, started)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.verify", description="اعتبارسنجی الگوها")
    sub = parser.add_subparsers(dest="command", required=True)
    file_parser = sub.add_parser("file", help="یادگیری و اعتبارسنجی واحدهای یک فایل")
    file_parser.add_argument("path")
    file_parser.add_argument("--tests", default=None, help="JSON: نام واحد -> فهرست آزمون")
    file_parser.add_argument("--mutants", type=int, default=6)
    file_parser.add_argument("--timeout", type=float, default=None)
    file_parser.add_argument("--max-runs", type=int, default=40)
    file_parser.add_argument("--max-seconds", type=float, default=90.0)
    sub.add_parser("stats", help="اندازه و شمار الگوهای ذخیره‌شده")
    args = parser.parse_args(argv)

    db = knowledge.connect()
    try:
        if args.command == "stats":
            info = knowledge.stats(db)
            size = knowledge.KNOWLEDGE_DB.stat().st_size if knowledge.KNOWLEDGE_DB.exists() else 0
            print(f"الگوها: {info['units']} | تأییدشده: {info['verified']} | میانگین وزن: {info['avg_weight']}")
            print(f"حجم پایگاه دانش: {size / 1048576:.1f} MB")
            return 0

        path = Path(args.path)
        if not path.exists():
            raise AgentError(f"فایل پیدا نشد: {path}")
        source = path.read_text(encoding="utf-8-sig", errors="replace")
        prelude = imports_of(source)
        units = pystruct.parse(source)
        if not units:
            print("واحد قابل بررسی پیدا نشد (کد خالی یا نامعتبر).")
            return 1

        test_map: dict = {}
        if args.tests:
            test_map = json.loads(Path(args.tests).read_text(encoding="utf-8-sig"))

        budget = probe.Budget(args.max_runs, args.max_seconds)
        reports = []
        for node in units:
            unit = node.as_dict()
            knowledge.upsert_unit(db, unit, "python", str(path))
            item = verify_unit(db, unit, test_map.get(unit["name"], []),
                               prelude=prelude, budget=budget,
                               mutation_limit=args.mutants, timeout=args.timeout)
            reports.append(item)
            print(f"{item['status']:8} {unit['kind']:9} {unit['name']}"
                  f"  tests={len(item['tests'])} mutation={item['mutation']}"
                  f" confidence={item['confidence']}")
            if budget.runs >= budget.max_runs or budget.seconds >= budget.max_seconds:
                print("سهمیهٔ نشست تمام شد؛ بقیه در نشست بعد.")
                break

        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        target = assert_writable(REPORT_DIR / f"verify_{int(time.time())}.json")
        target.write_text(json.dumps({"file": str(path), "budget": budget.as_dict(),
                                      "units": reports}, ensure_ascii=False, indent=2),
                          encoding="utf-8")
        ok = sum(1 for item in reports if item["status"] == "ok")
        print(f"\nنتیجه: {ok} از {len(reports)} تأیید شد | اجراها: {budget.runs}"
              f" | زمان: {budget.seconds:.1f}s")
        print("گزارش:", target)
        return 0
    except (AgentError, OSError, ValueError) as exc:
        print("توقف:", exc)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())