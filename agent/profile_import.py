"""One-time import of an existing browser profile into the agent profile.

Reads the real profile from C: (read-only) and writes only to D:. Caches are
skipped to keep the copy small. Newer Edge builds may use app-bound cookie
encryption, so logins are not guaranteed to survive the move.
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
from pathlib import Path

from .config import PROFILE_DIR, USER_CONFIG_PATH, assert_writable, cfg, get_logger
from .errors import AgentError

log = get_logger("profile")

ROOTS = {
    "msedge": Path(os.path.expandvars(r"%LOCALAPPDATA%\Microsoft\Edge\User Data")),
    "chrome": Path(os.path.expandvars(r"%LOCALAPPDATA%\Google\Chrome\User Data")),
}
SKIP_DIRS = {
    "cache", "code cache", "gpucache", "dawncache", "grshadercache",
    "shadercache", "service worker", "crashpad", "browsermetrics",
    "componentcrx", "shared_proto_db", "segmentation platform",
    "optimization_guide_hint_cache_store", "video_decode_stats",
}
SKIP_EXT = {".log", ".tmp", ".old", ".bak", ".pma"}


def _root(channel: str) -> Path:
    root = ROOTS.get(channel)
    if root is None or not root.exists():
        raise AgentError(f"پروفایل‌های {channel} پیدا نشد: {root}")
    return root


def _local_state(root: Path) -> dict:
    path = root / "Local State"
    if not path.exists():
        raise AgentError(f"Local State پیدا نشد: {path}")
    return json.loads(path.read_text(encoding="utf-8", errors="replace"))


def list_profiles(channel: str) -> list[dict]:
    root = _root(channel)
    cache = _local_state(root).get("profile", {}).get("info_cache", {})
    out = []
    for folder, info in cache.items():
        if (root / folder).is_dir():
            out.append({"folder": folder, "display": info.get("name", folder),
                        "email": info.get("user_name", "")})
    return out


def pick_folder(channel: str, wanted: str) -> str:
    profiles = list_profiles(channel)
    lowered = wanted.strip().lower()
    for item in profiles:
        if lowered in (item["folder"].lower(), item["display"].lower()):
            return item["folder"]
    names = ", ".join(f'{p["display"]} ({p["folder"]})' for p in profiles)
    raise AgentError(f"پروفایل «{wanted}» پیدا نشد. موجود: {names}")


def _wanted_files(source: Path):
    for dirpath, dirnames, filenames in os.walk(source):
        dirnames[:] = [d for d in dirnames if d.lower() not in SKIP_DIRS]
        for name in filenames:
            if Path(name).suffix.lower() in SKIP_EXT:
                continue
            yield Path(dirpath) / name


def measure(source: Path) -> tuple[int, int]:
    total = count = 0
    for path in _wanted_files(source):
        try:
            total += path.stat().st_size
            count += 1
        except OSError:
            continue
    return count, total


def mb(size: int) -> str:
    return f"{size / 1048576:.1f} MB"


def _write_local_state(state: dict, info: dict, target_root: Path) -> None:
    # Only the encryption key and a Default entry are needed; the rest of
    # Local State is machine-specific and Edge rebuilds it.
    minimal = {
        "os_crypt": state.get("os_crypt", {}),
        "profile": {
            "info_cache": {"Default": info or {"name": "Agent"}},
            "last_used": "Default",
            "last_active_profiles": ["Default"],
        },
    }
    path = assert_writable(target_root / "Local State")
    path.write_text(json.dumps(minimal, ensure_ascii=False), encoding="utf-8")


def _ensure_config() -> None:
    data: dict = {}
    if USER_CONFIG_PATH.exists():
        try:
            loaded = json.loads(USER_CONFIG_PATH.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                data = loaded
        except ValueError:
            data = {}
    data["browser_attach_cdp"] = False
    data.setdefault("chat_url", "https://chat.qwen.ai/")
    path = assert_writable(USER_CONFIG_PATH)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")


def import_profile(channel: str, wanted: str, force: bool = False) -> dict:
    root = _root(channel)
    folder = pick_folder(channel, wanted)
    source = root / folder
    state = _local_state(root)
    info = state.get("profile", {}).get("info_cache", {}).get(folder, {})
    target_root = assert_writable(PROFILE_DIR)
    target = assert_writable(target_root / "Default")
    if target.exists() and not force:
        raise AgentError(
            f"پروفایل ایجنت از قبل وجود دارد: {target}\n"
            "مرورگر ایجنت را ببند و با --force دوباره اجرا کن."
        )
    if target.exists():
        shutil.rmtree(target)
    target.mkdir(parents=True, exist_ok=True)
    copied = failed = size = 0
    for path in _wanted_files(source):
        dest = assert_writable(target / path.relative_to(source))
        dest.parent.mkdir(parents=True, exist_ok=True)
        try:
            shutil.copy2(path, dest)
            copied += 1
            size += dest.stat().st_size
        except (OSError, shutil.Error):
            failed += 1
    _write_local_state(state, info, target_root)
    _ensure_config()
    return {"folder": folder, "copied": copied, "failed": failed, "bytes": size}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.profile_import",
                                     description="وارد کردن پروفایل مرورگر به D:")
    parser.add_argument("--channel", default=cfg("browser_channel"))
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("list", help="فهرست پروفایل‌های واقعی")
    imp = sub.add_parser("import", help="کپی پروفایل انتخابی")
    imp.add_argument("--profile", required=True)
    imp.add_argument("--yes", action="store_true")
    imp.add_argument("--force", action="store_true")
    args = parser.parse_args(argv)
    try:
        if args.command == "list":
            for item in list_profiles(args.channel):
                print(f'{item["display"]}  ->  {item["folder"]}  {item["email"]}')
            return 0
        folder = pick_folder(args.channel, args.profile)
        count, total = measure(_root(args.channel) / folder)
        print(f"پروفایل «{args.profile}» = پوشهٔ {folder}")
        print(f"{count} فایل، {mb(total)} (بدون کش)")
        if not args.yes:
            print("این فقط برآورد بود؛ برای کپی واقعی --yes را اضافه کن.")
            return 0
        result = import_profile(args.channel, args.profile, force=args.force)
        print(f"کپی شد: {result['copied']} فایل، {mb(result['bytes'])}؛ ناموفق: {result['failed']}")
        print("اگر لاگین منتقل نشد، یک‌بار در مرورگر ایجنت دستی وارد شو.")
        return 0
    except (AgentError, OSError, ValueError) as exc:
        print("توقف:", exc)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())