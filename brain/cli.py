"""Single entry point for the learning subsystem: python -m brain <command>."""
from __future__ import annotations

import argparse
import importlib

MODULES = {
    "ingest": "ingest", "verify": "verify", "capability": "capability",
    "planner": "planner", "assembler": "assembler", "integrate": "integrate",
    "bridge": "bridge",
    "knowtest": "knowtest", "verifytest": "verifytest", "plantest": "plantest",
    "integratetest": "integratetest", "ingesttest": "ingesttest",
    "bridgetest": "bridgetest",
    "lesson": "lesson", "lessontest": "lessontest",
    "naming": "naming", "namingtest": "namingtest",
    "digest": "digest", "digesttest": "digesttest",
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="brain", description="هستهٔ یادگیری پایتون")
    parser.add_argument("command", choices=sorted(MODULES) + ["list"])
    parser.add_argument("rest", nargs=argparse.REMAINDER)
    args = parser.parse_args(argv)
    if args.command == "list":
        for name in sorted(MODULES):
            print(name)
        return 0
    try:
        module = importlib.import_module("." + MODULES[args.command], package=__package__)
    except ImportError as exc:
        print(f"ماژول {args.command} در دسترس نیست: {exc}")
        return 1
    entry = getattr(module, "main", None)
    if entry is not None:
        return int(entry(args.rest) or 0)
    entry = getattr(module, "run", None)
    if entry is None:
        print("این ماژول نقطهٔ اجرا ندارد:", args.command)
        return 1
    return int(entry() or 0)