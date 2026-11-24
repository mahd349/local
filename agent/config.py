"""Central paths, limits and runtime environment.

All writable locations live on D: by user constraint. C: is excluded from
indexing; installed browsers on C: are launched but never written to.
"""

from __future__ import annotations

import json
import logging
import os
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Any
import sys
from pathlib import Path
VERSION = "0.1.0"

CODE_ROOT = Path(r"D:\Python")
WORK_ROOT = Path(r"D:\Work")
LIB_DIR = WORK_ROOT / "Scripts" / "lib"
BROWSER_CACHE_DIR = WORK_ROOT / "Scripts" / "browsers"
DOWNLOAD_DIR = WORK_ROOT / "downloads"
PROFILE_DIR = WORK_ROOT / "agent-profile"
LIB_DIR = Path(r"D:\project\library")
if LIB_DIR.is_dir() and str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

STATE_DIR = WORK_ROOT / "agent-state"
TMP_DIR = STATE_DIR / "tmp"
LOG_DIR = STATE_DIR / "logs"
MISSION_DIR = STATE_DIR / "missions"
MACRO_DIR = STATE_DIR / "macros"
CAPTURE_DIR = STATE_DIR / "captures"
REPORT_DIR = STATE_DIR / "reports"
PIP_CACHE_DIR = STATE_DIR / "pip-cache"
DB_PATH = STATE_DIR / "agent.db"
TOKEN_PATH = STATE_DIR / "bridge.token"
USER_CONFIG_PATH = STATE_DIR / "config.json"

WRITE_ROOTS: tuple[Path, ...] = (CODE_ROOT, WORK_ROOT)
INDEX_EXCLUDE: tuple[Path, ...] = (
    Path("C:\\"),
    Path(r"D:\PC HP 650 lock"),
    Path(r"D:\Games"),
)
SKIP_DIR_NAMES = {
    "$recycle.bin", "system volume information", "node_modules", ".git",
    "__pycache__", "venv", ".venv", "windowsapps",
}

DEFAULTS: dict[str, Any] = {
    "bridge_host": "127.0.0.1",
    "bridge_port": 8765,
    "browser_channel": "msedge",
    "browser_sandbox": True,
    "browser_attach_cdp": False,
    "cdp_port": 9222,
    "chat_url": "https://chat.qwen.ai/",
    "fallback_chat_url": "https://chat.deepseek.com/",
    "ai_max_parts": 20,
    "ai_max_retries": 3,
    "ai_continue_text": "ادامه",
    "ai_done_marker": "اتمام کد",
    "run_timeout_sec": 60,
    "gui_grace_sec": 10,
    "fix_loops": 3,
    "pdf_max_bytes": 20 * 1024 * 1024,
    "approval_timeout_sec": 300,
    "download_allow_hosts": [],
    "action_timeout_ms": 15000,
    "nav_timeout_ms": 45000,
    "manual_wait_sec": 900,
    "guard_poll_sec": 2.0,
    "netcap_enabled": True,
    "netcap_body_max_bytes": 2000000,
    "screenshot_on_error": True,
    "mission_retry_attempts": 3,
    "mission_retry_delay_sec": 1.0,
    "download_mode": "approve",
    "overwrite_mode": "ask",
}

# Third-party packages are installed with --target, so the loader needs this.
if str(LIB_DIR) not in sys.path:
    sys.path.insert(0, str(LIB_DIR))

_config_cache: dict[str, Any] | None = None
_loggers: dict[str, logging.Logger] = {}


def ensure_dirs() -> None:
    for directory in (
        WORK_ROOT, LIB_DIR, DOWNLOAD_DIR, PROFILE_DIR, STATE_DIR, TMP_DIR,
        LOG_DIR, MISSION_DIR, MACRO_DIR, CAPTURE_DIR, REPORT_DIR, PIP_CACHE_DIR,
    ):
        directory.mkdir(parents=True, exist_ok=True)


def load_config(reload: bool = False) -> dict[str, Any]:
    global _config_cache
    if _config_cache is not None and not reload:
        return _config_cache
    merged = dict(DEFAULTS)
    if USER_CONFIG_PATH.exists():
        try:
            user_data = json.loads(USER_CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            raise RuntimeError(f"config.json readable nist: {exc}") from exc
        if not isinstance(user_data, dict):
            raise RuntimeError("config.json bayad yek JSON object bashad")
        unknown = set(user_data) - set(DEFAULTS)
        if unknown:
            raise RuntimeError("kelid-haye nashenas dar config.json: " + ", ".join(sorted(unknown)))
        merged.update(user_data)
    _config_cache = merged
    return merged


def cfg(key: str) -> Any:
    return load_config()[key]


def child_env(extra: dict[str, str] | None = None) -> dict[str, str]:
    """Environment for every subprocess: keeps heavy caches off C:."""
    env = os.environ.copy()
    env.update({
        "TMP": str(TMP_DIR),
        "TEMP": str(TMP_DIR),
        "TMPDIR": str(TMP_DIR),
        "PIP_CACHE_DIR": str(PIP_CACHE_DIR),
        "PLAYWRIGHT_BROWSERS_PATH": str(BROWSER_CACHE_DIR),
        "PYTHONUTF8": "1",
        "PYTHONIOENCODING": "utf-8",
    })
    if extra:
        env.update(extra)
    return env


def norm(path: str | Path) -> str:
    return os.path.normcase(os.path.abspath(os.path.expandvars(str(path))))


def is_within(path: str | Path, root: str | Path) -> bool:
    target, base = norm(path), norm(root)
    return target == base or target.startswith(base.rstrip("\\") + "\\")


def assert_writable(path: str | Path) -> Path:
    """Resolve first so junctions/symlinks cannot escape the D: jail."""
    resolved = Path(os.path.expandvars(str(path))).resolve(strict=False)
    if not any(is_within(resolved, root) for root in WRITE_ROOTS):
        raise PermissionError(f"neveshtan kharej az masir-haye mojaz masdud shod: {resolved}")
    return resolved


def is_indexable(path: str | Path) -> bool:
    return not any(is_within(path, bad) for bad in INDEX_EXCLUDE)


def get_logger(name: str = "agent") -> logging.Logger:
    if name in _loggers:
        return _loggers[name]
    ensure_dirs()
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    formatter = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    file_handler = RotatingFileHandler(
        LOG_DIR / "agent.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"
    )
    file_handler.setFormatter(formatter)
    stream_handler = logging.StreamHandler(sys.stdout)
    stream_handler.setFormatter(formatter)
    logger.addHandler(file_handler)
    logger.addHandler(stream_handler)
    _loggers[name] = logger
    return logger