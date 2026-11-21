"""Ingest texts larger than the corpus per-document cap.

corpus.MAX_TEXT truncates a single document, so a whole book or a subtitle
export silently loses most of its content. This module splits text at sentence
boundaries into pieces below that cap, adds each piece as its own document, and
rebuilds the derived stores once at the end. The chunk count is bounded so a
huge file cannot quietly consume the disk budget; raising the bound later adds
only the missing tail, because the corpus deduplicates by hash.
"""
from __future__ import annotations

import argparse
import re
import subprocess
import sys
import tempfile
from pathlib import Path

from agent.errors import AgentError

from . import corpus, ingest

PACKAGE_PARENT = Path(__file__).resolve().parent.parent
CHUNK_CHARS = 18000
MAX_CHUNKS = 60
MIN_WORDS = 1
_SENTENCE = re.compile(r"[^.!؟?]*[.!؟?]+|[^.!؟?]+$")


def _read_text(path: Path) -> tuple[str, str]:
    for encoding in ("utf-8-sig", "utf-8", "cp1256"):
        try:
            return path.read_text(encoding=encoding), encoding
        except UnicodeDecodeError:
            continue
    return path.read_text(encoding="utf-8", errors="replace"), "utf-8/replace"


def _pieces(line: str, limit: int) -> list[str]:
    if len(line) <= limit:
        return [line]
    out: list[str] = []
    for match in _SENTENCE.finditer(line):
        piece = match.group(0)
        while len(piece) > limit:
            cut = piece.rfind(" ", 0, limit)
            if cut <= 0:
                cut = limit
            out.append(piece[:cut])
            piece = piece[cut:]
        if piece:
            out.append(piece)
    return out


def split_text(text: str, limit: int) -> list[str]:
    chunks: list[str] = []
    buffer: list[str] = []
    size = 0
    for line in text.splitlines(keepends=True):
        for piece in _pieces(line, limit):
            if size + len(piece) > limit and buffer:
                chunks.append("".join(buffer))
                buffer, size = [], 0
            buffer.append(piece)
            size += len(piece)
    if buffer:
        chunks.append("".join(buffer))
    return [chunk for chunk in chunks if chunk.strip()]


def _add(path: Path) -> bool:
    done = subprocess.run(
        [sys.executable, "-m", "farsi", "add-file", str(path)],
        cwd=str(PACKAGE_PARENT), capture_output=True, text=True,
        encoding="utf-8", errors="replace",
    )
    line = (done.stdout or done.stderr or "").strip().replace("\n", " ")
    if line:
        print("   ", line[:160], flush=True)
    return done.returncode == 0


def teach_file(source: Path, limit: int, max_chunks: int) -> dict:
    if source.suffix.lower() in {".csv", ".tsv"}:
        report = ingest.convert(source, min_words=MIN_WORDS)
        print("  csv:", report["lines"], "lines ->", report["output"],
              flush=True)
        source = Path(report["output"])
    text, encoding = _read_text(source)
    chunks = split_text(text, limit)
    taken = chunks[:max_chunks]
    print(f"  {source.name}: {len(text)} chars | {encoding} | "
          f"{len(chunks)} chunks | taking {len(taken)}", flush=True)
    sent = 0
    with tempfile.TemporaryDirectory(prefix="farsi-teach-") as folder:
        for index, chunk in enumerate(taken, start=1):
            part = Path(folder) / f"part-{index:04d}.txt"
            part.write_text(chunk, encoding="utf-8")
            print(f"  chunk {index}/{len(taken)}", flush=True)
            if _add(part):
                sent += 1
    return {"file": str(source), "chars": len(text), "chunks": len(chunks),
            "sent": sent, "skipped": max(0, len(chunks) - len(taken))}


def run(paths: list[str], chunk_chars: int, max_chunks: int,
        rebuild: bool = True) -> dict:
    cap = int(getattr(corpus, "MAX_TEXT", 20000))
    limit = max(1000, min(chunk_chars, cap - 2000))
    reports = []
    for raw in paths:
        source = Path(raw)
        if not source.exists():
            print("  not found:", source, flush=True)
            continue
        reports.append(teach_file(source, limit, max_chunks))
    codes = {}
    if rebuild and reports:
        done = subprocess.run(
            [sys.executable, "-m", "farsi.purge", "rebuild"],
            cwd=str(PACKAGE_PARENT), capture_output=True, text=True,
            encoding="utf-8", errors="replace",
        )
        for line in (done.stdout or "").splitlines():
            if line.strip():
                print("  " + line.strip(), flush=True)
        codes["rebuild"] = done.returncode
    return {"limit": limit, "files": reports, "codes": codes}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="farsi.teach", description="تکه‌کردن و افزودن متن بزرگ")
    parser.add_argument("paths", nargs="+")
    parser.add_argument("--chunk-chars", type=int, default=CHUNK_CHARS)
    parser.add_argument("--max-chunks", type=int, default=MAX_CHUNKS)
    parser.add_argument("--no-rebuild", action="store_true")
    args = parser.parse_args(argv)
    try:
        report = run(args.paths, args.chunk_chars, args.max_chunks,
                     rebuild=not args.no_rebuild)
    except (AgentError, OSError, ValueError) as exc:
        print("توقف:", exc)
        return 1
    print("limit:", report["limit"])
    for item in report["files"]:
        print(f"  {item['file']}: chunks={item['chunks']} "
              f"sent={item['sent']} skipped={item['skipped']}")
    print("codes:", report["codes"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())