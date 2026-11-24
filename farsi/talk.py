"""Two-candidate reply offers and preference ratings.

This layer sits on top of intent and nlg. It does not invent text; it only
selects existing fillable templates, stores the offer, and feeds preference
signals back to template weights.
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

from agent.config import STATE_DIR, assert_writable, ensure_dirs
from agent.errors import AgentError

from . import intent, nlg, usage
from .text import normalize


TALK_DB = STATE_DIR / "farsi_talk.db"

_SCHEMA = """
CREATE TABLE IF NOT EXISTS offers(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    ts REAL,
    user_text TEXT,
    intent TEXT,
    route TEXT,
    status TEXT DEFAULT 'open',
    chosen INTEGER
);

CREATE TABLE IF NOT EXISTS offer_candidates(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id INTEGER NOT NULL,
    position INTEGER NOT NULL,
    template_key TEXT NOT NULL,
    text TEXT NOT NULL,
    score REAL,
    weight REAL,
    rating REAL,
    status TEXT DEFAULT 'pending'
);

CREATE INDEX IF NOT EXISTS idx_offer_candidates_offer
    ON offer_candidates(offer_id);
"""


def connect(path: str | Path | None = None) -> sqlite3.Connection:
    ensure_dirs()
    target = assert_writable(Path(path) if path else TALK_DB)
    target.parent.mkdir(parents=True, exist_ok=True)
    db = sqlite3.connect(str(target))
    db.row_factory = sqlite3.Row
    db.executescript(_SCHEMA)
    return db


def _intent_chain(decision: dict) -> list[str]:
    kind = decision.get("kind") or "conversation"
    route = decision.get("route") or ""

    if kind == "unknown":
        return ["unknown"]
    if kind == "action":
        return [route, "action"] if route else ["action"]
    return [route, "conversation"] if route else ["conversation"]


def _context_for(decision: dict, ndb: sqlite3.Connection) -> dict:
    context = {
        "title": decision.get("title") or "",
        "text": decision.get("text") or "",
    }
    context.update(decision.get("values") or {})

    if decision.get("argv"):
        context["command"] = " ".join(str(part) for part in decision["argv"][1:])

    if decision.get("missing"):
        context["missing"] = ", ".join(decision["missing"])

    if decision.get("route") == "talk.explain":
        fact = nlg.find_fact(ndb, decision.get("text") or "")
        if fact:
            context["fact"] = fact["text"]

    return context


def _candidates_for(ndb: sqlite3.Connection, udb, decision: dict,
                    limit: int = 2) -> list[dict]:
    chain = _intent_chain(decision)
    context = _context_for(decision, ndb)
    pool = nlg.candidates(ndb, udb, chain, context)

    if not pool and decision.get("kind") != "unknown":
        pool = nlg.candidates(ndb, udb, ["conversation"], context)

    seen: set[str] = set()
    selected: list[dict] = []

    for item in pool:
        text = str(item.get("text") or "").strip()
        if not text:
            continue

        key = normalize(text)
        if key in seen:
            continue

        seen.add(key)
        selected.append(item)

        if len(selected) >= limit:
            break

    return selected


def propose(tdb: sqlite3.Connection, ndb: sqlite3.Connection,
            udb, idb: sqlite3.Connection, text: str,
            *, learn: bool = False) -> dict:
    decision = intent.classify(str(text or "").strip(), idb, udb, learn=learn)
    candidates = _candidates_for(ndb, udb, decision)

    if not candidates:
        return {
            "offer_id": None,
            "decision": decision,
            "candidates": [],
            "message": "برای این مورد هنوز پاسخی یاد نگرفته‌ام.",
        }

    now = time.time()
    cursor = tdb.execute(
        "INSERT INTO offers(ts, user_text, intent, route, status) "
        "VALUES(?,?,?,?,?)",
        (
            now,
            decision.get("text") or str(text),
            decision.get("kind") or "",
            decision.get("route") or "",
            "open",
        ),
    )
    offer_id = int(cursor.lastrowid)

    output_candidates = []

    for position, item in enumerate(candidates, start=1):
        tdb.execute(
            "INSERT INTO offer_candidates"
            "(offer_id, position, template_key, text, score, weight, status) "
            "VALUES(?,?,?,?,?,?,?)",
            (
                offer_id,
                position,
                str(item["key"]),
                str(item["text"]),
                float(item.get("score") or 0.0),
                float(item.get("weight") or 0.0),
                "pending",
            ),
        )

        output_candidates.append({
            "position": position,
            "key": str(item["key"]),
            "text": str(item["text"]),
            "score": round(float(item.get("score") or 0.0), 4),
            "weight": round(float(item.get("weight") or 0.0), 3),
        })

    tdb.commit()

    return {
        "offer_id": offer_id,
        "decision": decision,
        "candidates": output_candidates,
        "message": "",
    }


def _fetch_candidates(tdb: sqlite3.Connection, offer_id: int) -> list[dict]:
    rows = tdb.execute(
        "SELECT * FROM offer_candidates WHERE offer_id=? ORDER BY position",
        (int(offer_id),),
    ).fetchall()
    return [dict(row) for row in rows]


def _apply_feedback(ndb: sqlite3.Connection, accepted_keys: set[str],
                    rejected_keys: set[str]) -> dict:
    weights: dict[str, float | None] = {}

    for key in accepted_keys:
        try:
            weights[key] = nlg.feedback(ndb, key, True)
        except AgentError:
            weights[key] = None

    for key in rejected_keys - accepted_keys:
        try:
            weights[key] = nlg.feedback(ndb, key, False)
        except AgentError:
            weights[key] = None

    return weights


def rate_offer(tdb: sqlite3.Connection, ndb: sqlite3.Connection,
               offer_id: int, *, choice: int | None = None,
               scores: dict[int, float] | None = None) -> dict:
    offer = tdb.execute(
        "SELECT * FROM offers WHERE id=?", (int(offer_id),)
    ).fetchone()

    if offer is None:
        raise AgentError(f"پیشنهاد {offer_id} پیدا نشد")

    candidates = _fetch_candidates(tdb, int(offer_id))
    if not candidates:
        raise AgentError(f"پیشنهاد {offer_id} نامزدی ندارد")

    if choice is None and not scores:
        raise AgentError("یکی از پارامترهای choice یا scores لازم است")

    result = {
        "offer_id": int(offer_id),
        "mode": "choice" if choice is not None else "scores",
        "updates": [],
        "weights": {},
    }

    if choice is not None:
        if choice not in (1, 2):
            raise AgentError("choice باید 1 یا 2 باشد")

        chosen = next(
            (item for item in candidates if item["position"] == choice),
            None,
        )
        if chosen is None:
            raise AgentError(f"نامزد {choice} در این پیشنهاد وجود ندارد")

        accepted_keys = {chosen["template_key"]}
        rejected_keys = {
            item["template_key"]
            for item in candidates
            if item["position"] != choice
        } - accepted_keys

        weights = _apply_feedback(ndb, accepted_keys, rejected_keys)
        result["weights"] = weights

        for item in candidates:
            if item["position"] == choice:
                rating = 1.0
            elif item["template_key"] in accepted_keys:
                rating = 0.0
            else:
                rating = -1.0

            tdb.execute(
                "UPDATE offer_candidates SET rating=?, status='rated' WHERE id=?",
                (rating, item["id"]),
            )

            result["updates"].append({
                "position": item["position"],
                "key": item["template_key"],
                "rating": rating,
            })

        tdb.execute(
            "UPDATE offers SET status='rated', chosen=? WHERE id=?",
            (int(choice), int(offer_id)),
        )
        tdb.commit()
        return result

    clean_scores: dict[int, float] = {}

    for raw_position, raw_value in (scores or {}).items():
        try:
            position = int(raw_position)
            value = float(raw_value)
        except (TypeError, ValueError):
            raise AgentError("نمره باید عدد باشد")

        if position not in (1, 2):
            raise AgentError("شماره نامزد باید 1 یا 2 باشد")

        if not 0.0 <= value <= 5.0:
            raise AgentError("نمره باید بین 0 تا 5 باشد")

        clean_scores[position] = value

    if not clean_scores:
        raise AgentError("هیچ نمره‌ای داده نشد")

    key_scores: dict[str, list[float]] = {}
    best_position = None
    best_value = -1.0

    for item in candidates:
        value = clean_scores.get(item["position"])
        if value is None:
            continue

        key_scores.setdefault(item["template_key"], []).append(value)

        if value > best_value:
            best_value = value
            best_position = item["position"]

    accepted_keys = {
        key
        for key, values in key_scores.items()
        if sum(values) / len(values) >= 4.0
    }

    rejected_keys = {
        key
        for key, values in key_scores.items()
        if sum(values) / len(values) <= 2.0
    } - accepted_keys

    weights = _apply_feedback(ndb, accepted_keys, rejected_keys)
    result["weights"] = weights

    for item in candidates:
        value = clean_scores.get(item["position"])
        if value is None:
            continue

        tdb.execute(
            "UPDATE offer_candidates SET rating=?, status='rated' WHERE id=?",
            (float(value), item["id"]),
        )

        result["updates"].append({
            "position": item["position"],
            "key": item["template_key"],
            "rating": value,
        })

    tdb.execute(
        "UPDATE offers SET status='rated', chosen=? WHERE id=?",
        (best_position, int(offer_id)),
    )
    tdb.commit()

    return result


def list_offers(tdb: sqlite3.Connection, limit: int = 10) -> list[dict]:
    rows = tdb.execute(
        "SELECT * FROM offers ORDER BY id DESC LIMIT ?",
        (int(limit),),
    ).fetchall()

    output = []
    for row in rows:
        item = dict(row)
        item["candidates"] = _fetch_candidates(tdb, int(item["id"]))
        output.append(item)

    return output


def stats(tdb: sqlite3.Connection) -> dict:
    total = tdb.execute("SELECT COUNT(*) AS n FROM offers").fetchone()["n"]
    rated = tdb.execute(
        "SELECT COUNT(*) AS n FROM offers WHERE status='rated'"
    ).fetchone()["n"]

    return {
        "offers": int(total or 0),
        "rated": int(rated or 0),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.talk",
        description="پیشنهاد پاسخ دوگزینه‌ای و دریافت امتیاز",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    say = sub.add_parser("say", help="پیشنهاد پاسخ برای یک متن")
    say.add_argument("text")
    say.add_argument("--learn", action="store_true")

    rate = sub.add_parser("rate", help="امتیاز دادن به یک پیشنهاد")
    rate.add_argument("offer_id", type=int)
    rate.add_argument("--choice", type=int, choices=(1, 2), default=None)
    rate.add_argument(
        "--score",
        action="append",
        default=[],
        help="مثال: --score 1=5 --score 2=2",
    )

    offers_parser = sub.add_parser("offers", help="فهرست پیشنهادهای اخیر")
    offers_parser.add_argument("--limit", type=int, default=10)

    sub.add_parser("stats", help="آمار پیشنهادها")

    args = parser.parse_args(argv)

    tdb = connect()
    ndb = nlg.prepare()
    udb = usage.connect()
    idb = intent.prepare()

    try:
        if args.command == "say":
            report = propose(tdb, ndb, udb, idb, args.text, learn=args.learn)

            if report["offer_id"] is None:
                print(report["message"])
                return 0

            decision = report["decision"]
            print(
                f"پیشنهاد #{report['offer_id']} | "
                f"قصد: {decision.get('kind')} / {decision.get('route') or '-'}"
            )

            for candidate in report["candidates"]:
                print(f"{candidate['position']}. {candidate['text']}")
                print(
                    "   "
                    f"قالب: {candidate['key']} | "
                    f"امتیاز: {candidate['score']} | "
                    f"وزن: {candidate['weight']}"
                )

            print("\nبرای امتیاز دادن:")
            print(f"python -m farsi.talk rate {report['offer_id']} --choice 1")
            print(
                f"python -m farsi.talk rate {report['offer_id']} "
                "--score 1=5 --score 2=3"
            )
            return 0

        if args.command == "rate":
            scores: dict[int, float] = {}

            for item in args.score:
                if "=" not in item:
                    raise AgentError("فرمت score باید مثل 1=5 باشد")

                position, value = item.split("=", 1)
                scores[int(position)] = float(value)

            if args.choice is not None and scores:
                raise AgentError("فقط یکی از --choice یا --score استفاده شود")

            report = rate_offer(
                tdb,
                ndb,
                args.offer_id,
                choice=args.choice,
                scores=scores or None,
            )

            print("به‌روزرسانی:", report["updates"])
            print("وزن‌ها:", report["weights"])
            return 0

        if args.command == "offers":
            for item in list_offers(tdb, args.limit):
                print(
                    f"#{item['id']} [{item['status']}] "
                    f"انتخاب‌شده: {item['chosen'] or '-'} | "
                    f"متن: {str(item['user_text'])[:60]}"
                )

                for candidate in item["candidates"]:
                    print(
                        f"  {candidate['position']}. "
                        f"{str(candidate['text'])[:70]} | "
                        f"rating={candidate['rating']}"
                    )

            return 0

        print(stats(tdb))
        return 0

    except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
        print("توقف:", exc)
        return 1

    finally:
        tdb.close()
        ndb.close()
        udb.close()
        idb.close()


if __name__ == "__main__":
    raise SystemExit(main())