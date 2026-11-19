"""Announced, D:-only dependency installation. No browser binaries are fetched."""

from __future__ import annotations

import importlib
import subprocess
import sys
from dataclasses import dataclass

from .config import LIB_DIR, child_env, ensure_dirs, get_logger

log = get_logger("installer")


@dataclass(frozen=True)
class Package:
    name: str
    module: str
    purpose: str
    approx_mb: str


PACKAGES: tuple[Package, ...] = (
    Package("playwright", "playwright", "کنترل واقعی مرورگر + شنود شبکه (CDP)", "~40"),
    Package("pyyaml", "yaml", "خواندن مأموریت‌های YAML", "~1"),
    Package("pypdf", "pypdf", "استخراج متن PDF برای ایندکس", "~1"),
)


def plan_text() -> str:
    lines = [
        "فهرست دانلودها (همه در D:\\Work\\Scripts\\lib نصب می‌شوند):",
        "",
    ]
    for pkg in PACKAGES:
        lines.append(f"  - {pkg.name:<12} {pkg.approx_mb:>5} MB   {pkg.purpose}")
    lines += [
        "",
        "وابستگی‌های خودکار: greenlet، pyee، typing-extensions (کم‌حجم).",
        "هیچ مرورگری دانلود نمی‌شود؛ از Edge/Chrome نصب‌شده استفاده می‌کنیم.",
        "حجم‌ها تقریبی‌اند و کش pip هم روی D: می‌ماند.",
    ]
    return "\n".join(lines)


def missing_packages() -> list[Package]:
    ensure_dirs()
    missing = []
    for pkg in PACKAGES:
        try:
            importlib.import_module(pkg.module)
        except ImportError:
            missing.append(pkg)
    return missing


def install(packages: list[Package], assume_yes: bool = False) -> int:
    if not packages:
        print("همه کتابخانه‌ها نصب هستند.")
        return 0
    print(plan_text())
    print()
    if not assume_yes:
        answer = input("اجازه نصب می‌دهی؟ [y/N] ").strip().lower()
        if answer not in ("y", "yes", "بله", "b"):
            print("نصب لغو شد.")
            return 1
    command = [
        sys.executable, "-m", "pip", "install",
        "--target", str(LIB_DIR),
        "--upgrade",
        "--disable-pip-version-check",
        "--no-input",
        *[pkg.name for pkg in packages],
    ]
    print("دستور اجرا:", " ".join(command))
    result = subprocess.run(command, env=child_env(), cwd=str(LIB_DIR.parent))
    if result.returncode != 0:
        log.error("pip failed with code %s", result.returncode)
        return result.returncode
    importlib.invalidate_caches()
    for pkg in packages:
        try:
            module = importlib.import_module(pkg.module)
        except ImportError as exc:
            log.error("import %s failed after install: %s", pkg.module, exc)
            return 1
        print(f"نصب شد: {pkg.name} ({getattr(module, '__version__', 'version n/a')})")
    return 0