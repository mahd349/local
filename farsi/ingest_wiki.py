"""Stream Wikipedia paragraphs into corpus.docs with template-diversity control.

Schema is known: id, hash, source, speaker, ts, raw, keys, quality, weight.
hash is UNIQUE, so dedup happens at two levels: hashes already in the table,
and hashes produced during this run. INSERT OR IGNORE is the last guard.

The source is bot-generated stubs. Taking the first N paragraphs would train
the model on one template forever, so paragraphs sharing a function-word
skeleton are capped, and the whole file is streamed instead of sliced.
"""
from __future__ import annotations

import argparse
import hashlib
import os
import sqlite3
import sys
import time

sys.path.insert(0, r"D:\Python")
sys.path.insert(0, r"D:\project\library")

from farsi import corpus
from farsi.text import persian_ratio, quality, tokens

BLOCK = 1 << 20
BATCH = 2000
SIG_LIMIT = 400_000

TS_NAMES = {"ts", "time", "created", "created_ts", "added_ts",
            "updated_ts", "ingested_ts"}
HASH_NAMES = {"hash", "sha1", "sha", "digest", "content_hash", "doc_hash"}
TEXT_NAMES = {"raw": "raw", "text": "raw", "body": "raw", "content": "raw",
              "keys": "keys", "tokens": "keys",
              "source": "source", "src": "source", "origin": "source",
              "path": "source", "file": "source", "filename": "source",
              "speaker": "speaker", "lang": "lang", "language": "lang",
              "title": "title", "name": "title"}
INT_NAMES = {"tokens": "n_tokens", "tokens_n": "n_tokens",
             "n_tokens": "n_tokens", "length": "n_tokens", "len": "n_tokens",
             "size": "n_tokens", "chars": "n_chars", "n_chars": "n_chars"}
REAL_NAMES = {"quality": "quality", "weight": "weight", "score": "quality"}


def paragraphs(path: str):
    buffer = ""
    with open(path, "r", encoding="utf-8", errors="replace") as handle:
        while True:
            block = handle.read(BLOCK)
            if not block:
                break
            buffer += block
            parts = buffer.split("\n\n")
            buffer = parts.pop()
            for part in parts:
                text = part.strip()
                if text:
                    yield text
    tail = buffer.strip()
    if tail:
        yield tail


def skeleton(keys: list[str]) -> str:
    return " ".join(k if len(k) <= 3 else "#" for k in keys)


def plan(db: sqlite3.Connection) -> tuple[list[str], list[str], str | None]:
    cols = [dict(r) for r in db.execute("PRAGMA table_info(docs)").fetchall()]
    names: list[str] = []
    kinds: list[str] = []
    blocking: list[str] = []
    hash_col: str | None = None

    for col in cols:
        name = str(col["name"])
        low = name.lower()
        ctype = str(col["type"] or "").upper()
        is_pk = int(col["pk"] or 0) > 0
        required = int(col["notnull"] or 0) > 0 and col["dflt_value"] is None

        if low == "id" and is_pk:
            continue

        if low in TS_NAMES:
            kind = "now"
        elif low in HASH_NAMES:
            kind = "hash"
            hash_col = name
        elif "INT" in ctype:
            kind = INT_NAMES.get(low)
        elif any(t in ctype for t in ("REAL", "FLOA", "DOUB")):
            kind = REAL_NAMES.get(low) or ("now" if low in TS_NAMES else None)
        else:
            kind = TEXT_NAMES.get(low)

        if kind is None:
            if required:
                blocking.append(f"{name} ({ctype or 'ANY'})")
            continue
        names.append(name)
        kinds.append(kind)

    if blocking:
        return [], [], None
    return names, kinds, hash_col


def values_for(kinds: list[str], raw: str, keys_str: str, n_tokens: int,
               qual: float, digest: str, source: str, now: float) -> tuple:
    out = []
    for kind in kinds:
        if kind == "raw":
            out.append(raw)
        elif kind == "keys":
            out.append(keys_str)
        elif kind == "n_tokens":
            out.append(n_tokens)
        elif kind == "n_chars":
            out.append(len(raw))
        elif kind == "quality":
            out.append(qual)
        elif kind == "weight":
            out.append(1.0)
        elif kind == "hash":
            out.append(digest)
        elif kind == "source":
            out.append(source)
        elif kind == "speaker":
            out.append("")
        elif kind == "lang":
            out.append("fa")
        elif kind == "title":
            out.append("")
        elif kind == "now":
            out.append(now)
        else:
            raise RuntimeError(f"unhandled kind: {kind}")
    return tuple(out)


def ingest(path: str, target: int, per_sig: int, min_quality: float,
           min_persian: float, min_tokens: int, dry_run: bool) -> dict:
    if not os.path.exists(path):
        raise SystemExit(f"file not found: {path}")

    db = corpus.connect()
    db.execute("PRAGMA journal_mode=WAL")
    db.execute("PRAGMA synchronous=NORMAL")

    names, kinds, hash_col = plan(db)
    if not names:
        db.close()
        raise SystemExit("ABORT: unmappable required columns: " + ", ".join(kinds))

    print("insert columns:", ", ".join(names))
    print("hash column:", hash_col or "(none)")

    seen: set[str] = set()
    if hash_col:
        seen.update(str(r[0]) for r in
                    db.execute(f"SELECT {hash_col} FROM docs").fetchall()
                    if r[0] is not None)
    print(f"hashes already present: {len(seen):,}")

    rows_before = db.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
    sql = (f"INSERT OR IGNORE INTO docs({','.join(names)}) "
           f"VALUES({','.join('?' for _ in names)})")

    sig_counts: dict[str, int] = {}
    batch: list[tuple] = []
    source = os.path.abspath(path)
    now = time.time()
    started = now
    changes_before = db.total_changes

    scanned = kept = 0
    skip_short = skip_lang = skip_qual = skip_sig = skip_dup = 0

    def flush() -> None:
        nonlocal batch
        if batch and not dry_run:
            db.executemany(sql, batch)
            db.commit()
        batch = []

    try:
        for raw in paragraphs(path):
            scanned += 1
            if kept >= target:
                break

            keys = tokens(raw)
            if len(keys) < min_tokens:
                skip_short += 1
                continue
            if persian_ratio(raw) < min_persian:
                skip_lang += 1
                continue
            qual = float(quality(raw))
            if qual < min_quality:
                skip_qual += 1
                continue

            keys_str = " ".join(keys)
            digest = hashlib.sha1(keys_str.encode("utf-8")).hexdigest()
            if digest in seen:
                skip_dup += 1
                continue

            sig = skeleton(keys)
            if len(sig_counts) < SIG_LIMIT or sig in sig_counts:
                count = sig_counts.get(sig, 0)
                if count >= per_sig:
                    skip_sig += 1
                    continue
                sig_counts[sig] = count + 1

            seen.add(digest)
            batch.append(values_for(kinds, raw, keys_str, len(keys), qual,
                                    digest, source, now))
            kept += 1

            if len(batch) >= BATCH:
                flush()
                if kept % 20000 == 0:
                    rate = kept / max(1e-9, time.time() - started)
                    print(f"  kept={kept:,} scanned={scanned:,} "
                          f"templates={len(sig_counts):,} {rate:.0f} rows/s")
        flush()
    except sqlite3.Error as exc:
        db.rollback()
        db.close()
        raise SystemExit(f"SQLite error, rolled back: {exc}")

    inserted = db.total_changes - changes_before
    rows_after = db.execute("SELECT COUNT(*) AS n FROM docs").fetchone()["n"]
    max_id = db.execute("SELECT MAX(id) AS m FROM docs").fetchone()["m"]
    sample = db.execute("SELECT id, ts, quality, raw FROM docs "
                        "ORDER BY id DESC LIMIT 1").fetchone()
    db.close()

    print("\n" + "=" * 66)
    print(f"scanned              {scanned:,}")
    print(f"kept                 {kept:,}")
    print(f"actually inserted    {inserted:,}")
    print(f"rows {rows_before:,} -> {rows_after:,}   max_id={max_id}")
    print(f"unique templates     {len(sig_counts):,}")
    print(f"skip: short={skip_short:,} lang={skip_lang:,} "
          f"quality={skip_qual:,} template_cap={skip_sig:,} dup={skip_dup:,}")
    print(f"seconds              {round(time.time() - started, 2)}")
    if sample:
        print(f"last row id={sample['id']} ts={sample['ts']} "
              f"q={sample['quality']}")
        print(f"  {str(sample['raw'])[:90]}")
    print("=" * 66)

    if inserted == 0 and not dry_run:
        print("WARNING: nothing inserted. Everything was a duplicate or filtered.")
    elif not dry_run:
        print("Next: python farsi\\build_all.py --budget 900")
    return {"inserted": inserted, "max_id": max_id}


def main() -> int:
    p = argparse.ArgumentParser(prog="ingest_wiki")
    p.add_argument("path")
    p.add_argument("--target", type=int, default=200000)
    p.add_argument("--per-sig", type=int, default=40)
    p.add_argument("--min-quality", type=float, default=0.3)
    p.add_argument("--min-persian", type=float, default=0.6)
    p.add_argument("--min-tokens", type=int, default=5)
    p.add_argument("--dry-run", action="store_true")
    a = p.parse_args()
    ingest(a.path, a.target, a.per_sig, a.min_quality, a.min_persian,
           a.min_tokens, a.dry_run)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())