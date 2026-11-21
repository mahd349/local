"""Report disk usage and retire obsolete artifacts.

Read-only unless --apply is given. Scripts are MOVED to a backup folder rather
than deleted, so a wrong guess is reversible. Databases that hold irreplaceable
evidence (corpus, frozen test set, learned morphology) are never touched.
"""
from __future__ import annotations

import argparse
import shutil
import sqlite3
import sys
import time
from pathlib import Path

sys.path.insert(0, r"D:\Python")
sys.path.insert(0, r"D:\project\library")

from agent.config import STATE_DIR

FARSI_DIR = Path(r"D:\Python\farsi")
LIB_DIR = Path(r"D:\project\library")
TRASH = FARSI_DIR / "_retired"

KEEP_DB = {
    "farsi_corpus.db", "farsi_usage.db", "farsi_lexicon.db", "farsi_frames.db",
    "farsi_eval.db", "farsi_morph.db", "farsi_syntax.db", "farsi_protected.db",
}
RETIRE_DB = {
    "farsi_roles.db": "نقش‌های خوشه‌ای، جایگزین شد با syntax",
    "farsi_coherence.db": "خوشه‌های نقش، جایگزین شد با syntax",
    "farsi_predict.db": "پیشنهادهای تولید معیوب قدیمی",
    "farsi_talk.db": "پیشنهادهای قالبی قدیمی",
    "farsi_constrained.db": "گزارش تولید معیوب قدیمی",
}
RETIRE_SCRIPTS = {
    "diagnose_corpus.py": "تشخیص انجام شد",
    "clean_wiki.py": "متن را خراب می‌کرد",
    "split_wiki.py": "لازم نشد",
    "test_add.py": "آزمون موقت",
    "test_corpus.py": "آزمون موقت",
    "probe_corpus.py": "کاوش انجام شد",
    "peek.py": "بازبینی موقت",
    "roles.py": "جایگزین شد با syntax",
    "coherence.py": "جایگزین شد با syntax",
}
KEEP_SCRIPTS = {
    "morph.py", "syntax.py", "fastbuild.py", "build_all.py",
    "ingest_wiki.py", "evaluator.py", "protected.py", "compare.py",
    "constrained.py", "cleanup.py",
}


def mb(size: int) -> str:
    return f"{size / 1024 / 1024:.1f} MB"


def gather() -> dict:
    state = Path(STATE_DIR)
    report = {"state_dir": str(state), "databases": [], "journals": [],
              "scripts": [], "library": []}

    if state.exists():
        for path in sorted(state.iterdir()):
            if not path.is_file():
                continue
            entry = {"name": path.name, "size": path.stat().st_size}
            if path.suffix == ".db":
                report["databases"].append(entry)
            elif path.name.endswith(("-wal", "-shm")):
                report["journals"].append(entry)

    if FARSI_DIR.exists():
        for path in sorted(FARSI_DIR.glob("*.py")):
            report["scripts"].append({"name": path.name,
                                      "size": path.stat().st_size})

    if LIB_DIR.exists():
        for path in sorted(LIB_DIR.iterdir()):
            if path.is_file():
                report["library"].append({"name": path.name,
                                          "size": path.stat().st_size})
            elif path.is_dir() and path.name in ("wiki_parts", "extracted"):
                total = sum(f.stat().st_size for f in path.rglob("*")
                            if f.is_file())
                report["library"].append({"name": path.name + "/",
                                          "size": total})
    return report


def show(report: dict) -> int:
    print("=" * 68)
    print("محل پایگاه‌های داده:", report["state_dir"])
    print("=" * 68)

    total_db = sum(e["size"] for e in report["databases"])
    print(f"\nپایگاه‌ها  (جمع {mb(total_db)})")
    for entry in sorted(report["databases"], key=lambda e: -e["size"]):
        name = entry["name"]
        if name in KEEP_DB:
            mark = "نگه‌داشتنی"
        elif name in RETIRE_DB:
            mark = "منسوخ: " + RETIRE_DB[name]
        else:
            mark = "ناشناس، دست نمی‌زنم"
        print(f"  {name:<28} {mb(entry['size']):>12}  {mark}")

    total_j = sum(e["size"] for e in report["journals"])
    if report["journals"]:
        print(f"\nفایل‌های ژورنال  (جمع {mb(total_j)}) — با checkpoint آزاد می‌شوند")
        for entry in sorted(report["journals"], key=lambda e: -e["size"])[:12]:
            print(f"  {entry['name']:<28} {mb(entry['size']):>12}")

    retire_scripts = [e for e in report["scripts"]
                      if e["name"] in RETIRE_SCRIPTS]
    if retire_scripts:
        print(f"\nاسکریپت‌های بازنشسته  (جمع {mb(sum(e['size'] for e in retire_scripts))})")
        for entry in retire_scripts:
            print(f"  {entry['name']:<28} {mb(entry['size']):>12}  "
                  f"{RETIRE_SCRIPTS[entry['name']]}")

    if report["library"]:
        total_lib = sum(e["size"] for e in report["library"])
        print(f"\nپوشهٔ کتابخانه  (جمع {mb(total_lib)})")
        for entry in sorted(report["library"], key=lambda e: -e["size"])[:15]:
            print(f"  {entry['name']:<28} {mb(entry['size']):>12}")

    reclaim = sum(e["size"] for e in report["databases"]
                  if e["name"] in RETIRE_DB) + total_j
    print("\n" + "=" * 68)
    print(f"فضای قابل بازیابی بدون از دست دادن دانش: {mb(reclaim)}")
    print("=" * 68)
    print("برای اجرای واقعی:  python -m farsi.cleanup --apply")
    return 0


def checkpoint() -> dict:
    freed = {}
    state = Path(STATE_DIR)
    for path in sorted(state.glob("*.db")):
        before = sum(f.stat().st_size for f in state.glob(path.name + "-*"))
        if before == 0:
            continue
        try:
            db = sqlite3.connect(str(path))
            db.execute("PRAGMA busy_timeout=60000")
            db.execute("PRAGMA wal_checkpoint(TRUNCATE)")
            db.close()
        except sqlite3.Error as exc:
            print(f"  {path.name}: {exc}")
            continue
        after = sum(f.stat().st_size for f in state.glob(path.name + "-*"))
        freed[path.name] = before - after
    return freed


def apply(report: dict, wipe_library: bool) -> dict:
    moved = []
    removed = []
    TRASH.mkdir(exist_ok=True)

    for entry in report["scripts"]:
        name = entry["name"]
        if name not in RETIRE_SCRIPTS or name in KEEP_SCRIPTS:
            continue
        source = FARSI_DIR / name
        if source.exists():
            shutil.move(str(source), str(TRASH / name))
            moved.append(name)

    state = Path(STATE_DIR)
    for name in RETIRE_DB:
        path = state / name
        if not path.exists():
            continue
        for suffix in ("", "-wal", "-shm"):
            target = Path(str(path) + suffix)
            if target.exists():
                size = target.stat().st_size
                target.unlink()
                removed.append((target.name, size))

    if wipe_library:
        for name in ("wiki_clean.txt",):
            path = LIB_DIR / name
            if path.exists():
                size = path.stat().st_size
                path.unlink()
                removed.append((name, size))
        folder = LIB_DIR / "wiki_parts"
        if folder.exists():
            size = sum(f.stat().st_size for f in folder.rglob("*") if f.is_file())
            shutil.rmtree(folder)
            removed.append(("wiki_parts/", size))

    return {"moved_scripts": moved, "removed": removed}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="farsi.cleanup",
                                description="گزارش و پاک‌سازی امن")
    p.add_argument("--apply", action="store_true", help="واقعاً اجرا کن")
    p.add_argument("--checkpoint", action="store_true",
                   help="فایل‌های ژورنال را جمع کن")
    p.add_argument("--library", action="store_true",
                   help="پاک‌سازی پوشهٔ کتابخانه هم انجام شود")
    a = p.parse_args(argv)

    report = gather()
    if not a.apply and not a.checkpoint:
        return show(report)

    if a.checkpoint:
        freed = checkpoint()
        print("ژورنال‌های جمع‌شده:")
        for name, size in freed.items():
            print(f"  {name:<28} {mb(size)}")
        print(f"جمع: {mb(sum(freed.values()))}")

    if a.apply:
        result = apply(report, a.library)
        print("\nاسکریپت‌های منتقل‌شده به", TRASH)
        for name in result["moved_scripts"]:
            print("  ", name)
        print("\nفایل‌های حذف‌شده:")
        for name, size in result["removed"]:
            print(f"  {name:<28} {mb(size)}")
        print(f"\nجمع آزادشده: {mb(sum(s for _, s in result['removed']))}")
        print("\nنکته: اسکریپت‌ها حذف نشدند، به _retired منتقل شدند.")
        print("اگر چیزی خراب شد، از آن پوشه برگردان.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())