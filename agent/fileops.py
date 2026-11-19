"""Sandboxed file operations and browser-based PDF printing."""

from __future__ import annotations

import base64
import shutil
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from .config import CODE_ROOT, STATE_DIR, assert_writable, get_logger
from .errors import AgentError
from .notify import Notifier

log = get_logger("fileops")
A4_INCHES = (8.27, 11.69)


def resolve_target(path_str: str) -> Path:
    raw = str(path_str).strip().replace("/", "\\")
    candidate = Path(raw)
    if not candidate.is_absolute():
        candidate = CODE_ROOT / candidate
    return assert_writable(candidate)


def make_dir(path_str: str) -> Path:
    target = resolve_target(path_str)
    target.mkdir(parents=True, exist_ok=True)
    return target


def write_text(path_str: str, content: str, notifier: Notifier, overwrite: str = "ask") -> Path:
    target = resolve_target(path_str)
    if target.exists() and overwrite == "ask":
        granted = notifier.approval("overwrite", f"فایل موجود بازنویسی شود؟ {target}",
                                    {"path": str(target)})
        if not granted:
            raise AgentError(f"اجازهٔ بازنویسی داده نشد: {target}")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(content, encoding="utf-8")
    return target


def print_page_to_pdf(page, path_str: str) -> Path:
    target = resolve_target(path_str)
    target.parent.mkdir(parents=True, exist_ok=True)
    session = page.context.new_cdp_session(page)
    try:
        result = session.send("Page.printToPDF", {
            "printBackground": True,
            "paperWidth": A4_INCHES[0],
            "paperHeight": A4_INCHES[1],
            "marginTop": 0.4, "marginBottom": 0.4, "marginLeft": 0.4, "marginRight": 0.4,
        })
    except PlaywrightError as exc:
        raise AgentError(f"ساخت PDF ناموفق: {exc}") from exc
    finally:
        try:
            session.detach()
        except PlaywrightError:
            pass
    target.write_bytes(base64.b64decode(result["data"]))
    return target


def html_to_pdf(browser, html: str, path_str: str) -> Path:
    existing = [p for p in browser.context.pages if not p.is_closed()]
    temp = assert_writable(STATE_DIR / "tmp" / "pdf_source.html")
    temp.parent.mkdir(parents=True, exist_ok=True)
    temp.write_text(html, encoding="utf-8")
    page = browser.context.new_page()
    try:
        page.goto(temp.as_uri(), wait_until="load")
        return print_page_to_pdf(page, path_str)
    finally:
        try:
            page.close()
        except PlaywrightError:
            pass
        # Restore focus so later clicks land on the mission page, not a hidden tab.
        for previous in existing:
            if not previous.is_closed():
                try:
                    previous.bring_to_front()
                except PlaywrightError:
                    pass
                break


def promote_download(pending: Path, dest_str: str | None, notifier: Notifier,
                     mode: str, url: str = "") -> Path | None:
    if mode == "deny":
        notifier.emit("download_denied", {"path": str(pending)})
        return None
    if mode == "quarantine" or not dest_str:
        return pending
    target = resolve_target(dest_str)
    summary = f"دانلود ذخیره شود؟\n{pending.name}\nاز: {url}\nبه: {target}"
    if not notifier.approval("download", summary,
                             {"src": str(pending), "dest": str(target), "url": url}):
        notifier.emit("download_denied", {"path": str(pending)})
        return None
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.move(str(pending), str(target))
    notifier.emit("download_saved", {"path": str(target)})
    return target