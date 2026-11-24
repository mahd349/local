"""Discover the real corpus ingestion contract. Prints API, schema and CLI.

Read-only except one probe insert which is rolled back.
"""
from __future__ import annotations

import inspect
import os
import sqlite3
import sys

sys.path.insert(0, r"D:\Python")
sys.path.insert(0, r"D:\project\library")

from farsi import corpus


def section(title: str) -> None:
    print("\n" + "=" * 66)
    print(title)
    print("=" * 66)


def show_api() -> list[str]:
    section("PUBLIC NAMES IN farsi.corpus")
    names = [n for n in dir(corpus) if not n.startswith("_")]
    print(", ".join(names))

    candidates = []
    for name in names:
        obj = getattr(corpus, name)
        if not callable(obj):
            continue
        low = name.lower()
        if any(k in low for k in ("add", "ingest", "store", "put", "save", "absorb", "learn")):
            candidates.append(name)
            try:
                print(f"  {name}{inspect.signature(obj)}")
            except (TypeError, ValueError):
                print(f"  {name}(?)")
    return candidates


def show_cli() -> None:
    section("CLI SUBCOMMANDS (corpus.main --help)")
    if not hasattr(corpus, "main"):
        print("corpus.main not found")
        return
    try:
        corpus.main(["--help"])
    except SystemExit:
        pass
    except Exception as exc:
        print(f"help failed: {exc}")


def show_source(candidates: list[str]) -> None:
    section("SOURCE OF main() AND ADD-LIKE FUNCTIONS (first 90 lines each)")
    targets = ["main"] + candidates
    for name in targets:
        obj = getattr(corpus, name, None)
        if obj is None or not callable(obj):
            continue
        print(f"\n----- {name} -----")
        try:
            lines = inspect.getsource(obj).splitlines()
        except (OSError, TypeError) as exc:
            print(f"  source unavailable: {exc}")
            continue
        for line in lines[:90]:
            print(line)
        if len(lines) > 90:
            print(f"  ... ({len(lines) - 90} more lines)")


def show_schema() -> None:
    section("docs TABLE SCHEMA")
    db = corpus.connect()
    try:
        print("learning_enabled:", corpus.learning_enabled())
        cols = db.execute("PRAGMA table_info(docs)").fetchall()
        print(f"\n{'cid':<4}{'name':<18}{'type':<12}{'notnull':<9}{'default':<14}{'pk'}")
        for c in cols:
            d = dict(c)
            print(f"{d['cid']:<4}{d['name']:<18}{str(d['type']):<12}"
                  f"{d['notnull']:<9}{str(d['dflt_value']):<14}{d['pk']}")

        print("\nINDEXES:")
        for r in db.execute("PRAGMA index_list(docs)").fetchall():
            print(" ", dict(r))

        n = db.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
        mx = db.execute("SELECT MAX(id) AS m FROM docs").fetchone()["m"]
        print(f"\nrows={n}  max_id={mx}")

        last = db.execute("SELECT * FROM docs ORDER BY id DESC LIMIT 1").fetchone()
        if last:
            print("\nLAST ROW (values truncated to 90 chars):")
            for k in dict(last).keys():
                v = str(dict(last)[k])
                print(f"  {k} = {v[:90]}")
    finally:
        db.close()


def probe(candidates: list[str]) -> None:
    section("PROBE: try each add-like function with a tiny text (rolled back)")
    text = "ایران کشوری در آسیا است. تهران پایتخت ایران است."
    for name in candidates:
        fn = getattr(corpus, name)
        db = corpus.connect()
        try:
            before = db.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
        except sqlite3.Error as exc:
            print(f"{name}: cannot count docs: {exc}")
            db.close()
            continue

        attempts = [(text,), (db, text), (text, "probe")]
        print(f"\n--- {name} ---")
        for args in attempts:
            try:
                result = fn(*args)
            except TypeError as exc:
                print(f"  args={len(args)} TypeError: {exc}")
                continue
            except Exception as exc:
                print(f"  args={len(args)} {type(exc).__name__}: {exc}")
                break
            after = db.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
            print(f"  args={len(args)} OK -> {result!r}  rows {before}->{after}")
            break
        db.rollback()
        db.close()


def main() -> int:
    candidates = show_api()
    show_cli()
    show_schema()
    probe(candidates)
    show_source(candidates)
    print("\nDONE. Send this whole output back.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())