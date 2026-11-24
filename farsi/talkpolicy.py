"""Preference learning and auto-selection policy for talk offers.

This module keeps the original talk.py unchanged. It records preference
signals in the same talk database, aggregates them per template/route, and
decides when a response can be chosen automatically.
"""
from __future__ import annotations

import argparse
import sqlite3
import time

from agent.errors import AgentError

from . import intent, nlg, talk, usage


_POLICY_SCHEMA = """
CREATE TABLE IF NOT EXISTS policy_signals(
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    offer_id INTEGER NOT NULL,
    template_key TEXT NOT NULL,
    route TEXT,
    accepted INTEGER DEFAULT 0,
    rejected INTEGER DEFAULT 0,
    neutral INTEGER DEFAULT 0,
    score REAL,
    ts REAL
);

CREATE TABLE IF NOT EXISTS template_policy(
    template_key TEXT PRIMARY KEY,
    rated_offers INTEGER DEFAULT 0,
    positive INTEGER DEFAULT 0,
    negative INTEGER DEFAULT 0,
    neutral INTEGER DEFAULT 0,
    score_sum REAL DEFAULT 0,
    score_count INTEGER DEFAULT 0,
    updated_ts REAL
);

CREATE TABLE IF NOT EXISTS route_policy(
    route TEXT PRIMARY KEY,
    rated_offers INTEGER DEFAULT 0,
    positive INTEGER DEFAULT 0,
    negative INTEGER DEFAULT 0,
    neutral INTEGER DEFAULT 0,
    updated_ts REAL
);

CREATE INDEX IF NOT EXISTS idx_policy_signals_offer
    ON policy_signals(offer_id);
"""


MIN_RATINGS = 4
MIN_POSITIVE_RATIO = 0.72
MIN_AVG_SCORE = 3.7
MAX_NEGATIVE = 2
MIN_MARGIN = 0.10


def connect(path=None) -> sqlite3.Connection:
    db = talk.connect(path)
    db.executescript(_POLICY_SCHEMA)
    return db


def _fetch_offer(db: sqlite3.Connection, offer_id: int) -> dict:
    row = db.execute(
        "SELECT * FROM offers WHERE id=?",
        (int(offer_id),),
    ).fetchone()

    if row is None:
        raise AgentError(f"پیشنهاد {offer_id} پیدا نشد")

    return dict(row)


def _fetch_candidates(db: sqlite3.Connection, offer_id: int) -> list[dict]:
    rows = db.execute(
        "SELECT * FROM offer_candidates WHERE offer_id=? ORDER BY position",
        (int(offer_id),),
    ).fetchall()

    return [dict(row) for row in rows]


def _clean_scores(scores: dict | None) -> dict[int, float]:
    if not scores:
        return {}

    clean: dict[int, float] = {}

    for raw_position, raw_value in scores.items():
        try:
            position = int(raw_position)
            value = float(raw_value)
        except (TypeError, ValueError):
            raise AgentError("نمره باید عدد باشد")

        if position not in (1, 2):
            raise AgentError("شماره نامزد باید 1 یا 2 باشد")

        if not 0.0 <= value <= 5.0:
            raise AgentError("نمره باید بین 0 تا 5 باشد")

        clean[position] = value

    return clean


def _group_choice_signals(candidates: list[dict], choice: int) -> list[dict]:
    chosen_key = None

    for candidate in candidates:
        if candidate["position"] == choice:
            chosen_key = candidate["template_key"]
            break

    if chosen_key is None:
        raise AgentError(f"نامزد {choice} در این پیشنهاد وجود ندارد")

    grouped: dict[str, dict] = {}

    for candidate in candidates:
        key = candidate["template_key"]
        entry = grouped.setdefault(
            key,
            {
                "accepted": 0,
                "rejected": 0,
                "neutral": 0,
                "score": None,
            },
        )

        if candidate["position"] == choice:
            entry["accepted"] = 1
        elif key == chosen_key:
            entry["neutral"] = 1
        else:
            entry["rejected"] = 1

    signals = []

    for key, entry in grouped.items():
        if entry["accepted"]:
            entry["rejected"] = 0
            entry["neutral"] = 0
        elif entry["rejected"]:
            entry["neutral"] = 0

        signals.append(
            {
                "template_key": key,
                "accepted": entry["accepted"],
                "rejected": entry["rejected"],
                "neutral": entry["neutral"],
                "score": entry["score"],
            }
        )

    return signals


def _group_score_signals(candidates: list[dict],
                         scores: dict[int, float]) -> list[dict]:
    grouped: dict[str, list[float]] = {}

    for candidate in candidates:
        value = scores.get(candidate["position"])
        if value is None:
            continue

        grouped.setdefault(candidate["template_key"], []).append(value)

    if not grouped:
        raise AgentError("هیچ نمره‌ای برای ثبت نیست")

    signals = []

    for key, values in grouped.items():
        average = sum(values) / len(values)
        accepted = 1 if average >= 4.0 else 0
        rejected = 1 if average <= 2.0 else 0
        neutral = 1 if not accepted and not rejected else 0

        signals.append(
            {
                "template_key": key,
                "accepted": accepted,
                "rejected": rejected,
                "neutral": neutral,
                "score": average,
            }
        )

    return signals


def _update_policy(db: sqlite3.Connection, route: str, signal: dict,
                   ts: float) -> None:
    key = signal["template_key"]
    score = signal.get("score")
    score_add = float(score) if score is not None else 0.0
    score_count_add = 1 if score is not None else 0

    row = db.execute(
        "SELECT * FROM template_policy WHERE template_key=?",
        (key,),
    ).fetchone()

    if row is None:
        db.execute(
            "INSERT INTO template_policy"
            "(template_key, rated_offers, positive, negative, neutral, "
            "score_sum, score_count, updated_ts) "
            "VALUES(?,?,?,?,?,?,?,?)",
            (
                key,
                1,
                int(signal.get("accepted") or 0),
                int(signal.get("rejected") or 0),
                int(signal.get("neutral") or 0),
                score_add,
                score_count_add,
                ts,
            ),
        )
    else:
        db.execute(
            "UPDATE template_policy SET "
            "rated_offers=rated_offers+1, "
            "positive=positive+?, "
            "negative=negative+?, "
            "neutral=neutral+?, "
            "score_sum=score_sum+?, "
            "score_count=score_count+?, "
            "updated_ts=? "
            "WHERE template_key=?",
            (
                int(signal.get("accepted") or 0),
                int(signal.get("rejected") or 0),
                int(signal.get("neutral") or 0),
                score_add,
                score_count_add,
                ts,
                key,
            ),
        )

    route_key = route or "conversation"

    route_row = db.execute(
        "SELECT * FROM route_policy WHERE route=?",
        (route_key,),
    ).fetchone()

    if route_row is None:
        db.execute(
            "INSERT INTO route_policy"
            "(route, rated_offers, positive, negative, neutral, updated_ts) "
            "VALUES(?,?,?,?,?,?)",
            (
                route_key,
                1,
                int(signal.get("accepted") or 0),
                int(signal.get("rejected") or 0),
                int(signal.get("neutral") or 0),
                ts,
            ),
        )
    else:
        db.execute(
            "UPDATE route_policy SET "
            "rated_offers=rated_offers+1, "
            "positive=positive+?, "
            "negative=negative+?, "
            "neutral=neutral+?, "
            "updated_ts=? "
            "WHERE route=?",
            (
                int(signal.get("accepted") or 0),
                int(signal.get("rejected") or 0),
                int(signal.get("neutral") or 0),
                ts,
                route_key,
            ),
        )


def record_rating(db: sqlite3.Connection, ndb: sqlite3.Connection,
                  offer_id: int, *, choice: int | None = None,
                  scores: dict | None = None) -> dict:
    report = talk.rate_offer(
        db,
        ndb,
        int(offer_id),
        choice=choice,
        scores=scores,
    )

    offer = _fetch_offer(db, int(offer_id))
    candidates = _fetch_candidates(db, int(offer_id))
    route = offer.get("route") or ""
    now = time.time()

    if choice is not None:
        signals = _group_choice_signals(candidates, int(choice))
    else:
        clean_scores = _clean_scores(scores)
        signals = _group_score_signals(candidates, clean_scores)

    for signal in signals:
        db.execute(
            "INSERT INTO policy_signals"
            "(offer_id, template_key, route, accepted, rejected, neutral, "
            "score, ts) VALUES(?,?,?,?,?,?,?,?)",
            (
                int(offer_id),
                signal["template_key"],
                route,
                int(signal.get("accepted") or 0),
                int(signal.get("rejected") or 0),
                int(signal.get("neutral") or 0),
                signal.get("score"),
                now,
            ),
        )

        _update_policy(db, route, signal, now)

    db.commit()
    return report


def policy_for(db: sqlite3.Connection, template_key: str) -> dict:
    row = db.execute(
        "SELECT * FROM template_policy WHERE template_key=?",
        (template_key,),
    ).fetchone()

    if row is None:
        return {
            "template_key": template_key,
            "rated_offers": 0,
            "positive": 0,
            "negative": 0,
            "neutral": 0,
            "score_sum": 0.0,
            "score_count": 0,
        }

    return dict(row)


def _policy_allows(policy: dict) -> tuple[bool, str]:
    positive = int(policy.get("positive") or 0)
    negative = int(policy.get("negative") or 0)
    neutral = int(policy.get("neutral") or 0)
    score_count = int(policy.get("score_count") or 0)
    score_sum = float(policy.get("score_sum") or 0.0)

    total = positive + negative + neutral

    if total < MIN_RATINGS:
        return False, "کمبود داده"

    if negative > MAX_NEGATIVE:
        return False, "منفی زیاد"

    denominator = positive + negative

    if denominator == 0:
        return False, "بدون سیگنال مثبت/منفی"

    ratio = positive / denominator

    if ratio < MIN_POSITIVE_RATIO:
        return False, "نسبت مثبت کم"

    if score_count:
        average = score_sum / score_count
        if average < MIN_AVG_SCORE:
            return False, "میانگین نمره کم"

    return True, ""


def _policy_score(policy: dict) -> float:
    positive = int(policy.get("positive") or 0)
    negative = int(policy.get("negative") or 0)
    neutral = int(policy.get("neutral") or 0)
    score_count = int(policy.get("score_count") or 0)
    score_sum = float(policy.get("score_sum") or 0.0)

    total = positive + negative + neutral
    if total == 0:
        return 0.0

    denominator = positive + negative
    ratio = positive / denominator if denominator else 0.0

    if score_count:
        average = score_sum / score_count
        average_norm = max(0.0, min(1.0, average / 5.0))
    else:
        average_norm = ratio

    return 0.5 * ratio + 0.5 * average_norm


def _route_veto(db: sqlite3.Connection, route: str) -> bool:
    route_key = route or "conversation"

    row = db.execute(
        "SELECT * FROM route_policy WHERE route=?",
        (route_key,),
    ).fetchone()

    if row is None:
        return False

    positive = int(row["positive"] or 0)
    negative = int(row["negative"] or 0)

    return negative > max(3, positive)


def choose(db: sqlite3.Connection, ndb: sqlite3.Connection,
           udb, idb: sqlite3.Connection, text: str, *,
           learn: bool = False, force_ask: bool = False) -> dict:
    report = talk.propose(db, ndb, udb, idb, text, learn=learn)

    result = {
        "auto": False,
        "reason": "",
        "report": report,
        "candidates": report.get("candidates", []),
    }

    if report.get("offer_id") is None:
        result["reason"] = "no-candidates"
        return result

    decision = report["decision"]
    candidates = report["candidates"]
    route = decision.get("route") or ""

    if force_ask:
        result["reason"] = "force-ask"
        return result

    if decision.get("kind") == "unknown":
        result["reason"] = "unknown-intent"
        return result

    if _route_veto(db, route):
        result["reason"] = "route-veto"
        return result

    enriched = []

    for index, candidate in enumerate(candidates):
        policy = policy_for(db, candidate["key"])
        allowed, reason = _policy_allows(policy)
        policy_score = _policy_score(policy)
        rank_score = max(0.0, 1.0 - 0.15 * index)
        combined = round(0.65 * policy_score + 0.35 * rank_score, 4)

        enriched.append({
            **candidate,
            "policy_allowed": allowed,
            "policy_reason": reason,
            "policy_score": round(policy_score, 4),
            "combined": combined,
        })

    enriched.sort(key=lambda item: item["combined"], reverse=True)
    result["candidates"] = enriched

    best_allowed = None
    second_allowed = None

    for item in enriched:
        if item["policy_allowed"]:
            if best_allowed is None:
                best_allowed = item
            else:
                second_allowed = item
                break

    if best_allowed is None:
        result["reason"] = "no-allowed-candidate"
        return result

    if second_allowed is not None:
        margin = best_allowed["combined"] - second_allowed["combined"]
        if margin < MIN_MARGIN:
            result["reason"] = "low-margin"
            return result

    db.execute(
        "UPDATE offers SET status='auto', chosen=? WHERE id=?",
        (int(best_allowed["position"]), int(report["offer_id"])),
    )
    db.commit()

    result.update({
        "auto": True,
        "reason": "policy-ok",
        "chosen_position": best_allowed["position"],
        "chosen_text": best_allowed["text"],
        "chosen_key": best_allowed["key"],
    })

    return result


def rebuild(db: sqlite3.Connection) -> int:
    db.execute("DELETE FROM template_policy")
    db.execute("DELETE FROM route_policy")

    rows = db.execute("SELECT * FROM policy_signals").fetchall()

    for row in rows:
        signal = {
            "template_key": row["template_key"],
            "accepted": int(row["accepted"] or 0),
            "rejected": int(row["rejected"] or 0),
            "neutral": int(row["neutral"] or 0),
            "score": row["score"],
        }

        _update_policy(
            db,
            row["route"] or "",
            signal,
            float(row["ts"] or time.time()),
        )

    db.commit()
    return len(rows)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.talkpolicy",
        description="سیاست یادگیری و انتخاب خودکار پاسخ",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    rate = sub.add_parser("rate", help="امتیاز دادن با ثبت سیگنال سیاست")
    rate.add_argument("offer_id", type=int)
    rate.add_argument("--choice", type=int, choices=(1, 2), default=None)
    rate.add_argument(
        "--score",
        action="append",
        default=[],
        help="مثال: --score 1=5 --score 2=2",
    )

    choose_parser = sub.add_parser(
        "choose",
        help="پیشنهاد پاسخ و تصمیم انتخاب خودکار",
    )
    choose_parser.add_argument("text")
    choose_parser.add_argument("--learn", action="store_true")
    choose_parser.add_argument(
        "--force-ask",
        action="store_true",
        help="حتماً از کاربر بپرس، حتی اگر سیاست اجازه دهد",
    )

    show = sub.add_parser("show", help="نمایش سیاست قالب‌ها")
    show.add_argument("template", nargs="?", default=None)
    show.add_argument("--limit", type=int, default=20)

    sub.add_parser("rebuild", help="بازسازی جدول سیاست از سیگنال‌ها")

    args = parser.parse_args(argv)

    db = connect()
    ndb = nlg.prepare()
    udb = usage.connect()
    idb = intent.prepare()

    try:
        if args.command == "rate":
            scores: dict[int, float] = {}

            for item in args.score:
                if "=" not in item:
                    raise AgentError("فرمت score باید مثل 1=5 باشد")

                position, value = item.split("=", 1)
                scores[int(position)] = float(value)

            if args.choice is not None and scores:
                raise AgentError("فقط یکی از --choice یا --score استفاده شود")

            report = record_rating(
                db,
                ndb,
                args.offer_id,
                choice=args.choice,
                scores=scores or None,
            )

            print("به‌روزرسانی قالب‌ها:", report.get("updates"))
            print("وزن‌ها:", report.get("weights"))
            return 0

        if args.command == "choose":
            result = choose(
                db,
                ndb,
                udb,
                idb,
                args.text,
                learn=args.learn,
                force_ask=args.force_ask,
            )

            report = result["report"]

            if report.get("offer_id") is None:
                print(report.get("message") or "پاسخی پیدا نشد.")
                return 0

            decision = report["decision"]

            print(
                f"پیشنهاد #{report['offer_id']} | "
                f"قصد: {decision.get('kind')} / {decision.get('route') or '-'}"
            )

            print(
                "انتخاب خودکار:",
                "بله" if result["auto"] else "خیر",
                "| دلیل:",
                result["reason"] or "-",
            )

            if result["auto"]:
                print("پاسخ انتخاب‌شده:", result.get("chosen_text"))

            for candidate in result["candidates"]:
                print(f"{candidate['position']}. {candidate['text']}")
                print(
                    "   "
                    f"قالب: {candidate['key']} | "
                    f"سیاست: {'مجاز' if candidate.get('policy_allowed') else 'غیرمجاز'} | "
                    f"امتیاز سیاست: {candidate.get('policy_score')} | "
                    f"ترکیب: {candidate.get('combined')}"
                )

            if not result["auto"]:
                offer_id = report["offer_id"]
                print("\nبرای امتیاز دادن:")
                print(f"python -m farsi.talkpolicy rate {offer_id} --choice 1")
                print(
                    f"python -m farsi.talkpolicy rate {offer_id} "
                    "--score 1=5 --score 2=3"
                )

            return 0

        if args.command == "show":
            if args.template:
                policy = policy_for(db, args.template)
                allowed, reason = _policy_allows(policy)

                print("سیاست قالب:", policy)
                print("اجازه انتخاب خودکار:", allowed)
                print("دلیل:", reason)
                return 0

            rows = db.execute(
                "SELECT * FROM template_policy "
                "ORDER BY rated_offers DESC, positive DESC "
                "LIMIT ?",
                (int(args.limit),),
            ).fetchall()

            for row in rows:
                policy = dict(row)
                allowed, reason = _policy_allows(policy)

                print(
                    f"{policy['template_key']} | "
                    f"rated={policy['rated_offers']} | "
                    f"+{policy['positive']} | "
                    f"-{policy['negative']} | "
                    f"~{policy['neutral']} | "
                    f"auto={allowed} | "
                    f"{reason}"
                )

            return 0

        if args.command == "rebuild":
            count = rebuild(db)
            print(f"{count} سیگنال برای بازسازی سیاست استفاده شد.")
            return 0

        return 1

    except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
        print("توقف:", exc)
        return 1

    finally:
        db.close()
        ndb.close()
        udb.close()
        idb.close()


if __name__ == "__main__":
    raise SystemExit(main())