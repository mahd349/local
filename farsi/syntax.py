"""Role-tagged Persian syntax with corpus-learned compound verbs.

Persian marks arguments overtly: را marks the definite object, the verb complex
sits at the right edge, prepositions label complements, ezafe binds a noun
phrase, personal endings encode agreement.

Compound verbs are learned, not listed. A token qualifies as a preverb when it
frequently sits immediately before a light verb, when that adjacency accounts
for a large share of its own usage, and when it is rarely marked as an argument
by را, a preposition, or an indefinite determiner. The last test keeps bare
objects out of the verb: غذا را خورد must not become a compound.

Every block stores its [start, end) span so the rendered pattern keeps the real
delimiters. Fillers pool per verb lemma and role, so evidence generalises across
surface patterns.
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from collections import Counter
from pathlib import Path

sys.path.insert(0, r"D:\Python")
sys.path.insert(0, r"D:\project\library")

from agent.config import STATE_DIR, assert_writable, ensure_dirs
from agent.errors import AgentError

from . import corpus, morph, usage
from .text import quality, sentences, tokens

SYNTAX_DB = STATE_DIR / "farsi_syntax.db"

MIN_FRAME_EVIDENCE = 3
MIN_FILLER_EVIDENCE = 2
MAX_TOKENS = 24
MIN_TOKENS = 4
MIN_QUALITY = 0.3
MAX_VERB_TOKENS = 4
CACHE_LIMIT = 200000

MIN_COMPOUND_COUNT = 6
MIN_COMPOUND_BOUND = 0.30
MAX_COMPOUND_ARG = 0.25
COPULA_LEMMA = "بودن"
COPULA_MIN_BOUND = 0.50
COPULA_MAX_ARG = 0.10
INDEFINITE = ("یک", "یه")

PREP_ROLE = {
    "به": "GOAL", "از": "SOURCE", "در": "LOC", "با": "INSTR",
    "برای": "BEN", "بر": "ON", "تا": "UNTIL", "بی": "WITHOUT",
    "بدون": "WITHOUT", "چون": "CAUSE", "زیرا": "CAUSE",
}
OBJECT_MARK = "را"
STOP_CHARS = ".!؟?،؛:«»()"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS role_frames(
    rkey TEXT PRIMARY KEY, pattern TEXT, verb_lemma TEXT, verb_surface TEXT,
    roles TEXT, n REAL DEFAULT 0, example TEXT, example_doc INTEGER,
    first_doc INTEGER, last_doc INTEGER, updated_ts REAL);
CREATE INDEX IF NOT EXISTS idx_rf_verb ON role_frames(verb_lemma, n DESC);
CREATE TABLE IF NOT EXISTS role_fillers(
    verb_lemma TEXT, role TEXT, term TEXT, n REAL,
    PRIMARY KEY(verb_lemma, role, term));
CREATE INDEX IF NOT EXISTS idx_rfill ON role_fillers(verb_lemma, role, n DESC);
CREATE TABLE IF NOT EXISTS frame_fillers(
    rkey TEXT, role TEXT, term TEXT, n REAL,
    PRIMARY KEY(rkey, role, term));
CREATE TABLE IF NOT EXISTS compounds(
    preverb TEXT, light_lemma TEXT, n REAL, freq REAL,
    bound REAL, arg REAL, accepted INTEGER, updated_ts REAL,
    PRIMARY KEY(preverb, light_lemma));
CREATE INDEX IF NOT EXISTS idx_compound_light
    ON compounds(light_lemma, accepted, n DESC);
CREATE TABLE IF NOT EXISTS meta(mkey TEXT PRIMARY KEY, mvalue TEXT);
"""

_CACHE: dict[tuple[str, bool], morph.Morph] = {}
_KEYS: dict[str, str] = {}


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = assert_writable(Path(path) if path else SYNTAX_DB)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=60000")
    db.executescript(_SCHEMA)
    return db


def read_meta(db: sqlite3.Connection, key: str, default: str) -> str:
    row = db.execute("SELECT mvalue FROM meta WHERE mkey=?", (key,)).fetchone()
    return str(row["mvalue"]) if row else default


def write_meta(db: sqlite3.Connection, key: str, value: str) -> None:
    db.execute("INSERT INTO meta(mkey,mvalue) VALUES(?,?) "
               "ON CONFLICT(mkey) DO UPDATE SET mvalue=excluded.mvalue",
               (key, str(value)))


def clean(word: str) -> str:
    return word.strip(STOP_CHARS).strip()


def key_of(word: str) -> str:
    hit = _KEYS.get(word)
    if hit is not None:
        return hit
    parts = tokens(word)
    value = parts[0] if len(parts) == 1 else morph.fold(word)
    if len(_KEYS) < CACHE_LIMIT:
        _KEYS[word] = value
    return value


def analyze(word: str, stems: set[str] | None, vocab: set[str] | None,
            final: bool = False) -> morph.Morph:
    key = (word, final)
    hit = _CACHE.get(key)
    if hit is not None:
        return hit
    value = morph.analyze(word, stems=stems, vocab=vocab, final=final)
    if len(_CACHE) < CACHE_LIMIT:
        _CACHE[key] = value
    return value


def semantic_head(morphs: list[morph.Morph]) -> morph.Morph:
    """In داده است the participle carries the meaning, the copula is auxiliary."""
    participles = [m for m in morphs if m.mood == "part"]
    if participles:
        return participles[-1]
    non_light = [m for m in morphs if not m.is_light]
    if non_light:
        return non_light[-1]
    return morphs[-1]


def verb_signature(morphs: list[morph.Morph], preverb: str = "",
                   preverb_key: str = "") -> tuple[str, str]:
    if not morphs:
        return "", ""
    base = morphs[0]
    tail = morphs[-1]
    head = semantic_head(morphs)
    surface = " ".join(m.surface for m in morphs)
    if preverb_key:
        anchor = base.lemma if base.is_light else head.lemma
        return preverb_key + "+" + anchor, (preverb + " " + surface).strip()
    if len(morphs) > 1 and tail.is_light:
        non_light = [m for m in morphs if not m.is_light]
        if non_light:
            return non_light[0].stem + "+" + tail.lemma, surface
    return head.lemma, surface


def split_object(words: list[str], analyses: list[morph.Morph]) -> tuple[int, bool]:
    """Find where the object noun phrase begins inside a longer span."""
    if len(words) <= 1:
        return 0, False
    if analyses[-1].pos == "ADJ":
        return 0, False
    start = len(words) - 1
    while start > 0 and analyses[start - 1].ezafe:
        start -= 1
    if start == 0:
        return 0, False
    for index in range(start):
        if analyses[index].pos == "PRON":
            return index + 1, True
    return start, True


def load_compounds(db: sqlite3.Connection) -> dict[str, set[str]]:
    out: dict[str, set[str]] = {}
    for row in db.execute("SELECT preverb, light_lemma FROM compounds "
                          "WHERE accepted=1"):
        out.setdefault(str(row["light_lemma"]), set()).add(str(row["preverb"]))
    return out


def parse(sentence: str, *, stems: set[str] | None = None,
          vocab: set[str] | None = None,
          compounds: dict[str, set[str]] | None = None) -> dict:
    words = [clean(w) for w in str(sentence or "").split()]
    words = [w for w in words if w]
    total = len(words)
    empty = {"blocks": [], "verb": [], "verb_lemma": "", "verb_surface": "",
             "preverb": "", "pattern": "", "roles": [], "agreement": None,
             "ambiguous": False}
    if not words:
        return empty

    known = stems if stems is not None else morph.known_stems(None)
    analyses = [analyze(w, known, vocab) for w in words]

    verb_start = total
    index = total - 1
    while index >= 0 and (total - index) <= MAX_VERB_TOKENS:
        current = analyses[index]
        if current.is_verb:
            verb_start = index
            index -= 1
            continue
        hinted = analyze(words[index], known, vocab, final=True)
        if hinted.is_verb:
            analyses[index] = hinted
            verb_start = index
            index -= 1
            continue
        break

    verb_morphs = analyses[verb_start:]
    preverb = ""
    preverb_key = ""
    if compounds and verb_morphs and verb_start > 0:
        base = verb_morphs[0]
        if base.is_light:
            candidate = key_of(words[verb_start - 1])
            if candidate in compounds.get(base.lemma, ()):
                preverb = words[verb_start - 1]
                preverb_key = candidate
                verb_start -= 1

    lemma, verb_surface = verb_signature(verb_morphs, preverb, preverb_key)

    blocks: list[dict] = []
    ambiguous = False
    span_start: int | None = None
    role = "AGENT"

    def close(end: int, force: str | None = None) -> None:
        nonlocal span_start, ambiguous
        if span_start is None or end <= span_start:
            span_start = None
            return
        chunk_words = words[span_start:end]
        chunk_morphs = analyses[span_start:end]
        label = force or role
        if force == "PATIENT":
            cut, did_split = split_object(chunk_words, chunk_morphs)
            if did_split and cut > 0:
                blocks.append({"role": "AGENT", "start": span_start,
                               "end": span_start + cut,
                               "surface": " ".join(chunk_words[:cut]),
                               "terms": tokens(" ".join(chunk_words[:cut]))})
                ambiguous = ambiguous or len(chunk_words) - cut > 1
                blocks.append({"role": "PATIENT", "start": span_start + cut,
                               "end": end,
                               "surface": " ".join(chunk_words[cut:]),
                               "terms": tokens(" ".join(chunk_words[cut:]))})
                span_start = None
                return
        blocks.append({"role": label, "start": span_start, "end": end,
                       "surface": " ".join(chunk_words),
                       "terms": tokens(" ".join(chunk_words))})
        span_start = None

    for index in range(verb_start):
        flat = morph.fold(words[index])
        if flat in PREP_ROLE:
            close(index)
            role = PREP_ROLE[flat]
            span_start = index + 1
            continue
        if flat == OBJECT_MARK:
            close(index, force="PATIENT")
            role = "AGENT"
            span_start = index + 1
            continue
        if span_start is None:
            span_start = index
    close(verb_start)

    seen_agent = False
    for block in blocks:
        if block["role"] == "AGENT":
            if seen_agent:
                block["role"] = "EXTRA"
            else:
                seen_agent = True
    refined: list[dict] = []
    for block in blocks:
        if block["role"] != "AGENT" or block["end"] - block["start"] <= 1:
            refined.append(block)
            continue
        chunk_words = words[block["start"]:block["end"]]
        chunk_morphs = analyses[block["start"]:block["end"]]
        cut = 0
        for position, item in enumerate(chunk_morphs[:-1]):
            if item.pos == "PRON":
                cut = position + 1
                break
        if cut == 0:
            edge = len(chunk_words) - 1
            while edge > 0 and chunk_morphs[edge - 1].ezafe:
                edge -= 1
            if edge == 0 or chunk_morphs[-1].pos == "ADJ":
                refined.append(block)
                continue
            cut = edge
            ambiguous = True
        refined.append({"role": "AGENT", "start": block["start"],
                        "end": block["start"] + cut,
                        "surface": " ".join(chunk_words[:cut]),
                        "terms": tokens(" ".join(chunk_words[:cut]))})
        refined.append({"role": "PATIENT", "start": block["start"] + cut,
                        "end": block["end"],
                        "surface": " ".join(chunk_words[cut:]),
                        "terms": tokens(" ".join(chunk_words[cut:]))})
    blocks = refined

    parts: list[str] = []
    cursor = 0
    for block in sorted(blocks, key=lambda b: b["start"]):
        parts.extend(words[cursor:block["start"]])
        parts.append("[" + block["role"] + "]")
        cursor = block["end"]
    parts.extend(words[cursor:verb_start])
    if lemma:
        tail = verb_morphs[-1]
        tense = "perf" if any(m.mood == "part" for m in verb_morphs) else tail.tense
        tag = lemma
        if tense:
            tag += f"#{tense}.{tail.person}{tail.number}"
        parts.append(tag)

    agreement = None
    agent = next((b for b in blocks if b["role"] == "AGENT"), None)
    if agent and verb_morphs:
        tail = verb_morphs[-1]
        pronoun = next((morph.PRONOUNS[t] for t in agent["terms"]
                        if t in morph.PRONOUNS), None)
        if pronoun and tail.person:
            agreement = {"expected": list(pronoun),
                         "actual": [tail.person, tail.number],
                         "ok": pronoun == (tail.person, tail.number)}

    return {"blocks": blocks, "verb": words[verb_start:], "verb_lemma": lemma,
            "verb_surface": verb_surface, "preverb": preverb,
            "pattern": " ".join(parts).strip(),
            "roles": [b["role"] for b in blocks], "agreement": agreement,
            "ambiguous": ambiguous}


def learn_compounds(db: sqlite3.Connection, cdb: sqlite3.Connection,
                    mdb: sqlite3.Connection, *, limit_docs: int | None = None,
                    max_seconds: float = 900.0,
                    min_count: int = MIN_COMPOUND_COUNT,
                    min_bound: float = MIN_COMPOUND_BOUND,
                    max_arg: float = MAX_COMPOUND_ARG) -> dict:
    """One full pass over the corpus; the result replaces the previous table."""
    started = time.time()
    stems = morph.known_stems(mdb)
    freq: Counter = Counter()
    adjacent: Counter = Counter()
    marked: Counter = Counter()
    docs = used = 0
    partial = False

    cursor = cdb.execute("SELECT id, raw FROM docs ORDER BY id")
    for row in cursor:
        docs += 1
        for sentence in sentences(str(row["raw"] or "")):
            words = [clean(w) for w in sentence.split()]
            words = [w for w in words if w]
            if not 3 <= len(words) <= MAX_TOKENS:
                continue
            keys = [key_of(w) for w in words]
            for position, item in enumerate(keys):
                freq[item] += 1
                if position + 1 < len(keys) and keys[position + 1] == OBJECT_MARK:
                    marked[item] += 1
                if position > 0 and (keys[position - 1] in PREP_ROLE
                                     or keys[position - 1] in INDEFINITE):
                    marked[item] += 1

            verb_start = len(words)
            collected: list[morph.Morph] = []
            index = len(words) - 1
            while index >= 0 and (len(words) - index) <= MAX_VERB_TOKENS:
                item = analyze(words[index], stems, None, final=True)
                if not item.is_verb:
                    break
                collected.insert(0, item)
                verb_start = index
                index -= 1
            if not collected or verb_start == 0:
                continue
            base = collected[0]
            if not base.is_light:
                continue
            if base.lemma == COPULA_LEMMA and base.mood == "inf":
                continue
            candidate_word = words[verb_start - 1]
            candidate_key = keys[verb_start - 1]
            if candidate_key in PREP_ROLE or candidate_key == OBJECT_MARK:
                continue
            if len(candidate_key) < 2 or any(c.isdigit() for c in candidate_key):
                continue
            item = analyze(candidate_word, stems, None)
            if item.pos in ("V", "PRON", "ADV", "ADJ") or item.affixes:
                continue
            adjacent[(candidate_key, base.lemma)] += 1
            used += 1

        if docs % 20000 == 0:
            print(f"  compounds scan: docs={docs:,} pairs={len(adjacent):,} "
                  f"{round(time.time() - started, 1)}s")
        if limit_docs and docs >= limit_docs:
            break
        if time.time() - started > max_seconds:
            partial = True
            break

    now = time.time()
    rows = []
    accepted = 0
    for (preverb, lemma), count in sorted(adjacent.items()):
        total = max(1, freq[preverb])
        bound = count / total
        arg = marked[preverb] / total
        need_bound = COPULA_MIN_BOUND if lemma == COPULA_LEMMA else min_bound
        need_arg = COPULA_MAX_ARG if lemma == COPULA_LEMMA else max_arg
        ok = count >= min_count and bound >= need_bound and arg <= need_arg
        accepted += int(ok)
        rows.append((preverb, lemma, float(count), float(total),
                     round(bound, 4), round(arg, 4), int(ok), now))

    db.execute("DELETE FROM compounds")
    db.executemany(
        "INSERT INTO compounds(preverb,light_lemma,n,freq,bound,arg,accepted,"
        "updated_ts) VALUES(?,?,?,?,?,?,?,?)", rows)
    db.commit()
    return {"docs": docs, "observations": used, "candidates": len(rows),
            "accepted": accepted, "partial": partial,
            "seconds": round(time.time() - started, 2)}


def reset(db: sqlite3.Connection) -> dict:
    counts = {}
    for table in ("role_frames", "role_fillers", "frame_fillers"):
        counts[table] = db.execute(
            f"SELECT COUNT(*) AS n FROM {table}").fetchone()["n"]
        db.execute(f"DELETE FROM {table}")
    write_meta(db, "last_doc", "0")
    db.commit()
    return {"cleared": counts, "last_doc": 0}


def mine(db: sqlite3.Connection, cdb: sqlite3.Connection,
         mdb: sqlite3.Connection, udb: sqlite3.Connection, *,
         limit_docs: int | None = None, max_seconds: float = 900.0,
         batch_docs: int = 200) -> dict:
    started = time.time()
    last = int(read_meta(db, "last_doc", "0"))
    newest = last
    stems = morph.known_stems(mdb)
    vocab = morph.load_vocab(udb)
    compounds = load_compounds(db)
    docs = made = skipped = 0
    frames: dict[str, dict] = {}
    fillers: dict[tuple, float] = {}
    frame_fillers: dict[tuple, float] = {}

    def flush() -> None:
        now = time.time()
        if frames:
            db.executemany(
                "INSERT INTO role_frames(rkey,pattern,verb_lemma,verb_surface,"
                "roles,n,example,example_doc,first_doc,last_doc,updated_ts) "
                "VALUES(?,?,?,?,?,?,?,?,?,?,?) ON CONFLICT(rkey) DO UPDATE SET "
                "n=n+excluded.n, last_doc=excluded.last_doc, "
                "updated_ts=excluded.updated_ts",
                [(k, v["pattern"], v["verb"], v["surface"], " ".join(v["roles"]),
                  v["n"], v["example"][:300], v["doc"], v["doc"], v["doc"], now)
                 for k, v in sorted(frames.items())])
        if fillers:
            db.executemany(
                "INSERT INTO role_fillers(verb_lemma,role,term,n) VALUES(?,?,?,?) "
                "ON CONFLICT(verb_lemma,role,term) DO UPDATE SET n=n+excluded.n",
                [(k[0], k[1], k[2], n) for k, n in sorted(fillers.items())])
        if frame_fillers:
            db.executemany(
                "INSERT INTO frame_fillers(rkey,role,term,n) VALUES(?,?,?,?) "
                "ON CONFLICT(rkey,role,term) DO UPDATE SET n=n+excluded.n",
                [(k[0], k[1], k[2], n) for k, n in sorted(frame_fillers.items())])
        write_meta(db, "last_doc", str(newest))
        db.commit()
        frames.clear(); fillers.clear(); frame_fillers.clear()

    cursor = cdb.execute("SELECT id, raw FROM docs WHERE id>? ORDER BY id", (last,))
    for row in cursor:
        doc_id = int(row["id"])
        newest = max(newest, doc_id)
        docs += 1
        for sentence in sentences(str(row["raw"] or "")):
            words = sentence.split()
            if not MIN_TOKENS <= len(words) <= MAX_TOKENS:
                skipped += 1
                continue
            if quality(sentence) < MIN_QUALITY:
                skipped += 1
                continue
            info = parse(sentence, stems=stems, vocab=vocab, compounds=compounds)
            if (not info["verb_lemma"] or "AGENT" not in info["roles"]
                    or info["ambiguous"]):
                skipped += 1
                continue
            key = info["pattern"]
            entry = frames.get(key)
            if entry is None:
                frames[key] = {"pattern": key, "verb": info["verb_lemma"],
                               "surface": info["verb_surface"],
                               "roles": info["roles"], "n": 1.0,
                               "example": sentence, "doc": doc_id}
            else:
                entry["n"] += 1.0
            for block in info["blocks"]:
                if block["role"] == "EXTRA":
                    continue
                for term in block["terms"]:
                    fk = (info["verb_lemma"], block["role"], term)
                    fillers[fk] = fillers.get(fk, 0.0) + 1.0
                    rk = (key, block["role"], term)
                    frame_fillers[rk] = frame_fillers.get(rk, 0.0) + 1.0
            made += 1

        if docs % batch_docs == 0:
            flush()
        if limit_docs and docs >= limit_docs:
            break
        if time.time() - started > max_seconds:
            break

    flush()
    return {"docs": docs, "sentences_used": made, "skipped": skipped,
            "compounds": sum(len(v) for v in compounds.values()),
            "last_doc": newest, "seconds": round(time.time() - started, 2)}


def fillers_for(db: sqlite3.Connection, verb_lemma: str, role: str,
                limit: int = 20) -> list[dict]:
    rows = db.execute(
        "SELECT term, n FROM role_fillers WHERE verb_lemma=? AND role=? "
        "AND n>=? ORDER BY n DESC LIMIT ?",
        (verb_lemma, role, MIN_FILLER_EVIDENCE, max(1, limit))).fetchall()
    return [dict(r) for r in rows]


def check(db: sqlite3.Connection, sentence: str, *,
          stems: set[str] | None = None, vocab: set[str] | None = None,
          compounds: dict[str, set[str]] | None = None) -> dict:
    info = parse(sentence, stems=stems, vocab=vocab, compounds=compounds)
    issues: list[str] = []
    if not info["verb_lemma"]:
        issues.append("no-verb")
    if "AGENT" not in info["roles"]:
        issues.append("no-agent")
    if info["agreement"] and not info["agreement"]["ok"]:
        issues.append("person-number-mismatch")
    if info["ambiguous"]:
        issues.append("ambiguous-object-boundary")
    row = db.execute("SELECT n FROM role_frames WHERE rkey=?",
                     (info["pattern"],)).fetchone()
    if row is None:
        issues.append("unseen-role-pattern")
    return {"sentence": sentence, "pattern": info["pattern"],
            "verb_lemma": info["verb_lemma"], "roles": info["roles"],
            "blocks": info["blocks"], "agreement": info["agreement"],
            "frame_evidence": float(row["n"]) if row else 0.0,
            "issues": issues, "ok": not issues}


def stats(db: sqlite3.Connection) -> dict:
    frames = db.execute("SELECT COUNT(*) AS n FROM role_frames").fetchone()["n"]
    solid = db.execute("SELECT COUNT(*) AS n FROM role_frames WHERE n>=?",
                       (MIN_FRAME_EVIDENCE,)).fetchone()["n"]
    fills = db.execute("SELECT COUNT(*) AS n FROM role_fillers").fetchone()["n"]
    verbs = db.execute("SELECT COUNT(DISTINCT verb_lemma) AS n "
                       "FROM role_frames").fetchone()["n"]
    comps = db.execute("SELECT COUNT(*) AS n FROM compounds "
                       "WHERE accepted=1").fetchone()["n"]
    return {"role_frames": int(frames), "solid_frames": int(solid),
            "role_fillers": int(fills), "verbs": int(verbs),
            "compounds": int(comps),
            "last_doc": int(read_meta(db, "last_doc", "0"))}


SAMPLES = ["علی کتاب را به مریم داد.", "آزادی به من تو",
           "من کتاب را خواندم.", "دانشجویان در کلاس نشستند.",
           "او شکست خورد.", "معلم درس را برای دانش‌آموزان توضیح داد.",
           "او غذا خورد.", "کتاب داده است."]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="farsi.syntax",
                                description="نقش‌های نحوی فارسی")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("selftest", help="تحلیل نمونه‌های ثابت")
    a = sub.add_parser("analyze", help="تحلیل نقش یک جمله")
    a.add_argument("sentence")
    c = sub.add_parser("check", help="بررسی نقش و تطابق")
    c.add_argument("sentence")
    sub.add_parser("reset", help="پاک کردن استخراج نقش‌ها")
    k = sub.add_parser("compounds", help="یادگیری فعل‌های مرکب از پیکره")
    k.add_argument("--limit-docs", type=int, default=None)
    k.add_argument("--max-seconds", type=float, default=900.0)
    k.add_argument("--min-count", type=int, default=MIN_COMPOUND_COUNT)
    k.add_argument("--min-bound", type=float, default=MIN_COMPOUND_BOUND)
    k.add_argument("--max-arg", type=float, default=MAX_COMPOUND_ARG)
    kl = sub.add_parser("compounds-list", help="فهرست مرکب‌های پذیرفته‌شده")
    kl.add_argument("--limit", type=int, default=40)
    m = sub.add_parser("mine", help="استخراج اسکلت نقش‌دار")
    m.add_argument("--limit-docs", type=int, default=None)
    m.add_argument("--max-seconds", type=float, default=900.0)
    f = sub.add_parser("fillers", help="پرکننده‌های یک نقش")
    f.add_argument("verb_lemma")
    f.add_argument("role")
    f.add_argument("--limit", type=int, default=20)
    sub.add_parser("stats", help="آمار")
    args = p.parse_args(argv)

    db = connect()
    mdb = morph.connect()
    try:
        stems = morph.known_stems(mdb)
        vocab = None
        try:
            udb = usage.connect()
            try:
                vocab = morph.load_vocab(udb, 150000)
            finally:
                udb.close()
        except sqlite3.Error:
            print("(واژگان در دسترس نبود)")
        compounds = load_compounds(db)

        if args.command == "selftest":
            print(f"مرکب‌های فعال: {sum(len(v) for v in compounds.values())}")
            for sentence in SAMPLES:
                info = parse(sentence, stems=stems, vocab=vocab,
                             compounds=compounds)
                print(f"\n{sentence}")
                print("  الگو:", info["pattern"])
                print("  فعل:", info["verb_lemma"], "|", info["verb_surface"])
                for block in info["blocks"]:
                    print(f"    {block['role']:<8} {block['surface']}")
                if info["agreement"]:
                    print("  تطابق:", info["agreement"])
            return 0
        if args.command == "analyze":
            info = parse(args.sentence, stems=stems, vocab=vocab,
                         compounds=compounds)
            print("الگو:", info["pattern"])
            print("فعل:", info["verb_lemma"], "|", info["verb_surface"])
            for block in info["blocks"]:
                print(f"  {block['role']:<8} {block['surface']}")
            print("تطابق:", info["agreement"], "| مبهم:", info["ambiguous"])
            return 0
        if args.command == "check":
            print(check(db, args.sentence, stems=stems, vocab=vocab,
                        compounds=compounds))
            return 0
        if args.command == "reset":
            print(reset(db))
            return 0
        if args.command == "compounds":
            cdb = corpus.connect()
            try:
                print(learn_compounds(db, cdb, mdb,
                                      limit_docs=args.limit_docs,
                                      max_seconds=args.max_seconds,
                                      min_count=args.min_count,
                                      min_bound=args.min_bound,
                                      max_arg=args.max_arg))
            finally:
                cdb.close()
            return 0
        if args.command == "compounds-list":
            rows = db.execute(
                "SELECT preverb, light_lemma, n, bound, arg FROM compounds "
                "WHERE accepted=1 ORDER BY n DESC LIMIT ?",
                (max(1, args.limit),)).fetchall()
            if not rows:
                print("مرکبی پذیرفته نشده؛ ابتدا compounds را اجرا کن.")
            for row in rows:
                print(f"{row['preverb']:<16} {row['light_lemma']:<12} "
                      f"n={row['n']:<7} bound={row['bound']} arg={row['arg']}")
            return 0
        if args.command == "mine":
            cdb = corpus.connect()
            udb = usage.connect()
            try:
                report = mine(db, cdb, mdb, udb, limit_docs=args.limit_docs,
                              max_seconds=args.max_seconds)
                print(report)
            finally:
                udb.close()
                cdb.close()
            print(stats(db))
            return 0
        if args.command == "fillers":
            for item in fillers_for(db, args.verb_lemma, args.role, args.limit):
                print(f"{item['term']:<20} n={item['n']}")
            return 0
        print(stats(db))
        return 0
    except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
        print("توقف:", exc)
        return 1
    finally:
        mdb.close()
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())