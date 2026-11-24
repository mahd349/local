"""File index: names plus best-effort content, searchable with SQLite FTS5.

C:, D:\\PC HP 650 lock and D:\\Games are never touched. Content extraction is
best-effort: Persian PDFs and OCR quality are reported, never assumed.
"""

from __future__ import annotations

import argparse
import ctypes
import os
import sqlite3
import subprocess
import time
from pathlib import Path

from .config import (
    SKIP_DIR_NAMES, STATE_DIR, TMP_DIR, assert_writable, cfg, child_env,
    ensure_dirs, get_logger, is_indexable,
)
from .errors import AgentError

log = get_logger("indexer")
INDEX_DB = STATE_DIR / "index.db"
MAX_TEXT_BYTES = 256_000
MAX_PDF_PAGES = 60
TEXT_EXT = {".txt", ".md", ".py", ".json", ".csv", ".log", ".yaml", ".yml", ".ini",
            ".html", ".htm", ".js", ".ts", ".css", ".xml", ".ahk", ".bat", ".ps1", ".sql"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".bmp", ".tif", ".tiff", ".webp"}

_SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    path TEXT PRIMARY KEY, name TEXT, ext TEXT, size INTEGER,
    mtime REAL, kind TEXT, quality TEXT, indexed_ts REAL
);
CREATE VIRTUAL TABLE IF NOT EXISTS fts USING fts5(
    path UNINDEXED, name, content, tokenize='unicode61 remove_diacritics 2'
);
"""


def connect() -> sqlite3.Connection:
    ensure_dirs()
    db = sqlite3.connect(str(INDEX_DB))
    db.row_factory = sqlite3.Row
    try:
        db.executescript(_SCHEMA)
    except sqlite3.OperationalError as exc:
        raise AgentError(f"SQLite شما FTS5 ندارد: {exc}") from exc
    return db


def fixed_drives() -> list[str]:
    kernel = ctypes.windll.kernel32
    mask = kernel.GetLogicalDrives()
    drives = []
    for index in range(26):
        if mask & (1 << index):
            root = f"{chr(65 + index)}:\\"
            if kernel.GetDriveTypeW(root) == 3 and is_indexable(root):
                drives.append(root)
    return drives


def iter_files(roots: list[str]):
    for root in roots:
        if not is_indexable(root):
            continue
        for dirpath, dirnames, filenames in os.walk(root):
            dirnames[:] = [d for d in dirnames
                           if d.lower() not in SKIP_DIR_NAMES
                           and is_indexable(Path(dirpath) / d)]
            for name in filenames:
                yield Path(dirpath) / name


def ocr_text(path: Path) -> tuple[str, str]:
    script = Path(__file__).with_name("ocr.ps1")
    if not script.exists():
        return "", "ocr_missing"
    out = assert_writable(TMP_DIR / f"ocr_{int(time.time() * 1000)}.txt")
    out.parent.mkdir(parents=True, exist_ok=True)
    command = ["powershell", "-NoProfile", "-ExecutionPolicy", "Bypass",
               "-File", str(script), "-Path", str(path), "-Out", str(out)]
    try:
        result = subprocess.run(command, env=child_env(), capture_output=True, timeout=90)
    except (OSError, subprocess.TimeoutExpired):
        return "", "ocr_failed"
    if result.returncode != 0 or not out.exists():
        return "", "ocr_unavailable"
    text = out.read_text(encoding="utf-8-sig", errors="replace").strip()
    out.unlink(missing_ok=True)
    return text, ("ocr" if text else "ocr_empty")


def extract(path: Path, ocr: bool = False) -> tuple[str, str, str]:
    ext = path.suffix.lower()
    try:
        size = path.stat().st_size
    except OSError:
        return "", "other", "unreadable"
    if ext in TEXT_EXT:
        try:
            with path.open("rb") as handle:
                data = handle.read(MAX_TEXT_BYTES)
        except OSError:
            return "", "text", "unreadable"
        quality = "truncated" if size > MAX_TEXT_BYTES else "full"
        return data.decode("utf-8", errors="replace"), "text", quality
    if ext == ".pdf":
        if size > int(cfg("pdf_max_bytes")):
            return "", "pdf", "too_large"
        try:
            from pypdf import PdfReader
            reader = PdfReader(str(path))
            pages = [(page.extract_text() or "") for page in reader.pages[:MAX_PDF_PAGES]]
        except Exception as exc:  # pypdf raises many unrelated types
            log.debug("pdf extract failed for %s: %s", path, exc)
            return "", "pdf", "extract_failed"
        text = "\n".join(pages).strip()
        return text, "pdf", ("full" if text else "no_text_layer")
    if ext in IMAGE_EXT:
        if ocr:
            text, quality = ocr_text(path)
            return text, "image", quality
        return "", "image", "name_only"
    return "", "other", "name_only"


def index_file(db: sqlite3.Connection, path: Path, content: bool, ocr: bool) -> str:
    try:
        stat = path.stat()
    except OSError:
        return "error"
    row = db.execute("SELECT size, mtime FROM files WHERE path=?", (str(path),)).fetchone()
    if row and row["size"] == stat.st_size and abs(row["mtime"] - stat.st_mtime) < 1:
        return "skipped"
    text, kind, quality = extract(path, ocr) if content else ("", "other", "name_only")
    db.execute("DELETE FROM fts WHERE path=?", (str(path),))
    db.execute("INSERT INTO fts(path, name, content) VALUES(?,?,?)",
               (str(path), path.name, text))
    db.execute(
        "INSERT INTO files(path, name, ext, size, mtime, kind, quality, indexed_ts) "
        "VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(path) DO UPDATE SET name=excluded.name, "
        "ext=excluded.ext, size=excluded.size, mtime=excluded.mtime, kind=excluded.kind, "
        "quality=excluded.quality, indexed_ts=excluded.indexed_ts",
        (str(path), path.name, path.suffix.lower(), stat.st_size, stat.st_mtime,
         kind, quality, time.time()),
    )
    return "indexed"


def build(roots: list[str], content: bool = True, ocr: bool = False,
          limit: int | None = None) -> dict:
    db = connect()
    stats = {"indexed": 0, "skipped": 0, "error": 0}
    seen = 0
    try:
        for path in iter_files(roots):
            stats[index_file(db, path, content, ocr)] += 1
            seen += 1
            if seen % 200 == 0:
                db.commit()
                print(f"  … {seen} فایل بررسی شد")
            if limit and seen >= limit:
                break
        db.commit()
    finally:
        db.close()
    return stats


def search(query: str, limit: int = 20) -> list[dict]:
    terms = [word.replace('"', "") for word in query.split() if word.strip()]
    if not terms:
        return []
    expression = " AND ".join(f'"{word}"' for word in terms)
    db = connect()
    try:
        rows = db.execute(
            "SELECT path, snippet(fts, 2, '[', ']', '…', 12) AS snip "
            "FROM fts WHERE fts MATCH ? LIMIT ?", (expression, limit),
        ).fetchall()
        return [{"path": row["path"], "snippet": row["snip"]} for row in rows]
    except sqlite3.OperationalError as exc:
        raise AgentError(f"عبارت جست‌وجو نامعتبر: {exc}") from exc
    finally:
        db.close()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.indexer", description="ایندکس فایل‌ها")
    sub = parser.add_subparsers(dest="command", required=True)
    build_parser = sub.add_parser("build", help="ساخت/به‌روزرسانی ایندکس")
    build_parser.add_argument("--root", action="append", default=None)
    build_parser.add_argument("--no-content", action="store_true")
    build_parser.add_argument("--ocr", action="store_true")
    build_parser.add_argument("--limit", type=int, default=None)
    search_parser = sub.add_parser("search", help="جست‌وجو در نام و محتوا")
    search_parser.add_argument("query")
    search_parser.add_argument("--limit", type=int, default=20)
    args = parser.parse_args(argv)

    try:
        if args.command == "build":
            roots = args.root or fixed_drives()
            print("ریشه‌ها:", "، ".join(roots) or "هیچ (همه ممنوع‌اند)")
            stats = build(roots, content=not args.no_content, ocr=args.ocr, limit=args.limit)
            print("نتیجه:", stats)
            return 0
        hits = search(args.query, args.limit)
        if not hits:
            print("چیزی پیدا نشد.")
        for hit in hits:
            print(hit["path"])
            if hit["snippet"]:
                print("   ", hit["snippet"].replace("\n", " ")[:160])
        return 0
    except AgentError as exc:
        print("توقف:", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())