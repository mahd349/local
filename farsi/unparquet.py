"""Merge parquet parts into plain Persian text the corpus can read.

Parquet is columnar and needs a real reader; the standard library cannot decode
it. pyarrow streams row groups, so a multi-gigabyte dataset never lands in RAM
at once. Rows are written one per line, label lines and broken template
artifacts are dropped, and a line without a terminal stop gets one, because
frame mining rejects unterminated fragments.
"""
from __future__ import annotations

import argparse
import re
from pathlib import Path

from agent.errors import AgentError

from .text import persian_ratio

TEXT_HINTS = ("text", "content", "article", "body", "sentence", "paragraph")
STOP_MARKS = ".!؟?"
_SPACE = re.compile(r"\s+")
_BROKEN = re.compile(r"--|،\s*و|\(\s*\)")
_DIGITS = re.compile(r"\d+")
_SEP = str.maketrans({"٬": ""})


def _part_key(path: Path) -> tuple:
    return (tuple(int(value) for value in _DIGITS.findall(path.name)), path.name)


def _expand(paths: list[str]) -> list[Path]:
    files: list[Path] = []
    for raw in paths:
        target = Path(raw)
        if target.is_dir():
            files.extend(target.glob("*.parquet"))
        elif target.exists():
            files.append(target)
        else:
            raise AgentError(f"پیدا نشد: {target}")
    if not files:
        raise AgentError("هیچ فایل parquet پیدا نشد")
    return sorted(set(files), key=_part_key)


def _pick_column(names: list[str], handle) -> str:
    lowered = [name.lower() for name in names]
    for index, name in enumerate(lowered):
        if any(hint in name for hint in TEXT_HINTS):
            return names[index]
    sample = next(handle.iter_batches(batch_size=64), None)
    best, best_score = names[0], -1.0
    if sample is None:
        return best
    for index, name in enumerate(names):
        values = [value for value in sample.column(index).to_pylist() if value]
        if not values:
            continue
        score = sum(persian_ratio(str(value)) for value in values) / len(values)
        if score > best_score:
            best, best_score = name, score
    return best


def _clean(value, min_chars: int, keep_labels: bool, dropped: dict) -> str:
    text = str(value or "").replace("\u200f", "").replace("\u200e", "")
    text = _SPACE.sub(" ", text).strip().translate(_SEP)
    if not text:
        dropped["empty"] += 1
        return ""
    if _BROKEN.search(text):
        dropped["broken"] += 1
        return ""
    if ":" in text and not keep_labels:
        head = text.split(":", 1)[0]
        if not any(mark in head for mark in STOP_MARKS):
            dropped["label"] += 1
            return ""
    if len(text) < min_chars:
        dropped["short"] += 1
        return ""
    if text[-1] not in STOP_MARKS:
        text += "."
    return text


def convert(paths: list[str], out: str | None = None, *, limit: int = 0,
            min_chars: int = 25, column: str | None = None,
            keep_labels: bool = False) -> dict:
    try:
        import pyarrow.parquet as pq
    except ImportError as exc:
        raise AgentError(
            "برای parquet نصب لازم است: python -m pip install pyarrow") from exc
    files = _expand(paths)
    target = Path(out) if out else files[0].with_name("merged-fa.txt")
    target.parent.mkdir(parents=True, exist_ok=True)
    dropped = {"empty": 0, "label": 0, "broken": 0, "short": 0}
    used = column
    rows = written = 0
    with target.open("w", encoding="utf-8", newline="\n") as sink:
        for file in files:
            handle = pq.ParquetFile(str(file))
            names = list(handle.schema_arrow.names)
            if used is None:
                used = _pick_column(names, handle)
            if used not in names:
                raise AgentError(f"ستون «{used}» در {file.name} نیست")
            print(f"  {file.name}: column={used}", flush=True)
            for batch in handle.iter_batches(batch_size=2048, columns=[used]):
                for value in batch.column(0).to_pylist():
                    rows += 1
                    line = _clean(value, min_chars, keep_labels, dropped)
                    if line:
                        sink.write(line + "\n")
                        written += 1
                    if limit and written >= limit:
                        break
                if limit and written >= limit:
                    break
            if limit and written >= limit:
                break
    return {"files": [file.name for file in files], "column": used,
            "rows": rows, "lines": written, "dropped": dropped,
            "output": str(target),
            "mb": round(target.stat().st_size / 1048576, 2)}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.unparquet", description="ادغام پارت‌های parquet به متن فارسی")
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--out", default=None)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--min-chars", type=int, default=25)
    parser.add_argument("--column", default=None)
    parser.add_argument("--keep-labels", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = convert(args.paths, args.out, limit=args.limit,
                         min_chars=args.min_chars, column=args.column,
                         keep_labels=args.keep_labels)
    except (AgentError, OSError, ValueError) as exc:
        print("توقف:", exc)
        return 1
    for key in ("files", "column", "rows", "lines", "dropped", "output", "mb"):
        print(f"{key}: {report[key]}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())