"""Sentence frames mined from real corpus sentences.

A frame is a real Persian sentence whose content words were replaced by slots.
Word order, function words and punctuation come from the source text, never
from a hand-written rule. For every slot we keep the words that actually
occupied that position, so the composer can substitute a distributional peer.
"""
from __future__ import annotations

import argparse
import re
import sqlite3
import time
from pathlib import Path

from agent.config import STATE_DIR, assert_writable, ensure_dirs
from agent.errors import AgentError

from . import corpus, lexicon, usage
from .text import (ZWNJ, normalize, persian_ratio, quality, tokens)

FRAMES_DB = STATE_DIR / "farsi_frames.db"

MIN_TOKENS = 4
MAX_TOKENS = 18
MIN_FIXED = 2
MAX_SLOTS = 2
MIN_PERSIAN = 0.5
MIN_QUALITY = 0.3
MIN_FRAME_WEIGHT = 0.1

_SLOT = re.compile(r"\{(\d+)\}")
_CHUNK = re.compile(r"\S+")
_LATIN = re.compile(r"[A-Za-z]")
# text.sentences() drops the terminator, so a line break looks like a full
# stop. Keeping the terminator lets us reject fragments that never ended.
_TERMINATED = re.compile(r"[^.!؟?\n]+([.!؟?\n]+|$)")
_STOPS = ".!؟?"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS frames(
    fkey TEXT PRIMARY KEY,
    surface TEXT,
    slots INTEGER,
    tokens_n INTEGER,
    n REAL DEFAULT 0,
    positive INTEGER DEFAULT 0,
    negative INTEGER DEFAULT 0,
    neutral INTEGER DEFAULT 0,
    score_sum REAL DEFAULT 0,
    score_count INTEGER DEFAULT 0,
    example TEXT,
    example_doc INTEGER,
    first_doc INTEGER,
    last_doc INTEGER,
    updated_ts REAL);
CREATE TABLE IF NOT EXISTS frame_slots(
    fkey TEXT, idx INTEGER, term TEXT, n REAL,
    PRIMARY KEY(fkey, idx, term));
CREATE INDEX IF NOT EXISTS idx_frame_slots_term ON frame_slots(term, n DESC);
CREATE TABLE IF NOT EXISTS meta(mkey TEXT PRIMARY KEY, mvalue TEXT);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = assert_writable(Path(path) if path else FRAMES_DB)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    return db


def _meta(db: sqlite3.Connection, key: str, default: str) -> str:
    row = db.execute("SELECT mvalue FROM meta WHERE mkey=?", (key,)).fetchone()
    return str(row["mvalue"]) if row else default


def _set_meta(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute(
        "INSERT INTO meta(mkey,mvalue) VALUES(?,?) "
        "ON CONFLICT(mkey) DO UPDATE SET mvalue=excluded.mvalue",
        (key, str(value)),
    )


def _stopped_sentences(raw: str):
    for match in _TERMINATED.finditer(str(raw or "")):
        stop = match.group(1) or ""
        if not any(mark in stop for mark in _STOPS):
            continue
        body = match.group(0)[:len(match.group(0)) - len(stop)].strip()
        if body:
            yield body


def _units(folded: str) -> list[dict] | None:
    """Align written chunks to normalized keys, keeping absolute offsets."""
    units: list[dict] = []
    for match in _CHUNK.finditer(folded):
        chunk = match.group(0)
        start, end = 0, len(chunk)
        while start < end and not (chunk[start].isalnum() or chunk[start] == ZWNJ):
            start += 1
        while end > start and not (chunk[end - 1].isalnum() or chunk[end - 1] == ZWNJ):
            end -= 1
        core = chunk[start:end].strip(ZWNJ)
        if not core:
            continue
        keys = tokens(core)
        if len(keys) != 1:
            return None
        offset = chunk.find(core, start)
        units.append({"key": keys[0],
                      "s": match.start() + offset,
                      "e": match.start() + offset + len(core)})
    return units


def _pattern(folded: str, units: list[dict], chosen: list[int],
             normalized: bool) -> str:
    parts: list[str] = []
    cursor = 0
    numbers = {index: number for number, index in enumerate(chosen)}
    for index, unit in enumerate(units):
        literal = folded[cursor:unit["s"]].replace("{", "").replace("}", "")
        parts.append(literal)
        if index in numbers:
            parts.append("{" + str(numbers[index]) + "}")
        elif normalized:
            parts.append(unit["key"])
        else:
            parts.append(folded[unit["s"]:unit["e"]])
        cursor = unit["e"]
    parts.append(folded[cursor:].replace("{", "").replace("}", ""))
    return "".join(parts).strip()


def _profile(udb: sqlite3.Connection, key: str, cache: dict,
             docs_total: int) -> tuple[bool, float]:
    if key in cache:
        return cache[key]
    content = lexicon.is_content(udb, key, docs_total=docs_total, ready=True)
    rank = 0.0
    if content:
        rank = lexicon.specificity(udb, key, docs_total) * usage.confidence(udb, key)
    cache[key] = (content, rank)
    return cache[key]


def _store(db: sqlite3.Connection, fkey: str, surface: str, slots: int,
           tokens_n: int, fillers: list[str], example: str, doc_id: int,
           now: float) -> None:
    row = db.execute("SELECT fkey, surface FROM frames WHERE fkey=?",
                     (fkey,)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO frames(fkey,surface,slots,tokens_n,n,example,"
            "example_doc,first_doc,last_doc,updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?)",
            (fkey, surface, slots, tokens_n, 1.0, example[:400], doc_id,
             doc_id, doc_id, now),
        )
    else:
        keep = surface
        old = str(row["surface"] or "")
        if (surface.count(ZWNJ), -len(surface)) > (old.count(ZWNJ), -len(old)):
            keep = surface
        db.execute(
            "UPDATE frames SET n=n+1, surface=?, last_doc=?, updated_ts=? "
            "WHERE fkey=?",
            (keep, doc_id, now, fkey),
        )

    db.executemany(
        "INSERT INTO frame_slots(fkey,idx,term,n) VALUES(?,?,?,1) "
        "ON CONFLICT(fkey,idx,term) DO UPDATE SET n=n+1",
        [(fkey, index, term) for index, term in enumerate(fillers)],
    )


def mine(db: sqlite3.Connection, cdb: sqlite3.Connection,
         udb: sqlite3.Connection, *, limit_docs: int | None = None,
         max_seconds: float = 60.0) -> dict:
    if not corpus.learning_enabled():
        return {"skipped": "learn-off"}
    info = lexicon.evidence(udb)
    if not info["ready"]:
        return {"skipped": "corpus-too-small",
                "terms": info["terms"], "docs": info["docs"]}

    started = time.time()
    last = int(_meta(db, "last_doc", "0"))
    newest = last
    docs = made = 0
    rejected = {"no_stop": 0, "latin": 0, "low_quality": 0, "shape": 0,
                "no_content": 0}
    cache: dict[str, tuple[bool, float]] = {}
    docs_total = info["docs"]

    cursor = cdb.execute("SELECT id, raw FROM docs WHERE id > ? ORDER BY id",
                         (last,))
    for row in cursor:
        doc_id = int(row["id"])
        newest = max(newest, doc_id)
        docs += 1
        raw = row["raw"] or ""

        for match in _TERMINATED.finditer(raw):
            stop = match.group(1) or ""
            if not any(mark in stop for mark in _STOPS):
                rejected["no_stop"] += 1
                continue

        for sentence in _stopped_sentences(raw):
            if _LATIN.search(sentence):
                rejected["latin"] += 1
                continue
            if (len(sentence) < MIN_TOKENS
                    or persian_ratio(sentence) < MIN_PERSIAN
                    or quality(sentence) < MIN_QUALITY):
                rejected["low_quality"] += 1
                continue

            folded = normalize(sentence, keep_zwnj=True)
            units = _units(folded)
            if not units or not MIN_TOKENS <= len(units) <= MAX_TOKENS:
                rejected["shape"] += 1
                continue

            ranked: list[tuple[float, int]] = []
            for index, unit in enumerate(units):
                content, rank = _profile(udb, unit["key"], cache, docs_total)
                if content:
                    ranked.append((rank, index))
            if not ranked:
                rejected["no_content"] += 1
                continue
            ranked.sort(key=lambda item: (-item[0], item[1]))

            for size in range(1, min(MAX_SLOTS, len(ranked)) + 1):
                chosen = sorted(index for _, index in ranked[:size])
                if len(units) - len(chosen) < MIN_FIXED:
                    continue
                fkey = _pattern(folded, units, chosen, normalized=True)
                surface = _pattern(folded, units, chosen, normalized=False)
                fillers = [units[index]["key"] for index in chosen]
                _store(db, fkey, surface, len(chosen), len(units), fillers,
                       sentence.strip(), doc_id, time.time())
                made += 1

        if docs % 100 == 0:
            _set_meta(db, "last_doc", str(newest))
            db.commit()
        if limit_docs and docs >= limit_docs:
            break
        if time.time() - started > max_seconds:
            break

    _set_meta(db, "last_doc", str(newest))
    db.commit()
    return {"docs": docs, "frames_added": made,
            "sentences_skipped": sum(rejected.values()),
            "rejected": rejected,
            "seconds": round(time.time() - started, 2)}


def frame_weight(db: sqlite3.Connection, fkey: str) -> float:
    row = db.execute("SELECT positive, negative FROM frames WHERE fkey=?",
                     (fkey,)).fetchone()
    if row is None:
        return 1.0
    positive = int(row["positive"] or 0)
    negative = int(row["negative"] or 0)
    value = 2.0 * (positive + 1) / (positive + negative + 2)
    return round(max(MIN_FRAME_WEIGHT, value), 3)


def rate_frame(db: sqlite3.Connection, fkey: str, score: float) -> dict:
    row = _resolve(db, fkey)
    try:
        value = float(score)
    except (TypeError, ValueError):
        raise AgentError("نمره باید عدد باشد")
    if not 0.0 <= value <= 5.0:
        raise AgentError("نمره باید بین 0 تا 5 باشد")

    positive = 1 if value >= 4.0 else 0
    negative = 1 if value <= 2.0 else 0
    neutral = 1 if not positive and not negative else 0
    db.execute(
        "UPDATE frames SET positive=positive+?, negative=negative+?, "
        "neutral=neutral+?, score_sum=score_sum+?, score_count=score_count+?, "
        "updated_ts=? WHERE fkey=?",
        (positive, negative, neutral, value, 1, time.time(), row["fkey"]),
    )
    db.commit()
    return {"fkey": row["fkey"], "score": value,
            "weight": frame_weight(db, row["fkey"])}


def _resolve(db: sqlite3.Connection, ref: str) -> dict:
    text = str(ref or "").strip()
    if not text:
        raise AgentError("شناسهٔ اسکلت خالی است")
    if text.isdigit():
        row = db.execute("SELECT rowid, * FROM frames WHERE rowid=?",
                         (int(text),)).fetchone()
    else:
        row = db.execute("SELECT rowid, * FROM frames WHERE fkey=?",
                         (text,)).fetchone()
    if row is None:
        raise AgentError(f"اسکلت «{text}» پیدا نشد")
    return dict(row)


def slot_fillers(db: sqlite3.Connection, fkey: str,
                 idx: int | None = None) -> list[dict]:
    if idx is None:
        rows = db.execute(
            "SELECT idx, term, n FROM frame_slots WHERE fkey=? "
            "ORDER BY idx, n DESC", (fkey,)).fetchall()
    else:
        rows = db.execute(
            "SELECT idx, term, n FROM frame_slots WHERE fkey=? AND idx=? "
            "ORDER BY n DESC", (fkey, int(idx))).fetchall()
    return [dict(row) for row in rows]


def frames_with_filler(db: sqlite3.Connection, term: str,
                       limit: int = 20) -> list[dict]:
    key = (tokens(term) or [""])[0]
    if not key:
        return []
    rows = db.execute(
        "SELECT f.rowid, f.*, fs.idx, fs.n AS filler_n "
        "FROM frame_slots fs JOIN frames f ON f.fkey = fs.fkey "
        "WHERE fs.term=? ORDER BY fs.n DESC, f.n DESC LIMIT ?",
        (key, max(1, int(limit))),
    ).fetchall()

    out = []
    for row in rows:
        item = dict(row)
        item["weight"] = frame_weight(db, item["fkey"])
        if item["weight"] <= MIN_FRAME_WEIGHT:
            continue
        item["rank_score"] = round(float(item["filler_n"]) * item["weight"], 4)
        out.append(item)
    out.sort(key=lambda item: (-item["rank_score"], item["fkey"]))
    return out


def top_frames(db: sqlite3.Connection, limit: int = 20) -> list[dict]:
    rows = db.execute(
        "SELECT rowid, * FROM frames ORDER BY n DESC, slots ASC LIMIT ?",
        (max(1, int(limit)),),
    ).fetchall()
    out = []
    for row in rows:
        item = dict(row)
        item["weight"] = frame_weight(db, item["fkey"])
        out.append(item)
    return out


def render(db: sqlite3.Connection, ldb: sqlite3.Connection, fkey: str,
           fillers: dict[int, str]) -> str:
    row = _resolve(db, fkey)
    surface = str(row["surface"] or "")
    clean: dict[int, str] = {}

    for raw_index, raw_term in (fillers or {}).items():
        try:
            index = int(raw_index)
        except (TypeError, ValueError):
            raise AgentError("شمارهٔ جای‌نگهدار باید عدد باشد")
        parts = tokens(str(raw_term or ""))
        if not parts:
            raise AgentError(f"واژهٔ جای‌نگهدار {index} خالی است")
        clean[index] = " ".join(lexicon.surface_for(ldb, part) for part in parts)

    missing = [str(i) for i in range(int(row["slots"])) if i not in clean]
    if missing:
        raise AgentError("جای‌نگهدار پر نشده: " + ", ".join(missing))

    def take(match: re.Match) -> str:
        index = int(match.group(1))
        if index not in clean:
            raise AgentError(f"جای‌نگهدار {index} در این اسکلت نیست")
        return clean[index]

    return _SLOT.sub(take, surface).strip()


def stats(db: sqlite3.Connection) -> dict:
    total = db.execute("SELECT COUNT(*) AS n FROM frames").fetchone()["n"]
    one = db.execute(
        "SELECT COUNT(*) AS n FROM frames WHERE slots=1").fetchone()["n"]
    two = db.execute(
        "SELECT COUNT(*) AS n FROM frames WHERE slots=2").fetchone()["n"]
    fills = db.execute("SELECT COUNT(*) AS n FROM frame_slots").fetchone()["n"]
    rated = db.execute(
        "SELECT COUNT(*) AS n FROM frames WHERE score_count>0").fetchone()["n"]
    return {"frames": int(total or 0), "one_slot": int(one or 0),
            "two_slot": int(two or 0), "slot_fillers": int(fills or 0),
            "rated": int(rated or 0),
            "last_doc": int(_meta(db, "last_doc", "0"))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.frames", description="اسکلت‌های استخراج‌شده از متن واقعی")
    sub = parser.add_subparsers(dest="command", required=True)

    mn = sub.add_parser("mine", help="استخراج اسکلت از پیکره")
    mn.add_argument("--limit-docs", type=int, default=None)
    mn.add_argument("--max-seconds", type=float, default=60.0)

    fw = sub.add_parser("for-word", help="اسکلت‌هایی که این واژه در آن‌ها بوده")
    fw.add_argument("word")
    fw.add_argument("--limit", type=int, default=20)

    ls = sub.add_parser("list", help="پرتکرارترین اسکلت‌ها")
    ls.add_argument("--limit", type=int, default=20)

    sh = sub.add_parser("show", help="نمایش یک اسکلت")
    sh.add_argument("ref")

    rd = sub.add_parser("render", help="پر کردن جای‌نگهدارها")
    rd.add_argument("ref")
    rd.add_argument("--fill", action="append", default=[],
                    help="مثال: --fill 0=عدالت --fill 1=انصاف")

    rt = sub.add_parser("rate", help="امتیاز به یک اسکلت")
    rt.add_argument("ref")
    rt.add_argument("--score", type=float, required=True)

    sub.add_parser("stats", help="آمار")

    args = parser.parse_args(argv)

    db = connect()
    ldb = lexicon.connect()
    udb = usage.connect()
    cdb = corpus.connect()

    try:
        if args.command == "mine":
            print(mine(db, cdb, udb, limit_docs=args.limit_docs,
                       max_seconds=args.max_seconds))
            print(stats(db))
            return 0

        if args.command == "for-word":
            items = frames_with_filler(db, args.word, args.limit)
            if not items:
                print("اسکلتی برای این واژه پیدا نشد.")
            for item in items:
                print(f"[{item['rowid']}] slot={item['idx']} "
                      f"n={item['n']} w={item['weight']}")
                print("   ", item["surface"])
            return 0

        if args.command == "list":
            for item in top_frames(db, args.limit):
                print(f"[{item['rowid']}] n={item['n']} slots={item['slots']} "
                      f"w={item['weight']}")
                print("   ", item["surface"])
            return 0

        if args.command == "show":
            item = _resolve(db, args.ref)
            print("کلید:", item["fkey"])
            print("نمایش:", item["surface"])
            print("شواهد:", item["n"], "| جای‌نگهدار:", item["slots"],
                  "| وزن:", frame_weight(db, item["fkey"]))
            print("نمونهٔ منبع:", item["example"])
            for filler in slot_fillers(db, item["fkey"]):
                print(f"   slot {filler['idx']}: {filler['term']} "
                      f"(n={filler['n']})")
            return 0

        if args.command == "render":
            fillers: dict[int, str] = {}
            for item in args.fill:
                if "=" not in item:
                    raise AgentError("فرمت fill باید مثل 0=عدالت باشد")
                index, term = item.split("=", 1)
                fillers[int(index)] = term
            print(render(db, ldb, args.ref, fillers))
            return 0

        if args.command == "rate":
            print(rate_frame(db, args.ref, args.score))
            return 0

        print(stats(db))
        return 0

    except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
        print("توقف:", exc)
        return 1
    finally:
        cdb.close()
        udb.close()
        ldb.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())