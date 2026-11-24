"""Persian morphology: stem, affixes, person/number, tense, negation, mood.

Additive layer. farsi.text.tokens() is untouched, so existing databases stay
valid. Affix inventories and irregular forms are linguistic constants; regular
verb stems are learned from corpus prefix evidence.

Two decisions matter for correctness:
  * tense comes from the STEM, not the prefix: نمی‌رفتند is past imperfective
  * a bare past stem is only a verb in sentence-final position, because
    شکست / نشست / داد / برد are also deverbal nouns
"""
from __future__ import annotations

import argparse
import sqlite3
import sys
import time
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, r"D:\Python")
sys.path.insert(0, r"D:\project\library")

from agent.config import STATE_DIR, assert_writable, ensure_dirs

from . import usage
from .text import ZWNJ

MORPH_DB = STATE_DIR / "farsi_morph.db"

MIN_LEARNED_EVIDENCE = 3
HAMZA = "\u0654"
ALEF_HAMZA = "\u06c0"
TANWIN = "\u064b"

PAST_TO_PRESENT = {
    "کرد": "کن", "رفت": "رو", "گفت": "گو", "دید": "بین", "شنید": "شنو",
    "خورد": "خور", "داد": "ده", "زد": "زن", "برد": "بر", "آورد": "آور",
    "گرفت": "گیر", "یافت": "یاب", "شد": "شو", "بود": "باش", "داشت": "دار",
    "گذاشت": "گذار", "نوشت": "نویس", "خواند": "خوان", "ساخت": "ساز",
    "شناخت": "شناس", "فروخت": "فروش", "خرید": "خر", "مرد": "میر",
    "جست": "جو", "بست": "بند", "شکست": "شکن", "نشست": "نشین",
    "ایستاد": "ایست", "افتاد": "افت", "فرستاد": "فرست", "رسید": "رس",
    "کشید": "کش", "پوشید": "پوش", "نوشید": "نوش", "بخشید": "بخش",
    "خندید": "خند", "گریست": "گری", "دانست": "دان", "توانست": "توان",
    "خواست": "خواه", "آموخت": "آموز", "سپرد": "سپار", "شمرد": "شمار",
    "نگاشت": "نگار", "گزارد": "گزار", "گماشت": "گمار", "پنداشت": "پندار",
    "انگاشت": "انگار", "پذیرفت": "پذیر", "شتافت": "شتاب", "بافت": "باف",
    "کاست": "کاه", "خاست": "خیز", "آراست": "آرای", "زدود": "زدای",
    "آزمود": "آزمای", "افزود": "افزای", "سرود": "سرای", "نمود": "نمای",
    "گشود": "گشای", "فرمود": "فرمای", "پیمود": "پیمای", "ربود": "ربای",
    "کاشت": "کار", "نگریست": "نگر", "زیست": "زی", "گریخت": "گریز",
    "ریخت": "ریز", "سوخت": "سوز", "دوخت": "دوز", "انداخت": "انداز",
    "پرداخت": "پرداز", "تاخت": "تاز", "نواخت": "نواز", "گداخت": "گداز",
    "انگیخت": "انگیز", "آمیخت": "آمیز", "آویخت": "آویز", "گسیخت": "گسل",
}
PAST_TO_PRESENT.update({
    "آمد": "آ", "گردید": "گرد", "رساند": "رسان", "درآمد": "درآ",
    "پیوست": "پیوند", "گسست": "گسل", "باخت": "باز", "فشرد": "فشار",
    "گسترد": "گستر", "پرورد": "پرور", "آزرد": "آزار", "چشید": "چش",
    "ترسید": "ترس", "لرزید": "لرز", "فهمید": "فهم", "پرسید": "پرس",
    "آفرید": "آفرین", "چرخید": "چرخ", "پیچید": "پیچ", "سنجید": "سنج",
    "گزید": "گزین", "رست": "ره", "طلبید": "طلب", "آسود": "آسا",
    "افسرد": "افسر", "پژمرد": "پژمر",
})
PRESENT_TO_PAST = {v: k for k, v in PAST_TO_PRESENT.items()}
LIGHT_VERBS = {"کرد", "شد", "داد", "زد", "خورد", "برد", "آورد", "گرفت",
               "یافت", "بود", "نمود", "گردید", "گشت", "فرمود", "داشت",
               "کن", "شو", "ده", "زن", "خور", "بر", "آور", "گیر", "یاب",
               "باش", "نمای", "دار"}
IRREGULAR_ADJ = {"به": "خوب"}
COPULA = {
    "است": (3, "sg", "pres", False), "هست": (3, "sg", "pres", False),
    "هستم": (1, "sg", "pres", False), "هستی": (2, "sg", "pres", False),
    "هستیم": (1, "pl", "pres", False), "هستید": (2, "pl", "pres", False),
    "هستند": (3, "pl", "pres", False),
    "نیست": (3, "sg", "pres", True), "نیستم": (1, "sg", "pres", True),
    "نیستی": (2, "sg", "pres", True), "نیستیم": (1, "pl", "pres", True),
    "نیستید": (2, "pl", "pres", True), "نیستند": (3, "pl", "pres", True),
    "بود": (3, "sg", "past", False), "بودم": (1, "sg", "past", False),
    "بودی": (2, "sg", "past", False), "بودیم": (1, "pl", "past", False),
    "بودید": (2, "pl", "past", False), "بودند": (3, "pl", "past", False),
    "شد": (3, "sg", "past", False), "شدم": (1, "sg", "past", False),
    "شدی": (2, "sg", "past", False), "شدیم": (1, "pl", "past", False),
    "شدید": (2, "pl", "past", False), "شدند": (3, "pl", "past", False),
    "ام": (1, "sg", "pres", False), "ای": (2, "sg", "pres", False),
    "ایم": (1, "pl", "pres", False), "اید": (2, "pl", "pres", False),
    "اند": (3, "pl", "pres", False),
}
ENDINGS = (("یم", 1, "pl"), ("ید", 2, "pl"), ("ند", 3, "pl"),
           ("م", 1, "sg"), ("ی", 2, "sg"), ("د", 3, "sg"))
PAST_ENDINGS = (("یم", 1, "pl"), ("ید", 2, "pl"), ("ند", 3, "pl"),
                ("م", 1, "sg"), ("ی", 2, "sg"))
ENCLITICS = (("مان", 1, "pl"), ("تان", 2, "pl"), ("شان", 3, "pl"),
             ("م", 1, "sg"), ("ت", 2, "sg"), ("ش", 3, "sg"))
SAFE_PLURALS = ("های", "ها", "گان", "جات")
VOCAB_PLURALS = ("یان", "ان", "ات")
PRONOUNS = {
    "من": (1, "sg"), "ما": (1, "pl"), "تو": (2, "sg"), "شما": (2, "pl"),
    "او": (3, "sg"), "وی": (3, "sg"), "ایشان": (3, "pl"), "آنها": (3, "pl"),
    "آنان": (3, "pl"), "خود": (3, "sg"), "خودم": (1, "sg"),
    "خودت": (2, "sg"), "خودش": (3, "sg"), "خودمان": (1, "pl"),
    "خودتان": (2, "pl"), "خودشان": (3, "pl"),
}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS learned_stems(
    stem TEXT PRIMARY KEY, kind TEXT, evidence REAL, updated_ts REAL);
CREATE TABLE IF NOT EXISTS cache(
    term TEXT PRIMARY KEY, stem TEXT, lemma TEXT, pos TEXT,
    person INTEGER, number TEXT, tense TEXT, mood TEXT,
    negated INTEGER, ezafe INTEGER, affixes TEXT, updated_ts REAL);
CREATE TABLE IF NOT EXISTS meta(mkey TEXT PRIMARY KEY, mvalue TEXT);
"""


@dataclass(slots=True)
class Morph:
    surface: str
    stem: str
    lemma: str = ""
    pos: str = "UNK"
    person: int = 0
    number: str = ""
    tense: str = ""
    mood: str = "indic"
    negated: bool = False
    ezafe: bool = False
    verbal_stem: bool = False
    affixes: tuple[str, ...] = ()

    @property
    def is_verb(self) -> bool:
        return self.pos == "V"

    @property
    def is_light(self) -> bool:
        return self.is_verb and self.stem in LIGHT_VERBS


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = assert_writable(Path(path) if path else MORPH_DB)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    return db


def fold(word: str) -> str:
    return word.replace(ZWNJ, "").strip()


def strip_ezafe(word: str) -> tuple[str, bool]:
    if word.endswith(HAMZA):
        return word[:-1], True
    if word.endswith(ALEF_HAMZA):
        return word[:-1] + "ه", True
    if word.endswith("هٔ"):
        return word[:-1], True
    return word, False


def known_stems(db: sqlite3.Connection | None) -> set[str]:
    stems = set(PAST_TO_PRESENT) | set(PRESENT_TO_PAST)
    if db is not None:
        stems.update(str(r["stem"]) for r in
                     db.execute("SELECT stem FROM learned_stems WHERE evidence>=?",
                                (MIN_LEARNED_EVIDENCE,)))
    return stems


def load_vocab(udb: sqlite3.Connection, limit: int = 300000) -> set[str]:
    return {str(r["term"]) for r in
            udb.execute("SELECT term FROM terms ORDER BY freq DESC LIMIT ?",
                        (limit,))}


def lemma_of(stem: str) -> str:
    if stem in PAST_TO_PRESENT:
        return stem + "ن"
    if stem in PRESENT_TO_PAST:
        return PRESENT_TO_PAST[stem] + "ن"
    return stem + "ن"


def analyze(word: str, *, stems: set[str] | None = None,
            vocab: set[str] | None = None, final: bool = False) -> Morph:
    surface = str(word or "").strip()
    if not surface:
        return Morph("", "")
    base, ezafe = strip_ezafe(surface)
    flat = fold(base)
    if not flat:
        return Morph(surface, "")
    known = stems if stems is not None else (set(PAST_TO_PRESENT) | set(PRESENT_TO_PAST))

    if flat in PRONOUNS:
        person, number = PRONOUNS[flat]
        return Morph(surface, flat, flat, "PRON", person, number, ezafe=ezafe)

    if flat in COPULA:
        person, number, tense, neg = COPULA[flat]
        return Morph(surface, "بود", "بودن", "V", person, number, tense,
                     "indic", neg, ezafe, False, ("copula",))

    for prefix in ("نمی", "می", "ب", "ن"):
        if not flat.startswith(prefix) or len(flat) <= len(prefix) + 1:
            continue
        body = flat[len(prefix):]
        neg = prefix in ("ن", "نمی")
        mood = "subj" if prefix == "ب" else "indic"
        for ending, person, number in ENDINGS:
            if not body.endswith(ending) or len(body) <= len(ending):
                continue
            stem = body[:-len(ending)]
            if stem in PAST_TO_PRESENT:
                return Morph(surface, stem, lemma_of(stem), "V", person, number,
                             "past", mood, neg, ezafe, True, (prefix, ending))
            if stem in PRESENT_TO_PAST or stem in known:
                return Morph(surface, stem, lemma_of(stem), "V", person, number,
                             "pres", mood, neg, ezafe, True, (prefix, ending))
        if body.endswith("ه") and len(body) >= 3 and body[:-1] in PAST_TO_PRESENT:
            stem = body[:-1]
            return Morph(surface, stem, lemma_of(stem), "V", 0, "", "past",
                         "part", neg, ezafe, True, (prefix, "ه"))
        if body in PAST_TO_PRESENT:
            return Morph(surface, body, lemma_of(body), "V", 3, "sg", "past",
                         mood, neg, ezafe, True, (prefix,))
        if body in PRESENT_TO_PAST or body in known:
            tense = "past" if prefix == "ن" else "pres"
            return Morph(surface, body, lemma_of(body), "V", 0, "", tense,
                         mood, neg, ezafe, True, (prefix,))

    if flat in PAST_TO_PRESENT:
        if final:
            return Morph(surface, flat, lemma_of(flat), "V", 3, "sg", "past",
                         "indic", False, ezafe, True, ())
        return Morph(surface, flat, lemma_of(flat), "N", 0, "", "", "indic",
                     False, ezafe, True, ())

    if flat.endswith("ن") and len(flat) > 3 and flat[:-1] in known:
        stem = flat[:-1]
        return Morph(surface, stem, lemma_of(stem), "V", 0, "", "", "inf",
                     False, ezafe, True, ("ن",))

    if flat.endswith("نده") and len(flat) > 4:
        stem = flat[:-3]
        if stem in PRESENT_TO_PAST or stem in known:
            return Morph(surface, stem, lemma_of(stem), "N", 0, "", "",
                         "agentive", False, ezafe, False, ("نده",))

    if flat.endswith("ه") and len(flat) >= 3:
        stem = flat[:-1]
        if stem in PAST_TO_PRESENT or stem in known:
            return Morph(surface, stem, lemma_of(stem), "V", 0, "", "past",
                         "part", False, ezafe, True, ("ه",))

    for ending, person, number in PAST_ENDINGS:
        if flat.endswith(ending) and len(flat) > len(ending) + 1:
            stem = flat[:-len(ending)]
            if stem in PAST_TO_PRESENT:
                return Morph(surface, stem, lemma_of(stem), "V", person, number,
                             "past", "indic", False, ezafe, True, (ending,))
    for ending, person, number in ENDINGS:
        if flat.endswith(ending) and len(flat) > len(ending) + 1:
            stem = flat[:-len(ending)]
            if stem in PRESENT_TO_PAST:
                return Morph(surface, stem, lemma_of(stem), "V", person, number,
                             "pres", "indic", False, ezafe, True, (ending,))

    if flat.endswith(TANWIN):
        stem = flat[:-1]
        if stem.endswith("ا"):
            stem = stem[:-1]
        return Morph(surface, stem, stem, "ADV", affixes=("اً",))

    affixes: list[str] = []
    stem = flat
    pos = "N" if ezafe else "UNK"

    if stem.endswith("ترین") and len(stem) > 5:
        stem, pos = stem[:-4], "ADJ"
        affixes.append("ترین")
    elif stem.endswith("تر") and len(stem) > 4:
        stem, pos = stem[:-2], "ADJ"
        affixes.append("تر")
    if stem in IRREGULAR_ADJ:
        stem = IRREGULAR_ADJ[stem]

    for plural in SAFE_PLURALS:
        if stem.endswith(plural) and len(stem) > len(plural) + 2:
            candidate = stem[:-len(plural)]
            if plural == "گان":
                candidate += "ه"
            stem = candidate
            pos = "N"
            affixes.append(plural)
            break
    else:
        if stem.endswith("یان") and len(stem) > 5:
            candidate = stem[:-3]
            if candidate and candidate[-1] in "وا" and (
                    vocab is None or candidate in vocab):
                stem, pos = candidate, "N"
                affixes.append("یان")
        if not affixes:
            for plural in VOCAB_PLURALS:
                if plural == "یان" or not stem.endswith(plural):
                    continue
                if len(stem) <= len(plural) + 2:
                    continue
                candidate = stem[:-len(plural)]
                if vocab is not None and candidate not in vocab:
                    continue
                stem, pos = candidate, "N"
                affixes.append(plural)
                break

    for clitic, person, number in ENCLITICS:
        if stem.endswith(clitic) and len(stem) > len(clitic) + 2:
            stem = stem[:-len(clitic)]
            affixes.append(clitic)
            return Morph(surface, stem, stem, pos if pos != "UNK" else "N",
                         person, number, "", "indic", False, ezafe, False,
                         tuple(affixes))

    number = "pl" if any(a in SAFE_PLURALS or a in VOCAB_PLURALS
                         for a in affixes) else ""
    return Morph(surface, stem, stem, pos, 0, number, "", "indic", False,
                 ezafe, False, tuple(affixes))


def harvest(db: sqlite3.Connection, udb: sqlite3.Connection,
            limit: int = 400000) -> dict:
    started = time.time()
    found: dict[str, float] = {}
    for row in udb.execute("SELECT term, freq FROM terms ORDER BY freq DESC "
                           "LIMIT ?", (limit,)):
        flat = fold(str(row["term"]))
        for prefix in ("نمی", "می"):
            if not flat.startswith(prefix) or len(flat) <= len(prefix) + 1:
                continue
            body = flat[len(prefix):]
            freq = float(row["freq"])
            for ending, _, _ in ENDINGS:
                if body.endswith(ending) and len(body) > len(ending) + 1:
                    stem = body[:-len(ending)]
                    if len(stem) >= 2:
                        found[stem] = found.get(stem, 0.0) + freq
                    break
            else:
                if len(body) >= 2:
                    found[body] = found.get(body, 0.0) + freq
            break

    now = time.time()
    db.executemany(
        "INSERT INTO learned_stems(stem,kind,evidence,updated_ts) "
        "VALUES(?,?,?,?) ON CONFLICT(stem) DO UPDATE SET "
        "evidence=MAX(evidence,excluded.evidence), updated_ts=excluded.updated_ts",
        [(s, "verb", f, now) for s, f in found.items()])
    db.commit()
    return {"candidates": len(found),
            "accepted": sum(1 for f in found.values() if f >= MIN_LEARNED_EVIDENCE),
            "seconds": round(time.time() - started, 2)}


def warm(db: sqlite3.Connection, udb: sqlite3.Connection, *,
         limit: int = 200000, rebuild: bool = False) -> dict:
    started = time.time()
    if rebuild:
        db.execute("DELETE FROM cache")
        db.commit()
    stems = known_stems(db)
    vocab = load_vocab(udb)
    now = time.time()
    batch = []
    for row in udb.execute("SELECT term FROM terms ORDER BY freq DESC LIMIT ?",
                           (limit,)):
        term = str(row["term"])
        m = analyze(term, stems=stems, vocab=vocab)
        batch.append((term, m.stem, m.lemma, m.pos, m.person, m.number,
                      m.tense, m.mood, int(m.negated), int(m.ezafe),
                      " ".join(m.affixes), now))
    db.executemany(
        "INSERT INTO cache(term,stem,lemma,pos,person,number,tense,mood,"
        "negated,ezafe,affixes,updated_ts) VALUES(?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(term) DO UPDATE SET stem=excluded.stem, "
        "lemma=excluded.lemma, pos=excluded.pos, person=excluded.person, "
        "number=excluded.number, tense=excluded.tense, mood=excluded.mood, "
        "negated=excluded.negated, ezafe=excluded.ezafe, "
        "affixes=excluded.affixes, updated_ts=excluded.updated_ts", batch)
    db.commit()
    return {"cached": len(batch), "seconds": round(time.time() - started, 2)}


def stats(db: sqlite3.Connection) -> dict:
    learned = db.execute("SELECT COUNT(*) AS n FROM learned_stems "
                         "WHERE evidence>=?", (MIN_LEARNED_EVIDENCE,)).fetchone()["n"]
    cached = db.execute("SELECT COUNT(*) AS n FROM cache").fetchone()["n"]
    verbs = db.execute("SELECT COUNT(*) AS n FROM cache WHERE pos='V'").fetchone()["n"]
    return {"learned_stems": int(learned), "cached": int(cached),
            "cached_verbs": int(verbs)}


SELFTEST = ["می‌رود", "نمی‌رفتند", "برو", "گفتم", "کتابم", "کتاب‌ها", "مردان",
            "بهترین", "خوب‌تر", "سریعاً", "رفته", "کننده", "رفتن", "است",
            "نیستند", "بودیم", "خانهٔ", "دانشجویان", "می‌کنیم", "شما",
            "داد", "شکست", "پرداختند", "می‌شد"]


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="farsi.morph", description="تحلیل صرفی فارسی")
    sub = p.add_subparsers(dest="command", required=True)
    sub.add_parser("selftest", help="تحلیل نمونه‌های ثابت")
    w = sub.add_parser("word", help="تحلیل یک واژه")
    w.add_argument("word")
    w.add_argument("--final", action="store_true")
    h = sub.add_parser("harvest", help="یادگیری بن فعل از پیکره")
    h.add_argument("--limit", type=int, default=400000)
    c = sub.add_parser("warm", help="پیش‌محاسبهٔ تحلیل واژه‌ها")
    c.add_argument("--limit", type=int, default=200000)
    c.add_argument("--rebuild", action="store_true")
    sub.add_parser("stats", help="آمار")
    args = p.parse_args(argv)

    db = connect()
    try:
        if args.command in ("selftest", "word"):
            stems = known_stems(db)
            vocab = None
            try:
                udb = usage.connect()
                try:
                    vocab = load_vocab(udb, 100000)
                finally:
                    udb.close()
            except sqlite3.Error:
                print("(واژگان در دسترس نبود؛ جمع‌های مبهم بررسی نمی‌شوند)")
            words = SELFTEST if args.command == "selftest" else [args.word]
            for word in words:
                final = True if args.command == "word" and args.final else False
                m = analyze(word, stems=stems, vocab=vocab, final=final)
                print(f"{word:<14} stem={m.stem:<11} pos={m.pos:<5} "
                      f"lemma={m.lemma:<11} {m.person}{m.number:<3} "
                      f"{m.tense:<5} {m.mood:<9} neg={int(m.negated)} "
                      f"ez={int(m.ezafe)} affix={','.join(m.affixes)}")
            return 0

        udb = usage.connect()
        try:
            if args.command == "harvest":
                print(harvest(db, udb, args.limit))
            elif args.command == "warm":
                print(warm(db, udb, limit=args.limit, rebuild=args.rebuild))
        finally:
            udb.close()
        print(stats(db))
        return 0
    except sqlite3.Error as exc:
        print("توقف:", exc)
        return 1
    finally:
        db.close()


if __name__ == "__main__":
    raise SystemExit(main())