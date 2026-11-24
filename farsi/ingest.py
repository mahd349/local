"""Turn subtitle CSV exports into clean Persian prose for the corpus.

Only the Persian text column is kept, so timestamps and titles never reach the
statistics. Subtitle exports often carry the sentence stop at the wrong end
because of right-to-left rendering; it is moved back to the end, otherwise
frame mining would reject every line as unterminated. Speaker dashes split a
row into its turns; bracketed name cards and translator notes are dropped.
"""
from __future__ import annotations

import argparse
import csv
import re
import sqlite3
from pathlib import Path

from agent.errors import AgentError

from .text import persian_ratio, tokens

STOPS = ".!؟?"
MIN_WORDS = 3
MIN_FA = 0.6
HINTS = ("subtitle_text", "text", "persian", "sentence", "fa")
_BRACKET = re.compile(r"\[[^\]]*\]|\([^)]*\)")
_SPACE = re.compile(r"\s+")
_LATIN = re.compile(r"[A-Za-z]")
_TURN = re.compile(r"\s+-\s+|^-\s+|\s+-$")


def _open_rows(path: Path):
    with path.open("rb") as probe:
        if probe.read(4) == b"PAR1":
            raise AgentError("این فایل parquet است، نه CSV؛ اول به CSV تبدیلش کن")
    handle = path.open("r", encoding="utf-8-sig", errors="replace", newline="")
    sample = handle.read(8192)
    handle.seek(0)
    try:
        dialect = csv.Sniffer().sniff(sample, delimiters=",;\t|")
        delimiter = dialect.delimiter
    except csv.Error:
        delimiter = max(",;\t|", key=lambda ch: sample.count(ch))
    return handle, csv.reader(handle, delimiter=delimiter), delimiter


def _pick_column(first: list[str], probe: list[list[str]]) -> tuple[int, bool]:
    lowered = [str(cell).strip().lower() for cell in first]
    for index, name in enumerate(lowered):
        if any(hint in name for hint in HINTS):
            return index, True
    width = max((len(row) for row in probe), default=0)
    best, best_score = 0, -1.0
    for index in range(width):
        cells = [row[index] for row in probe if len(row) > index and row[index]]
        if not cells:
            continue
        score = sum(persian_ratio(cell) for cell in cells) / len(cells)
        if score > best_score:
            best, best_score = index, score
    return best, False


def _clean(cell: str) -> list[str]:
    raw = str(cell or "").replace("\u200f", "").replace("\u200e", "")
    raw = _BRACKET.sub(" ", raw).replace("♪", " ").replace("*", " ")
    raw = _SPACE.sub(" ", raw).strip()
    if not raw:
        return []
    out: list[str] = []
    for part in _TURN.split(raw):
        part = part.strip(" -–—\t")
        if not part:
            continue
        lead = 0
        while lead < len(part) and part[lead] in STOPS + " ":
            lead += 1
        marks = [ch for ch in part[:lead] if ch in STOPS]
        body = part[lead:].strip()
        if not body:
            continue
        if body[-1] not in STOPS:
            body += marks[0] if marks else "."
        out.append(body)
    return out


def convert(src: str | Path, dst: str | Path | None = None, *,
            limit: int = 0, min_words: int = MIN_WORDS) -> dict:
    source = Path(src)
    if not source.exists():
        raise AgentError(f"فایل پیدا نشد: {source}")
    target = Path(dst) if dst else source.with_name(source.stem + "-fa.txt")
    handle, reader, delimiter = _open_rows(source)
    rejected = {"short": 0, "latin": 0, "low_fa": 0, "empty": 0}
    rows = written = 0
    column = 0
    header = False
    try:
        first = next(reader, [])
        probe = [first]
        for _ in range(199):
            row = next(reader, None)
            if row is None:
                break
            probe.append(row)
        column, header = _pick_column(first, probe)
        stream = probe[1:] if header else probe
        with target.open("w", encoding="utf-8", newline="\n") as out:
            for row in _chain(stream, reader):
                rows += 1
                cell = row[column] if len(row) > column else ""
                lines = _clean(cell)
                if not lines:
                    rejected["empty"] += 1
                for line in lines:
                    if _LATIN.search(line):
                        rejected["latin"] += 1
                        continue
                    if persian_ratio(line) < MIN_FA:
                        rejected["low_fa"] += 1
                        continue
                    if len(tokens(line)) < min_words:
                        rejected["short"] += 1
                        continue
                    out.write(line + "\n")
                    written += 1
                if limit and rows >= limit:
                    break
    finally:
        handle.close()
    return {"source": str(source), "output": str(target),
            "delimiter": delimiter, "column": column, "header": header,
            "rows": rows, "lines": written, "rejected": rejected}


def _chain(head: list, reader):
    for row in head:
        yield row
    for row in reader:
        yield row


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.ingest", description="تبدیل CSV زیرنویس به متن فارسی تمیز")
    parser.add_argument("source")
    parser.add_argument("--out", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--min-words", type=int, default=MIN_WORDS)
    args = parser.parse_args(argv)
    try:
        report = convert(args.source, args.out, limit=args.limit,
                         min_words=args.min_words)
    except (AgentError, OSError, ValueError, csv.Error, sqlite3.Error) as exc:
        print("توقف:", exc)
        return 1
    for key in ("source", "output", "delimiter", "column", "header",
                "rows", "lines", "rejected"):
        print(f"{key}: {report[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())