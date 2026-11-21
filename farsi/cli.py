"""Single entry point: python -m farsi <command>."""
from __future__ import annotations

import argparse
from pathlib import Path

from . import corpus


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="farsi", description="پیکرهٔ فارسی محلی")
    sub = parser.add_subparsers(dest="command", required=True)
    add = sub.add_parser("add", help="افزودن یک متن")
    add.add_argument("--text", required=True)
    add.add_argument("--source", default="user")
    add.add_argument("--speaker", default="me")
    file_parser = sub.add_parser("add-file", help="افزودن فایل متنی یا خروجی JSON چت")
    file_parser.add_argument("path")
    file_parser.add_argument("--source", default="archive")
    sub.add_parser("stats", help="آمار پیکره")
    recent_parser = sub.add_parser("recent", help="آخرین متن‌ها")
    recent_parser.add_argument("--limit", type=int, default=10)
    sub.add_parser("on", help="روشن کردن یادگیری")
    sub.add_parser("off", help="خاموش کردن یادگیری")
    args = parser.parse_args(argv)

    if args.command in ("on", "off"):
        corpus.set_learning(args.command == "on")
        print("یادگیری:", "روشن" if args.command == "on" else "خاموش")
        return 0

    db = corpus.connect()
    try:
        if args.command == "add":
            print(corpus.add_text(db, args.text, source=args.source, speaker=args.speaker))
        elif args.command == "add-file":
            path = Path(args.path)
            if path.suffix.lower() == ".json":
                print(corpus.add_json(db, path, source=args.source))
            else:
                text = path.read_text(encoding="utf-8-sig", errors="replace")
                print(corpus.add_text(db, text, source=args.source, speaker="file"))
        elif args.command == "stats":
            print(corpus.stats(db))
        else:
            for item in corpus.recent(db, args.limit):
                print(f"#{item['id']} [{item['source']}/{item['speaker']}] "
                      f"q={item['quality']} {item['raw'][:70]}")
        return 0
    finally:
        db.close()