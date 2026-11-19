"""Turn verified knowledge units into capability cards whose types are proven by execution.

A unit is promoted only when a probe really calls it with sample values and the
observed return type is usable. Units needing outside context, third-party
packages or blocked operations are skipped instead of guessed.
"""
from __future__ import annotations

import argparse
import ast
import json
import textwrap
import time
from pathlib import Path

from agent.config import REPORT_DIR, assert_writable, get_logger
from agent.errors import AgentError

from . import capability, knowledge, probe, verify

log = get_logger("bridge")

SAMPLES = {"str": "sample", "path": "probe.txt", "url": "https://example.com"}
RETURN_TYPES = {"str": "str", "int": "int", "float": "float", "bool": "bool",
                "list": "list", "dict": "dict", "NoneType": ""}
SKIP_TAGS = {"gui", "http", "process", "threading", "async"}
_URL_WORDS = ("url", "link", "uri", "href")
_PATH_WORDS = ("path", "file", "fname", "dir", "folder", "dest", "src")
_TEXT_WORDS = ("text", "msg", "message", "content", "body", "name", "title", "value")


def _guess(name: str) -> str | None:
    lowered = name.lower()
    if any(word in lowered for word in _URL_WORDS):
        return "url"
    if any(word in lowered for word in _PATH_WORDS):
        return "path"
    if lowered in ("s", "t") or any(word in lowered for word in _TEXT_WORDS):
        return "str"
    return None


def _function(source: str):
    try:
        tree = ast.parse(textwrap.dedent(source))
    except (SyntaxError, ValueError):
        return None
    if len(tree.body) != 1 or not isinstance(tree.body[0], ast.FunctionDef):
        return None
    node = tree.body[0]
    if node.decorator_list or node.args.vararg or node.args.kwarg or node.args.posonlyargs:
        return None
    if not any(isinstance(child, ast.Return) and child.value is not None
               for child in ast.walk(node)):
        return None
    return node


def _params(node) -> list[dict] | None:
    out = []
    for arg in node.args.args:
        kind = _guess(arg.arg)
        if kind is None:
            return None
        out.append({"name": arg.arg, "type": kind})
    return out


_SAFE_CALLS = {
    "re.compile", "frozenset", "set", "tuple", "list", "dict", "Path",
    "os.path.join", "os.path.abspath", "collections.OrderedDict",
}


def _dotted_name(node) -> str:
    parts: list[str] = []
    while isinstance(node, ast.Attribute):
        parts.append(node.attr)
        node = node.value
    if isinstance(node, ast.Name):
        parts.append(node.id)
        return ".".join(reversed(parts))
    return ""


def _pure(node) -> bool:
    """A module-level value is reusable context only if it has no side effects."""
    if isinstance(node, ast.Constant):
        return True
    if isinstance(node, (ast.Name, ast.Attribute)):
        return True
    if isinstance(node, (ast.List, ast.Tuple, ast.Set)):
        return all(_pure(item) for item in node.elts)
    if isinstance(node, ast.Dict):
        if any(key is None for key in node.keys):
            return False
        return all(_pure(k) for k in node.keys) and all(_pure(v) for v in node.values)
    if isinstance(node, ast.BinOp):
        return _pure(node.left) and _pure(node.right)
    if isinstance(node, ast.UnaryOp):
        return _pure(node.operand)
    if isinstance(node, ast.Call):
        return (_dotted_name(node.func) in _SAFE_CALLS
                and all(_pure(a) for a in node.args)
                and all(_pure(k.value) for k in node.keywords))
    return False


def _names_in(node) -> set[str]:
    return {item.id for item in ast.walk(node) if isinstance(item, ast.Name)}


def _unit_names(source: str) -> set[str]:
    try:
        return _names_in(ast.parse(textwrap.dedent(source or "")))
    except (SyntaxError, ValueError):
        return set()


def context_of(source: str, needed: set[str] | None = None) -> str:
    """Only the imports and side-effect-free assignments a unit actually needs.

    A module that imports tkinter at the top must not force tkinter into the
    probe for a plain string helper, so unused names are dropped. Dependencies
    of a kept constant are pulled in transitively.
    """
    import builtins
    import sys

    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return ""
    stdlib = sys.stdlib_module_names
    entries: list[tuple] = []
    for order, node in enumerate(tree.body):
        if isinstance(node, ast.Import):
            roots = [alias.name.split(".")[0] for alias in node.names]
            if not all(root in stdlib for root in roots):
                continue
            bound = [alias.asname or alias.name.split(".")[0] for alias in node.names]
            entries.append((order, "import", bound, None,
                            ast.get_source_segment(source, node)))
        elif isinstance(node, ast.ImportFrom) and node.level == 0:
            if (node.module or "").split(".")[0] not in stdlib:
                continue
            bound = [alias.asname or alias.name for alias in node.names]
            entries.append((order, "import", bound, None,
                            ast.get_source_segment(source, node)))
        elif isinstance(node, (ast.Assign, ast.AnnAssign)) and node.value is not None:
            if not _pure(node.value):
                continue
            targets = node.targets if isinstance(node, ast.Assign) else [node.target]
            bound = [t.id for t in targets if isinstance(t, ast.Name)]
            if not bound:
                continue
            entries.append((order, "assign", bound, node.value,
                            ast.get_source_segment(source, node)))
    entries = [item for item in entries if item[4]]

    if needed is None:
        required = {name for item in entries for name in item[2]}
    else:
        required = set(needed)
    while True:
        before = len(required)
        for item in entries:
            if item[1] == "assign" and any(n in required for n in item[2]):
                required |= _names_in(item[3])
        if len(required) == before:
            break

    known = set(dir(builtins))
    chosen: list[tuple] = []
    pending = list(entries)
    while pending:
        rest, progress = [], False
        for item in pending:
            order, kind, bound, value, segment = item
            if not any(name in required for name in bound):
                continue
            if kind == "import":
                chosen.append((order, segment))
                known.update(bound)
                progress = True
            elif _names_in(value) <= known:
                chosen.append((order, segment))
                known.update(bound)
                progress = True
            else:
                rest.append(item)
        pending = rest
        if not progress:
            break
    chosen.sort(key=lambda pair: pair[0])
    return "\n".join(segment for _, segment in chosen)


def _prelude(unit: dict) -> str:
    path = Path(str(unit.get("origin") or ""))
    if not path.is_absolute() or path.suffix.lower() != ".py" or not path.exists():
        return ""
    try:
        source = path.read_text(encoding="utf-8-sig", errors="replace")
    except OSError:
        return ""
    return context_of(source, _unit_names(unit.get("source") or ""))

def _renamed(source: str, old: str, new: str) -> str:
    tree = ast.parse(textwrap.dedent(source))
    for node in ast.walk(tree):
        if isinstance(node, ast.FunctionDef) and node.name == old:
            node.name = new
        elif isinstance(node, ast.Name) and node.id == old:
            node.id = new
    return ast.unparse(tree)


def _probe_source(prelude: str, body: str, name: str, params: list[dict]) -> str:
    args = ", ".join(repr(SAMPLES[item["type"]]) for item in params)
    return (prelude.strip() + "\n\n" + body + "\n\n"
            + "import json as _json\n"
            + f"_result = {name}({args})\n"
            + "print(_json.dumps({'kind': type(_result).__name__}))\n")


def _observe(report: dict) -> str | None:
    for line in reversed((report.get("stdout") or "").splitlines()):
        line = line.strip()
        if line.startswith("{") and line.endswith("}"):
            try:
                kind = json.loads(line).get("kind")
            except ValueError:
                continue
            if kind in RETURN_TYPES:
                return RETURN_TYPES[kind]
    return None


def prepare_card(unit: dict, *, budget=None, timeout=None,
                 run_probe: bool = True, stats: dict | None = None) -> dict | None:
    if unit.get("kind") != "function" or unit.get("flags"):
        return None
    if str(unit.get("name") or "").startswith("cap_"):
        return None  # already a capability card, not new knowledge
    tags = unit.get("tags") or []
    if SKIP_TAGS & set(tags):
        return None
    node = _function(unit.get("source") or "")
    if node is None:
        return None
    params = _params(node)
    if params is None:
        return None
    new_name = "cap_k_" + unit["signature"]
    try:
        body = _renamed(unit["source"], node.name, new_name)
    except (SyntaxError, ValueError):
        return None

    card = {
        "key": "k." + unit["signature"],
        "title": f"الگوی آموخته‌شده: {unit['name']}",
        # A precise token: these cards enter through type wiring, not keywords.
        "goals": ["k." + unit["signature"]],
        "inputs": params,
        "outputs": [],
        "packages": [],
        "effects": ["file-io"] if "file-io" in tags else [],
        "context": [], "exclude": [],
        "priority": 20,
        "origin": "knowledge:" + unit["signature"],
        "body": body,
    }
    if not run_probe:
        return card

    source = _probe_source(_prelude(unit), body, new_name, params)
    try:
        report = probe.run_snippet(source, timeout=timeout, budget=budget, label="bridge")
    except AgentError:
        return None
    if stats is not None:
        stats["probed"] += 1
    if report["status"] != "ok":
        return None
    observed = _observe(report)
    if observed is None:
        return None
    card["outputs"] = [{"name": "result", "type": observed}] if observed else []
    return card


def suggest(kdb, *, limit: int = 20) -> list[dict]:
    out = []
    for unit in knowledge.find(kdb, limit=max(1, limit) * 8, verified_only=True):
        card = prepare_card(unit, run_probe=False)
        if card is None:
            continue
        out.append({"key": card["key"], "name": unit["name"],
                    "inputs": [item["type"] for item in card["inputs"]],
                    "origin": unit.get("origin", "")})
        if len(out) >= limit:
            break
    return out


def promote(db, kdb, *, limit: int = 10, budget=None, timeout=None) -> dict:
    stats = {"seen": 0, "probed": 0, "promoted": 0}
    for unit in knowledge.find(kdb, limit=max(1, limit) * 8, verified_only=True):
        if stats["promoted"] >= limit:
            break
        if budget is not None and (budget.runs >= budget.max_runs
                                   or budget.seconds >= budget.max_seconds):
            break
        stats["seen"] += 1
        card = prepare_card(unit, budget=budget, timeout=timeout, stats=stats)
        if card is None:
            continue
        capability.upsert(db, card)
        capability.record(db, card["key"], True)
        stats["promoted"] += 1
    return stats


def stats(db) -> dict:
    cards = capability.all_caps(db)
    learned = [card for card in cards if card["key"].startswith("k.")]
    return {"cards": len(cards), "learned": len(learned),
            "verified": sum(1 for card in learned if card["verified"])}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain.bridge", description="دانش → قابلیت")
    sub = parser.add_subparsers(dest="command", required=True)
    show = sub.add_parser("suggest", help="پیش‌نمایش بدون اجرا")
    show.add_argument("--limit", type=int, default=20)
    up = sub.add_parser("promote", help="اثبات با اجرا و ثبت کارت")
    up.add_argument("--limit", type=int, default=10)
    up.add_argument("--max-runs", type=int, default=40)
    up.add_argument("--max-seconds", type=float, default=120.0)
    sub.add_parser("stats", help="شمار کارت‌های آموخته‌شده")
    args = parser.parse_args(argv)

    kdb = knowledge.connect()
    db = capability.prepare()
    try:
        if args.command == "suggest":
            rows = suggest(kdb, limit=args.limit)
            for row in rows:
                print(f"{row['name']:24} ورودی: {', '.join(row['inputs']) or '-'}")
            print(f"\n{len(rows)} کاندید (بدون اجرا)")
            return 0
        if args.command == "promote":
            budget = probe.Budget(args.max_runs, args.max_seconds)
            report = promote(db, kdb, limit=args.limit, budget=budget)
            print(f"بررسی‌شده: {report['seen']} | اجرا: {report['probed']} "
                  f"| ارتقا: {report['promoted']}")
            REPORT_DIR.mkdir(parents=True, exist_ok=True)
            target = assert_writable(REPORT_DIR / f"bridge_{int(time.time())}.json")
            target.write_text(json.dumps({"report": report, "budget": budget.as_dict()},
                                         ensure_ascii=False, indent=2), encoding="utf-8")
            print("گزارش:", target)
            return 0
        info = stats(db)
        print(f"کارت‌ها: {info['cards']} | آموخته‌شده: {info['learned']} "
              f"| تأییدشده: {info['verified']}")
        return 0
    except (AgentError, OSError, ValueError) as exc:
        print("توقف:", exc)
        return 1
    finally:
        db.close()
        kdb.close()


if __name__ == "__main__":
    raise SystemExit(main())