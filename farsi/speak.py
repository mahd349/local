"""Role-based generation with structural guards and verb diversity.

A sentence is assembled from an observed role frame; every slot is filled from
the pool of that same verb and role, and the verb is conjugated to agree with
the chosen subject.

Guards added after inspecting real output:
  * a frame with two identical role tags would render the same filler twice
  * a frame ending in a preposition comes from a truncated source sentence
  * a repeated surface token means the render collapsed two spans
  * picking only the highest-evidence frames lets one boilerplate family
    dominate, so frames are sampled with weights across the whole set
  * at most one output per verb, and copula frames are downweighted because
    their predicate has no selectional restriction we can verify

The usage database is never opened, so this runs during a co-occurrence build.
"""
from __future__ import annotations

import argparse
import math
import random
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\Python")
sys.path.insert(0, r"D:\project\library")

from agent.config import STATE_DIR, assert_writable, ensure_dirs

from . import corpus, lexicon, morph, pool, syntax
from .text import tokens

SPEAK_DB = STATE_DIR / "farsi_speak.db"

MIN_FRAME_EVIDENCE = 3.0
DEFAULT_TRIES = 240
DEFAULT_FRAME_POOL = 2000
COPULA_PENALTY = 0.45
PAST_END = {(1, "sg"): "م", (2, "sg"): "ی", (3, "sg"): "",
            (1, "pl"): "یم", (2, "pl"): "ید", (3, "pl"): "ند"}
PRES_END = {(1, "sg"): "م", (2, "sg"): "ی", (3, "sg"): "د",
            (1, "pl"): "یم", (2, "pl"): "ید", (3, "pl"): "ند"}
COPULA_END = {(1, "sg"): "ام", (2, "sg"): "ای", (3, "sg"): "است",
              (1, "pl"): "ایم", (2, "pl"): "اید", (3, "pl"): "اند"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS offers(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL, topic TEXT, rkey TEXT, text TEXT, score REAL);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = assert_writable(Path(path) if path else SPEAK_DB)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    db.row_factory = sqlite3.Row
    db.execute("PRAGMA busy_timeout=60000")
    db.executescript(_SCHEMA)
    return db


def conjugate(lemma: str, tense: str, person: int, number: str) -> str:
    parts = lemma.split("+")
    light = parts[-1]
    preverb = " ".join(parts[:-1])
    stem = light[:-1] if light.endswith("ن") and len(light) > 2 else light
    key = (person or 3, number or "sg")
    if tense == "pres":
        present = morph.PAST_TO_PRESENT.get(stem)
        body = ("می" + morph.ZWNJ + present + PRES_END[key]) if present \
            else stem + PAST_END[key]
    elif tense == "perf":
        body = stem + "ه" + " " + COPULA_END[key]
    else:
        body = stem + PAST_END[key]
    return (preverb + " " + body).strip()


def split_tag(pattern: str) -> tuple[list[str], str, str, int, str]:
    parts = pattern.split()
    tag = parts[-1]
    if "#" in tag:
        lemma, meta = tag.split("#", 1)
        tense, pn = meta.split(".", 1) if "." in meta else (meta, "")
        person = int(pn[0]) if pn and pn[0].isdigit() else 0
        number = pn[1:] if len(pn) > 1 else ""
    else:
        lemma, tense, person, number = tag, "", 0, ""
    return parts[:-1], lemma, tense, person, number


def structure(pattern: str) -> str:
    parts, lemma, _t, _p, _n = split_tag(pattern)
    return " ".join(parts + [lemma])


def frame_defect(body: list[str]) -> str:
    roles = [p[1:-1] for p in body if p.startswith("[") and p.endswith("]")]
    if not roles or "AGENT" not in roles:
        return "no-agent-role"
    if len(set(roles)) != len(roles):
        return "duplicate-role"
    literals = [morph.fold(p) for p in body
                if not (p.startswith("[") and p.endswith("]"))]
    if literals and literals[-1] in syntax.PREP_ROLE:
        return "dangling-preposition"
    for index in range(len(body) - 1):
        left, right = morph.fold(body[index]), morph.fold(body[index + 1])
        if left in syntax.PREP_ROLE and right in syntax.PREP_ROLE:
            return "stacked-preposition"
    return ""


def sample_frames(frames: list[dict], count: int, heat: float,
                  rng: random.Random) -> list[dict]:
    """Weighted sampling without replacement over the whole frame set."""
    if len(frames) <= count:
        picked = list(frames)
        rng.shuffle(picked)
        return picked
    keyed = []
    for frame in frames:
        weight = max(0.1, math.log1p(float(frame["n"]))) ** (1.0 / heat)
        keyed.append((rng.random() ** (1.0 / weight), frame))
    keyed.sort(key=lambda item: -item[0])
    return [frame for _, frame in keyed[:count]]


def pick(items: list[dict], temperature: float, min_p: float,
         rng: random.Random) -> dict | None:
    if not items:
        return None
    heat = max(0.05, float(temperature))
    weights = [max(1e-9, float(i["score"])) ** (1.0 / heat) for i in items]
    total = sum(weights) or 1.0
    probs = [w / total for w in weights]
    floor = max(probs) * max(0.0, min(1.0, min_p))
    kept = [(i, p) for i, p in zip(items, probs) if p >= floor]
    if not kept:
        kept = list(zip(items, probs))
    mass = sum(p for _, p in kept) or 1.0
    roll = rng.random() * mass
    acc = 0.0
    for item, prob in kept:
        acc += prob
        if roll <= acc:
            return item
    return kept[-1][0]


def subject_features(term: str, poses: dict[str, tuple[str, str, str]]) -> tuple[int, str]:
    if term in morph.PRONOUNS:
        return morph.PRONOUNS[term]
    info = poses.get(term)
    if info and info[1] == "pl":
        return 3, "pl"
    return 3, "sg"


def topic_frames(sdb: sqlite3.Connection, term: str, limit: int) -> list[dict]:
    rows = sdb.execute(
        "SELECT f.rkey, f.pattern, f.verb_lemma, f.verb_surface, f.n "
        "FROM frame_fillers ff JOIN role_frames f ON f.rkey = ff.rkey "
        "WHERE ff.term=? AND f.n>=? ORDER BY f.n DESC LIMIT ?",
        (term, MIN_FRAME_EVIDENCE, max(1, limit))).fetchall()
    return [dict(r) for r in rows]


def general_frames(sdb: sqlite3.Connection, limit: int) -> list[dict]:
    rows = sdb.execute(
        "SELECT rkey, pattern, verb_lemma, verb_surface, n "
        "FROM role_frames WHERE n>=? ORDER BY n DESC LIMIT ?",
        (MIN_FRAME_EVIDENCE, max(1, limit))).fetchall()
    return [dict(r) for r in rows]


def generate(topic: str | None, limit: int, temperature: float, min_p: float,
             seed: int | None, tries: int, frame_pool: int) -> dict:
    started = time.time()
    rng = random.Random(seed)
    pdb = pool.connect()
    sdb = syntax.connect()
    ldb = lexicon.connect()
    mdb = morph.connect()
    cdb = corpus.connect()
    try:
        stems = morph.known_stems(mdb)
        _total, df_map = pool.df_snapshot(ldb)
        vocab = set(df_map)
        poses = pool.morph_snapshot(mdb)
        compounds = syntax.load_compounds(sdb)

        anchor = ""
        frames: list[dict] = []
        if topic:
            for word in tokens(topic):
                found = topic_frames(sdb, word, frame_pool)
                if found:
                    anchor = word
                    frames = found
                    break
        if not frames:
            frames = general_frames(sdb, frame_pool)
        if not frames:
            return {"candidates": [], "reason": "no-frames",
                    "rejected": {}, "seconds": round(time.time() - started, 2)}

        order = sample_frames(frames, tries, max(0.2, temperature), rng)
        rejected: dict[str, int] = {}
        results: list[dict] = []
        seen: set[str] = set()
        cache: dict[tuple, list[dict]] = {}

        def bump(reason: str) -> None:
            rejected[reason] = rejected.get(reason, 0) + 1

        for frame in order:
            pattern = str(frame["pattern"])
            body, lemma, tense, person, _number = split_tag(pattern)
            defect = frame_defect(body)
            if defect:
                bump(defect)
                continue
            roles = [p[1:-1] for p in body
                     if p.startswith("[") and p.endswith("]")]

            fillers: dict[str, str] = {}
            missing = False
            for role in roles:
                key = (lemma, role)
                if key not in cache:
                    cache[key] = pool.candidates(pdb, lemma, role, 40)
                items = cache[key]
                if role == "AGENT" and anchor:
                    match = next((i for i in items if i["term"] == anchor), None)
                    if match:
                        fillers[role] = anchor
                        continue
                chosen = pick(items, temperature, min_p, rng)
                if chosen is None:
                    missing = True
                    break
                fillers[role] = str(chosen["term"])
            if missing:
                bump("empty-pool")
                continue
            if len(set(fillers.values())) < len(fillers):
                bump("duplicate-filler")
                continue

            want_person, want_number = subject_features(fillers["AGENT"], poses)
            if tense and person:
                verb_surface = conjugate(lemma, tense, want_person, want_number)
            else:
                verb_surface = str(frame["verb_surface"] or "")
            if not verb_surface:
                bump("no-verb-form")
                continue

            words: list[str] = []
            for token in body:
                if token.startswith("[") and token.endswith("]"):
                    words.append(lexicon.surface_for(ldb, fillers[token[1:-1]]))
                else:
                    words.append(token)
            words.extend(verb_surface.split())
            if len(set(words)) < len(words):
                bump("repeated-token")
                continue
            sentence = " ".join(words).strip() + "."
            if sentence in seen:
                bump("repeat")
                continue
            seen.add(sentence)

            info = syntax.parse(sentence, stems=stems, vocab=vocab,
                                compounds=compounds)
            if not info["verb_lemma"]:
                bump("parse-no-verb")
                continue
            if "AGENT" not in info["roles"]:
                bump("parse-no-agent")
                continue
            if structure(info["pattern"]) != structure(pattern):
                bump("structure-drift")
                continue
            if info["agreement"] and not info["agreement"]["ok"]:
                bump("agreement")
                continue

            evidence = math.log1p(float(frame["n"]))
            strength = sum(
                math.log1p(float(i["n"]))
                for role in roles
                for i in cache[(lemma, role)] if i["term"] == fillers[role]
            ) / max(1, len(roles))
            score = 0.5 * evidence + 0.5 * strength
            if lemma == morph.COPULA_LEMMA if hasattr(morph, "COPULA_LEMMA") \
                    else lemma == "بودن":
                score *= COPULA_PENALTY
            results.append({"text": sentence, "rkey": str(frame["rkey"]),
                            "verb": lemma, "roles": fillers,
                            "frame_n": float(frame["n"]),
                            "score": round(score, 4)})
            if len(results) >= limit * 12:
                break

        results.sort(key=lambda item: -item["score"])
        final: list[dict] = []
        used_verbs: set[str] = set()
        for item in results:
            if item["verb"] in used_verbs:
                continue
            keys = " ".join(tokens(item["text"]))
            if cdb.execute("SELECT 1 FROM docs WHERE keys=? LIMIT 1",
                           (keys,)).fetchone() is not None:
                bump("already-in-corpus")
                continue
            used_verbs.add(item["verb"])
            final.append(item)
            if len(final) >= limit:
                break

        return {"topic": anchor, "candidates": final, "rejected": rejected,
                "seconds": round(time.time() - started, 2)}
    finally:
        cdb.close(); mdb.close(); ldb.close(); sdb.close(); pdb.close()


def record(db: sqlite3.Connection, topic: str, report: dict) -> None:
    now = time.time()
    db.executemany(
        "INSERT INTO offers(ts,topic,rkey,text,score) VALUES(?,?,?,?,?)",
        [(now, topic, i["rkey"], i["text"], i["score"])
         for i in report.get("candidates", [])])
    db.commit()


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="farsi.speak",
                                description="تولید جمله بر پایهٔ نقش")
    sub = p.add_subparsers(dest="command", required=True)
    say = sub.add_parser("say", help="ساخت جمله")
    say.add_argument("topic", nargs="?", default=None)
    say.add_argument("--limit", type=int, default=3)
    say.add_argument("--temperature", type=float, default=0.8)
    say.add_argument("--min-p", type=float, default=0.05)
    say.add_argument("--tries", type=int, default=DEFAULT_TRIES)
    say.add_argument("--frame-pool", type=int, default=DEFAULT_FRAME_POOL)
    say.add_argument("--seed", type=int, default=None)
    ck = sub.add_parser("check", help="بررسی ساختاری یک جمله")
    ck.add_argument("sentence")
    sub.add_parser("history", help="آخرین جمله‌های ثبت‌شده")
    args = p.parse_args(argv)

    db = connect()
    try:
        if args.command == "say":
            report = generate(args.topic, args.limit, args.temperature,
                              args.min_p, args.seed, args.tries,
                              args.frame_pool)
            record(db, args.topic or "", report)
            if not report["candidates"]:
                print("جمله‌ای ساخته نشد.", report.get("reason", ""))
                print("رد شده‌ها:", report.get("rejected", {}))
                return 0
            for index, item in enumerate(report["candidates"], 1):
                print(f"\n{index}. {item['text']}")
                print(f"   فعل: {item['verb']} | شواهد: {item['frame_n']} "
                      f"| امتیاز: {item['score']}")
                print(f"   نقش‌ها: {item['roles']}")
            print("\nرد شده‌ها:", report.get("rejected", {}))
            print("زمان:", report["seconds"])
            return 0
        if args.command == "check":
            sdb = syntax.connect()
            ldb = lexicon.connect()
            mdb = morph.connect()
            try:
                stems = morph.known_stems(mdb)
                _t, df_map = pool.df_snapshot(ldb)
                info = syntax.parse(args.sentence, stems=stems,
                                    vocab=set(df_map),
                                    compounds=syntax.load_compounds(sdb))
            finally:
                mdb.close(); ldb.close(); sdb.close()
            print("الگو:", info["pattern"])
            print("فعل:", info["verb_lemma"])
            for block in info["blocks"]:
                print(f"  {block['role']:<8} {block['surface']}")
            print("تطابق:", info["agreement"])
            return 0
        for row in db.execute("SELECT id, text, score FROM offers "
                              "ORDER BY id DESC LIMIT 10"):
            print(f"#{row['id']} {row['text']}  ({row['score']})")
        return 0
    except sqlite3.Error as exc:
        print("توقف:", exc)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())