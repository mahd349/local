"""Word-level knowledge on top of the usage engine.

Document frequency is counted per SENTENCE, not per corpus row: a whole book
may arrive as one document, which would make every idf collapse. Specificity is
judged as a share of the maximum possible idf, so the same threshold works for
one book and for a hundred. The unit of knowledge stays a word or a learned
phrase, never a finished answer.
"""
from __future__ import annotations

import argparse
import math
import sqlite3
import time
from functools import lru_cache
from pathlib import Path

from agent.config import STATE_DIR, assert_writable, ensure_dirs
from agent.errors import AgentError

from . import corpus, usage
from .text import ZWNJ, normalize, sentences, tokens

LEXICON_DB = STATE_DIR / "farsi_lexicon.db"

MIN_TERMS = 60
MIN_SENTENCES = 20
MIN_WORD_FREQ = 2.0
MIN_WORD_CONF = 0.15
CONTENT_IDF_RATIO = 0.28
MIN_NEIGHBOR_SCORE = 0.35
MIN_PHRASE_NPMI = 0.35
MIN_WORD_WEIGHT = 0.1

_SCHEMA = """
CREATE TABLE IF NOT EXISTS surface(
    term TEXT, form TEXT, n REAL, PRIMARY KEY(term, form));
CREATE INDEX IF NOT EXISTS idx_surface_term ON surface(term, n DESC);
CREATE TABLE IF NOT EXISTS df(term TEXT PRIMARY KEY, n INTEGER DEFAULT 0);
CREATE TABLE IF NOT EXISTS word_stats(
    term TEXT PRIMARY KEY,
    positive INTEGER DEFAULT 0,
    negative INTEGER DEFAULT 0,
    neutral INTEGER DEFAULT 0,
    score_sum REAL DEFAULT 0,
    score_count INTEGER DEFAULT 0,
    updated_ts REAL);
CREATE TABLE IF NOT EXISTS meta(mkey TEXT PRIMARY KEY, mvalue TEXT);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = assert_writable(Path(path) if path else LEXICON_DB)
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


@lru_cache(maxsize=1)
def _df_snapshot() -> tuple[int, dict[str, int]]:
    """Read-only snapshot; invalidated by harvest. Keeps call sites cheap."""
    db = connect()
    try:
        total = int(_meta(db, "sentences_total", "0"))
        rows = db.execute("SELECT term, n FROM df").fetchall()
        return total, {str(row["term"]): int(row["n"]) for row in rows}
    finally:
        db.close()


def _idf_total() -> int:
    return _df_snapshot()[0]


def idf_ceiling(docs_total: int | None = None) -> float:
    total = _idf_total() if docs_total is None else docs_total
    return math.log1p(max(1, total))


def evidence(udb: sqlite3.Connection) -> dict:
    """`docs` is the idf denominator (sentences), kept for call-site compat."""
    total = _idf_total()
    terms = int(usage.stats(udb)["terms"])
    return {"terms": terms, "docs": total, "sentences": total,
            "ready": terms >= MIN_TERMS and total >= MIN_SENTENCES}


def specificity(udb: sqlite3.Connection, term: str,
                docs_total: int | None = None) -> float:
    if usage.term(udb, term) is None:
        return 0.0
    total, table = _df_snapshot()
    seen = table.get(term, 0)
    if seen <= 0:
        return 0.0
    denominator = total if docs_total is None else docs_total
    return round(math.log(1.0 + max(1, denominator) / seen), 4)


def specificity_ratio(udb: sqlite3.Connection, term: str,
                      docs_total: int | None = None) -> float:
    """Absolute idf shrinks as sentences grow; the share does not."""
    ceiling = idf_ceiling(docs_total)
    if ceiling <= 0:
        return 0.0
    return round(specificity(udb, term, docs_total) / ceiling, 4)


def is_content(udb: sqlite3.Connection, term: str, *,
               docs_total: int | None = None, ready: bool | None = None) -> bool:
    info_ready = evidence(udb)["ready"] if ready is None else ready
    if not info_ready:
        return False
    row = usage.term(udb, term)
    if row is None or float(row["freq"]) < MIN_WORD_FREQ:
        return False
    if usage.confidence(udb, term) < MIN_WORD_CONF:
        return False
    return specificity_ratio(udb, term, docs_total) >= CONTENT_IDF_RATIO


def _surface_pairs(raw: str):
    for chunk in normalize(raw, keep_zwnj=True).split():
        form = chunk
        while form and not (form[0].isalnum() or form[0] == ZWNJ):
            form = form[1:]
        while form and not (form[-1].isalnum() or form[-1] == ZWNJ):
            form = form[:-1]
        form = form.strip(ZWNJ)
        if not form:
            continue
        keys = tokens(form)
        if len(keys) != 1:
            continue
        yield keys[0], form


def _flush(db: sqlite3.Connection, surface_counts: dict,
           df_counts: dict) -> None:
    if surface_counts:
        db.executemany(
            "INSERT INTO surface(term,form,n) VALUES(?,?,?) "
            "ON CONFLICT(term,form) DO UPDATE SET n=n+excluded.n",
            [(term, form, n) for (term, form), n in surface_counts.items()],
        )
    if df_counts:
        db.executemany(
            "INSERT INTO df(term,n) VALUES(?,?) "
            "ON CONFLICT(term) DO UPDATE SET n=n+excluded.n",
            list(df_counts.items()),
        )
    db.commit()


def harvest(db: sqlite3.Connection, cdb: sqlite3.Connection, *,
            limit_docs: int | None = None, max_seconds: float = 60.0,
            rebuild: bool = False) -> dict:
    if not corpus.learning_enabled():
        return {"skipped": "learn-off"}

    if rebuild:
        db.execute("DELETE FROM surface")
        db.execute("DELETE FROM df")
        _set_meta(db, "last_doc", "0")
        _set_meta(db, "sentences_total", "0")
        db.commit()
        _df_snapshot.cache_clear()

    started = time.time()
    last = int(_meta(db, "last_doc", "0"))
    base_total = int(_meta(db, "sentences_total", "0"))
    newest = last
    docs = forms = fresh_sentences = 0
    surface_counts: dict[tuple[str, str], float] = {}
    df_counts: dict[str, int] = {}

    cursor = cdb.execute("SELECT id, raw FROM docs WHERE id > ? ORDER BY id",
                         (last,))
    for row in cursor:
        doc_id = int(row["id"])
        newest = max(newest, doc_id)
        docs += 1
        raw = row["raw"] or ""

        for term, form in _surface_pairs(raw):
            surface_counts[(term, form)] = surface_counts.get((term, form), 0.0) + 1.0
            forms += 1

        for sentence in sentences(raw):
            keys = set(tokens(sentence))
            if not keys:
                continue
            fresh_sentences += 1
            for key in keys:
                df_counts[key] = df_counts.get(key, 0) + 1

        if docs % 100 == 0:
            _flush(db, surface_counts, df_counts)
            surface_counts.clear()
            df_counts.clear()
            _set_meta(db, "sentences_total", str(base_total + fresh_sentences))
            _set_meta(db, "last_doc", str(newest))
            db.commit()
            _df_snapshot.cache_clear()
        if limit_docs and docs >= limit_docs:
            break
        if time.time() - started > max_seconds:
            break

    _flush(db, surface_counts, df_counts)
    _set_meta(db, "sentences_total", str(base_total + fresh_sentences))
    _set_meta(db, "last_doc", str(newest))
    db.commit()
    _df_snapshot.cache_clear()

    return {"docs": docs, "forms": forms, "sentences": fresh_sentences,
            "sentences_total": base_total + fresh_sentences,
            "seconds": round(time.time() - started, 2)}


def surface_for(db: sqlite3.Connection, term: str) -> str:
    rows = db.execute("SELECT form, n FROM surface WHERE term=?",
                      (term,)).fetchall()
    if not rows:
        return term
    best = max(rows, key=lambda r: (float(r["n"]), ZWNJ in r["form"],
                                    -len(r["form"])))
    return str(best["form"])


def restore(db: sqlite3.Connection, text: str) -> str:
    return " ".join(surface_for(db, term) for term in tokens(text))


def word_weight(db: sqlite3.Connection, term: str) -> float:
    row = db.execute("SELECT positive, negative FROM word_stats WHERE term=?",
                     (term,)).fetchone()
    if row is None:
        return 1.0
    positive = int(row["positive"] or 0)
    negative = int(row["negative"] or 0)
    return round(max(MIN_WORD_WEIGHT,
                     2.0 * (positive + 1) / (positive + negative + 2)), 3)


def rate_word(db: sqlite3.Connection, term: str, score: float) -> dict:
    key = (tokens(term) or [""])[0]
    if not key:
        raise AgentError("واژه خالی است")
    try:
        value = float(score)
    except (TypeError, ValueError):
        raise AgentError("نمره باید عدد باشد")
    if not 0.0 <= value <= 5.0:
        raise AgentError("نمره باید بین 0 تا 5 باشد")

    positive = 1 if value >= 4.0 else 0
    negative = 1 if value <= 2.0 else 0
    neutral = 1 if not positive and not negative else 0
    now = time.time()

    row = db.execute("SELECT term FROM word_stats WHERE term=?",
                     (key,)).fetchone()
    if row is None:
        db.execute(
            "INSERT INTO word_stats"
            "(term,positive,negative,neutral,score_sum,score_count,updated_ts) "
            "VALUES(?,?,?,?,?,?,?)",
            (key, positive, negative, neutral, value, 1, now),
        )
    else:
        db.execute(
            "UPDATE word_stats SET positive=positive+?, negative=negative+?, "
            "neutral=neutral+?, score_sum=score_sum+?, score_count=score_count+?, "
            "updated_ts=? WHERE term=?",
            (positive, negative, neutral, value, 1, now, key),
        )
    db.commit()
    return {"term": key, "score": value, "weight": word_weight(db, key)}


def topic_of(db: sqlite3.Connection, udb: sqlite3.Connection, text: str) -> dict:
    info = evidence(udb)
    known: list[dict] = []
    unknown: list[str] = []

    for word in dict.fromkeys(tokens(text)):
        row = usage.term(udb, word)
        if row is None:
            unknown.append(word)
            continue
        if not info["ready"]:
            continue
        conf = usage.confidence(udb, word)
        idf = specificity(udb, word, info["docs"])
        ratio = specificity_ratio(udb, word, info["docs"])
        if (conf < MIN_WORD_CONF or ratio < CONTENT_IDF_RATIO
                or float(row["freq"]) < MIN_WORD_FREQ):
            continue
        weight = word_weight(db, word)
        known.append({"term": word, "surface": surface_for(db, word),
                      "confidence": conf, "specificity": idf, "ratio": ratio,
                      "weight": weight,
                      "score": round(idf * conf * weight, 4)})

    known.sort(key=lambda item: (-item["score"], item["term"]))
    if known:
        reason = ""
    elif not info["ready"]:
        reason = "corpus-too-small"
    else:
        reason = "no-known-content-word"

    return {"topic": known[0]["term"] if known else None,
            "surface": known[0]["surface"] if known else "",
            "candidates": known[:5], "unknown": unknown, "reason": reason,
            "terms": info["terms"], "docs": info["docs"]}


def related(db: sqlite3.Connection, udb: sqlite3.Connection, term: str,
            limit: int = 12) -> list[dict]:
    key = (tokens(term) or [""])[0]
    if not key:
        return []

    docs_total = _idf_total()
    out: list[dict] = []
    seen: set[str] = {key}

    try:
        peers = usage.neighbors(udb, key, limit=max(limit * 3, limit))
    except (sqlite3.Error, KeyError, ValueError):
        peers = []

    for item in peers:
        name = str(item["term"])
        if name in seen:
            continue
        if (float(item["score"]) < MIN_NEIGHBOR_SCORE
                or float(item["confidence"]) < MIN_WORD_CONF):
            continue
        weight = word_weight(db, name)
        if weight <= MIN_WORD_WEIGHT:
            continue
        seen.add(name)
        out.append({"term": name, "surface": surface_for(db, name),
                    "kind": "word", "score": round(float(item["score"]), 4),
                    "confidence": float(item["confidence"]),
                    "specificity": specificity(udb, name, docs_total),
                    "weight": weight,
                    "rank_score": round(float(item["score"]) * weight, 4)})

    rows = udb.execute(
        "SELECT phrase, n, npmi FROM phrases WHERE phrase LIKE ? "
        "ORDER BY npmi DESC, n DESC LIMIT ?",
        (f"%{key}%", max(limit, 1)),
    ).fetchall()

    for row in rows:
        phrase = str(row["phrase"])
        parts = phrase.split()
        if key not in parts or phrase in seen:
            continue
        if float(row["npmi"]) < MIN_PHRASE_NPMI:
            continue
        weight = min(word_weight(db, part) for part in parts)
        if weight <= MIN_WORD_WEIGHT:
            continue
        seen.add(phrase)
        out.append({"term": phrase,
                    "surface": " ".join(surface_for(db, part) for part in parts),
                    "kind": "phrase", "score": round(float(row["npmi"]), 4),
                    "confidence": min(usage.confidence(udb, part) for part in parts),
                    "specificity": specificity(udb, parts[0], docs_total),
                    "weight": weight,
                    "rank_score": round(float(row["npmi"]) * weight, 4)})

    out.sort(key=lambda item: (-item["rank_score"], item["term"]))
    return out[:max(1, int(limit))]


def stats(db: sqlite3.Connection) -> dict:
    forms = db.execute("SELECT COUNT(*) AS n FROM surface").fetchone()["n"]
    terms = db.execute("SELECT COUNT(DISTINCT term) AS n FROM surface").fetchone()["n"]
    df_terms = db.execute("SELECT COUNT(*) AS n FROM df").fetchone()["n"]
    rated = db.execute("SELECT COUNT(*) AS n FROM word_stats").fetchone()["n"]
    return {"surface_forms": int(forms or 0), "surface_terms": int(terms or 0),
            "df_terms": int(df_terms or 0), "rated_words": int(rated or 0),
            "sentences_total": int(_meta(db, "sentences_total", "0")),
            "last_doc": int(_meta(db, "last_doc", "0"))}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.lexicon", description="دانش واژه‌ای روی موتور هم‌نشینی")
    sub = parser.add_subparsers(dest="command", required=True)

    hv = sub.add_parser("harvest", help="یادگیری شکل نوشتاری و فراوانی جمله‌ای")
    hv.add_argument("--limit-docs", type=int, default=None)
    hv.add_argument("--max-seconds", type=float, default=60.0)
    hv.add_argument("--rebuild", action="store_true")

    tp = sub.add_parser("topic", help="واژهٔ اصلی یک متن")
    tp.add_argument("text")

    rl = sub.add_parser("related", help="واژه‌های هم‌رده")
    rl.add_argument("word")
    rl.add_argument("--limit", type=int, default=12)

    dfc = sub.add_parser("df", help="فراوانی جمله‌ای و idf یک واژه")
    dfc.add_argument("word")

    rs = sub.add_parser("restore", help="بازگردانی شکل نوشتاری")
    rs.add_argument("text")

    rt = sub.add_parser("rate", help="امتیاز به یک واژه")
    rt.add_argument("word")
    rt.add_argument("--score", type=float, required=True)

    sub.add_parser("stats", help="آمار")

    args = parser.parse_args(argv)

    db = connect()
    udb = usage.connect()
    cdb = corpus.connect()

    try:
        if args.command == "harvest":
            print(harvest(db, cdb, limit_docs=args.limit_docs,
                          max_seconds=args.max_seconds, rebuild=args.rebuild))
            print(stats(db))
            return 0

        if args.command == "topic":
            report = topic_of(db, udb, args.text)
            print("موضوع:", report["surface"] or "-")
            if report["reason"]:
                print("دلیل:", report["reason"],
                      f"(واژه‌ها {report['terms']}، جمله {report['docs']})")
            for item in report["candidates"]:
                print(f"  {item['surface']:18} idf={item['specificity']} "
                      f"ratio={item['ratio']} conf={item['confidence']} "
                      f"w={item['weight']}")
            if report["unknown"]:
                print("نادیده:", " ".join(report["unknown"][:8]))
            return 0

        if args.command == "related":
            items = related(db, udb, args.word, args.limit)
            if not items:
                print("هم‌ردهٔ قابل اعتمادی پیدا نشد.")
            for item in items:
                print(f"{item['surface']:24} {item['kind']:6} "
                      f"score={item['score']} conf={item['confidence']} "
                      f"w={item['weight']}")
            return 0

        if args.command == "df":
            key = (tokens(args.word) or [""])[0]
            total, table = _df_snapshot()
            print("واژه:", key, "| جمله‌ها:", table.get(key, 0), "از", total)
            print("idf:", specificity(udb, key),
                  "| سقف:", round(idf_ceiling(), 4))
            print("نسبت:", specificity_ratio(udb, key),
                  "| آستانه:", CONTENT_IDF_RATIO)
            print("محتوایی:", is_content(udb, key))
            return 0

        if args.command == "restore":
            print(restore(db, args.text))
            return 0

        if args.command == "rate":
            print(rate_word(db, args.word, args.score))
            return 0

        print(stats(db))
        return 0

    except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
        print("توقف:", exc)
        return 1
    finally:
        cdb.close()
        udb.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())