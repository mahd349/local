"""Practice entry point for talk offers, ratings and auto-selection.

This module does not change talk.py or talkpolicy.py. It wraps them with a
small CLI so the loop of propose -> choose -> rate can be practiced quickly.
"""
from __future__ import annotations

import argparse
import re
import sqlite3

from agent.errors import AgentError

from . import intent, nlg, talkpolicy, usage


def _open():
    tdb = talkpolicy.connect()
    ndb = nlg.prepare()
    udb = usage.connect()
    idb = intent.prepare()
    return tdb, ndb, udb, idb


def _parse_score_args(items: list[str] | None) -> dict[int, float]:
    scores: dict[int, float] = {}

    for item in items or []:
        if "=" not in item:
            raise AgentError("فرمت score باید مثل 1=5 باشد")

        raw_position, raw_value = item.split("=", 1)

        try:
            position = int(raw_position)
            value = float(raw_value)
        except ValueError:
            raise AgentError("فرمت score باید مثل 1=5 باشد")

        if position not in (1, 2):
            raise AgentError("شماره نامزد باید 1 یا 2 باشد")

        if not 0.0 <= value <= 5.0:
            raise AgentError("نمره باید بین 0 تا 5 باشد")

        scores[position] = value

    return scores


def _parse_user_rating(raw: str, positions: list[int]) -> tuple[int | None, dict[int, float] | None]:
    text = str(raw or "").strip()

    if not text:
        return None, None

    if text in ("1", "2"):
        choice = int(text)
        if choice not in positions:
            raise AgentError(f"نامزد {choice} در این پیشنهاد وجود ندارد")
        return choice, None

    if "=" in text:
        scores: dict[int, float] = {}

        for token in re.split(r"[,\s]+", text):
            if not token:
                continue

            if "=" not in token:
                raise AgentError("فرمت نمره باید مثل 1=5 باشد")

            raw_position, raw_value = token.split("=", 1)

            try:
                position = int(raw_position)
                value = float(raw_value)
            except ValueError:
                raise AgentError("فرمت نمره باید مثل 1=5 باشد")

            if position not in positions:
                raise AgentError(f"نامزد {position} در این پیشنهاد وجود ندارد")

            if not 0.0 <= value <= 5.0:
                raise AgentError("نمره باید بین 0 تا 5 باشد")

            scores[position] = value

        if not scores:
            raise AgentError("هیچ نمره‌ای داده نشد")

        return None, scores

    parts = [part for part in re.split(r"[,\s]+", text) if part]

    try:
        values = [float(part) for part in parts]
    except ValueError:
        raise AgentError("ورودی امتیاز نامعتبر است")

    if len(values) == 1 and len(positions) == 1:
        value = values[0]
        if not 0.0 <= value <= 5.0:
            raise AgentError("نمره باید بین 0 تا 5 باشد")
        return None, {positions[0]: value}

    if len(values) == 1 and int(values[0]) in positions and values[0].is_integer():
        return int(values[0]), None

    if len(values) == len(positions):
        scores = {}
        for position, value in zip(positions, values):
            if not 0.0 <= value <= 5.0:
                raise AgentError("نمره باید بین 0 تا 5 باشد")
            scores[position] = value
        return None, scores

    raise AgentError(
        "یا شماره نامزد را وارد کن، یا نمره‌ها را مثل 1=5 2=3 بنویس"
    )


def _print_result(result: dict, show_instructions: bool = True) -> None:
    report = result["report"]

    if report.get("offer_id") is None:
        print(report.get("message") or "پاسخی پیدا نشد.")
        return

    decision = report["decision"]

    print(
        f"پیشنهاد #{report['offer_id']} | "
        f"قصد: {decision.get('kind')} / {decision.get('route') or '-'}"
    )

    print(
        "انتخاب خودکار:",
        "بله" if result.get("auto") else "خیر",
        "| دلیل:",
        result.get("reason") or "-",
    )

    if result.get("auto"):
        print("پاسخ انتخاب‌شده:", result.get("chosen_text"))

    for candidate in result.get("candidates", []):
        print(f"{candidate.get('position')}. {candidate.get('text')}")

        details = []

        if candidate.get("key"):
            details.append(f"قالب: {candidate['key']}")

        if "policy_allowed" in candidate:
            details.append(
                "سیاست: " + ("مجاز" if candidate["policy_allowed"] else "غیرمجاز")
            )

        if "policy_score" in candidate:
            details.append(f"امتیاز سیاست: {candidate['policy_score']}")

        if "combined" in candidate:
            details.append(f"ترکیب: {candidate['combined']}")

        if details:
            print("   " + " | ".join(details))

    if not show_instructions:
        return

    offer_id = report["offer_id"]

    if result.get("auto"):
        chosen = result.get("chosen_position")
        print("\nاگر پاسخ خودکار درست یا نادرست بود، می‌توانی امتیاز بدهی:")
        print(f"python -m farsi.talkchat rate {offer_id} --score {chosen}=5")
        print(f"python -m farsi.talkchat rate {offer_id} --score {chosen}=1")
        return

    print("\nبرای امتیاز دادن:")
    print(f"python -m farsi.talkchat rate {offer_id} --choice 1")
    print(f"python -m farsi.talkchat rate {offer_id} --score 1=5 --score 2=3")


def _apply_rating(tdb: sqlite3.Connection, ndb: sqlite3.Connection,
                  offer_id: int, *, choice: int | None = None,
                  scores: dict[int, float] | None = None) -> None:
    report = talkpolicy.record_rating(
        tdb,
        ndb,
        int(offer_id),
        choice=choice,
        scores=scores,
    )

    print("ثبت شد.")
    print("به‌روزرسانی:", report.get("updates"))
    print("وزن‌ها:", report.get("weights"))


def _loop(tdb: sqlite3.Connection, ndb: sqlite3.Connection,
          udb, idb: sqlite3.Connection, *, learn: bool = False) -> None:
    print("حالت تمرین. برای خروج بنویس: exit")

    while True:
        try:
            text = input("متن> ").strip()
        except (EOFError, KeyboardInterrupt):
            print()
            break

        if text.lower() in {"exit", "quit", "خروج"}:
            break

        if not text:
            continue

        try:
            result = talkpolicy.choose(
                tdb,
                ndb,
                udb,
                idb,
                text,
                learn=learn,
                force_ask=False,
            )

            report = result["report"]

            if report.get("offer_id") is None:
                print(report.get("message") or "پاسخی پیدا نشد.")
                continue

            _print_result(result, show_instructions=False)

            offer_id = report["offer_id"]
            positions = [
                int(candidate["position"])
                for candidate in result.get("candidates", [])
            ] or [1, 2]

            if result.get("auto"):
                chosen = int(result.get("chosen_position") or positions[0])
                raw = input("نمره پاسخ انتخاب‌شده (0..5 یا خالی): ").strip()

                if raw:
                    try:
                        value = float(raw)
                        if not 0.0 <= value <= 5.0:
                            raise ValueError
                        _apply_rating(tdb, ndb, offer_id, scores={chosen: value})
                    except (ValueError, AgentError) as exc:
                        print("ثبت نشد:", exc)

                continue

            raw = input("انتخاب 1/2 یا نمره مثل 1=5 2=3: ").strip()

            if raw:
                try:
                    choice, scores = _parse_user_rating(raw, positions)
                    _apply_rating(tdb, ndb, offer_id, choice=choice, scores=scores)
                except (ValueError, AgentError) as exc:
                    print("ثبت نشد:", exc)

        except (AgentError, OSError, ValueError, sqlite3.Error) as exc:
            print("خطا:", exc)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.talkchat",
        description="تمرین گفت‌وگو با انتخاب خودکار و امتیازدهی",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    say = sub.add_parser("say", help="یک پیام بده و نتیجه سیاست را ببین")
    say.add_argument("text")
    say.add_argument("--learn", action="store_true")
    say.add_argument("--force-ask", action="store_true")
    say.add_argument("--choice", type=int, choices=(1, 2), default=None)
    say.add_argument(
        "--score",
        action="append",
        default=[],
        help="مثال: --score 1=5 --score 2=2",
    )

    rate = sub.add_parser("rate", help="امتیاز دادن به یک پیشنهاد")
    rate.add_argument("offer_id", type=int)
    rate.add_argument("--choice", type=int, choices=(1, 2), default=None)
    rate.add_argument(
        "--score",
        action="append",
        default=[],
        help="مثال: --score 1=5 --score 2=2",
    )

    loop = sub.add_parser("loop", help="حالت تمرین پشت‌سرهم")
    loop.add_argument("--learn", action="store_true")

    args = parser.parse_args(argv)

    tdb, ndb, udb, idb = _open()

    try:
        if args.command == "say":
            scores = _parse_score_args(args.score)

            if args.choice is not None and scores:
                raise AgentError("فقط یکی از --choice یا --score استفاده شود")

            result = talkpolicy.choose(
                tdb,
                ndb,
                udb,
                idb,
                args.text,
                learn=args.learn,
                force_ask=args.force_ask,
            )

            _print_result(result)

            offer_id = result["report"].get("offer_id")

            if offer_id is None:
                return 0

            if args.choice is not None or scores:
                _apply_rating(
                    tdb,
                    ndb,
                    offer_id,
                    choice=args.choice,
                    scores=scores or None,
                )

            return 0

        if args.command == "rate":
            scores = _parse_score_args(args.score)

            if args.choice is not None and scores:
                raise AgentError("فقط یکی از --choice یا --score استفاده شود")

            if args.choice is None and not scores:
                raise AgentError("یکی از --choice یا --score لازم است")

            _apply_rating(
                tdb,
                ndb,
                args.offer_id,
                choice=args.choice,
                scores=scores or None,
            )
            return 0

        if args.command == "loop":
            _loop(tdb, ndb, udb, idb, learn=args.learn)
            return 0

        return 1

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