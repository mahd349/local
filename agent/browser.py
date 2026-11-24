"""Edge/Chrome control in two modes.

Launch mode: our own persistent profile on D:.
Attach mode: connect to a browser window the user opened themselves (CDP).
Attaching keeps navigator.webdriver false, so manual logins (Google) behave
like a real person. Downloads are quarantined on D: in both modes.
"""

from __future__ import annotations

import re
import time
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page, sync_playwright

from .config import (
    DOWNLOAD_DIR, PROFILE_DIR, STATE_DIR, assert_writable, cfg, get_logger,
)
from .errors import AgentError
from .notify import Notifier
from .store import Store

log = get_logger("browser")
SHOT_DIR = STATE_DIR / "shots"
PENDING_DIR = DOWNLOAD_DIR / "_pending"
TEST_PROFILE_DIR = STATE_DIR / "test-profile"


def _isolated() -> bool:
    """Test runs get their own browser so leftover tabs cannot poison them."""
    import os
    return os.environ.get("AGENT_ISOLATED") == "1"


def _profile_dir():
    return TEST_PROFILE_DIR if _isolated() else PROFILE_DIR
_BAD_NAME = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_BLANK_URLS = {"about:blank", "", "chrome://newtab/", "edge://newtab/", "chrome://new-tab-page/"}


def safe_name(name: str) -> str:
    cleaned = _BAD_NAME.sub("_", name).strip(" .") or "file"
    return cleaned[:120]


def _is_blank(page: Page) -> bool:
    try:
        return page.url.strip() in _BLANK_URLS
    except PlaywrightError:
        return False


class Browser:
    def __init__(self, store: Store, notifier: Notifier):
        self.store = store
        self.notifier = notifier
        self._pw = None
        self._ctx = None
        self._attached = False
        self._pending_downloads: list[Any] = []
        self.saved_downloads: list[Path] = []

    def __enter__(self) -> "Browser":
        self.start()
        return self

    def __exit__(self, *exc_info) -> None:
        self.close()

    def start(self):
        assert_writable(_profile_dir())
        SHOT_DIR.mkdir(parents=True, exist_ok=True)
        PENDING_DIR.mkdir(parents=True, exist_ok=True)
        self._attached = False
        # One Playwright loop per process: a second start() raises inside asyncio.
        self._pw = sync_playwright().start()
        if cfg("browser_attach_cdp") and not _isolated():
            self._attach()
        else:
            self._launch()
        self._ctx.set_default_timeout(cfg("action_timeout_ms"))
        self._ctx.set_default_navigation_timeout(cfg("nav_timeout_ms"))
        self._ctx.on("page", self._wire_page)
        for page in self._ctx.pages:
            self._wire_page(page)
        if not self._attached:
            self._prune_blank_pages()
        log.info("browser ready (attached=%s)", self._attached)
        return self._ctx

    def _attach(self):
        port = int(cfg("cdp_port"))
        try:
            remote = self._pw.chromium.connect_over_cdp(f"http://127.0.0.1:{port}")
        except PlaywrightError as exc:
            self.close()
            raise AgentError(
                f"اتصال به مرورگر روی پورت {port} ممکن نشد. "
                "اول «Agent Browser.cmd» را دابل‌کلیک کن، بعد دوباره امتحان کن."
            ) from exc
        if not remote.contexts:
            self.close()
            raise AgentError("مرورگر متصل هیچ پنجره‌ای ندارد")
        self._ctx = remote.contexts[0]
        self._attached = True
        self._quarantine_via_cdp()

    def _quarantine_via_cdp(self):
        # CDP cannot set downloads_path at launch; enforce the D: folder here.
        try:
            page = self._ctx.pages[0] if self._ctx.pages else self._ctx.new_page()
            session = self._ctx.new_cdp_session(page)
            session.send("Browser.setDownloadBehavior", {
                "behavior": "allow",
                "downloadPath": str(PENDING_DIR),
                "eventsEnabled": True,
            })
        except PlaywrightError as exc:
            log.warning("download quarantine not enforced: %s", exc)

    def _launch(self):
        try:
            self._ctx = self._pw.chromium.launch_persistent_context(
                user_data_dir=str(_profile_dir()),
                channel=cfg("browser_channel"),
                headless=False,
                chromium_sandbox=bool(cfg("browser_sandbox")),
                accept_downloads=True,
                downloads_path=str(PENDING_DIR),
                no_viewport=True,
                timezone_id="Asia/Tehran",
                args=["--start-maximized", "--no-first-run",
                      "--no-default-browser-check"],
            )
        except PlaywrightError as exc:
            self.close()
            raise AgentError(
                "مرورگر بالا نیامد. اگر پنجرهٔ ایجنت از قبل باز است آن را ببند. "
                f"جزئیات: {exc}"
            ) from exc







    def _prune_blank_pages(self) -> None:
        pages = self.pages()
        blanks = [p for p in pages if _is_blank(p)]
        keep = 1 if len(blanks) == len(pages) else 0
        for page in blanks[keep:]:
            try:
                page.close()
            except PlaywrightError:
                pass

    def _wire_page(self, page: Page) -> None:
        page.on("download", lambda download: self._on_download(page, download))
        page.on("crash", lambda _: self.notifier.emit("browser_crash", {"url": page.url}))

    def _on_download(self, page: Page, download) -> None:
        self._pending_downloads.append(download)
        self.notifier.emit("download_pending", {
            "name": download.suggested_filename, "url": download.url, "page": page.url,
        })

    def save_download(self, download) -> Path:
        target = assert_writable(PENDING_DIR / safe_name(download.suggested_filename))
        counter = 1
        while target.exists():
            target = target.with_name(f"{target.stem}_{counter}{target.suffix}")
            counter += 1
        download.save_as(str(target))
        if download in self._pending_downloads:
            self._pending_downloads.remove(download)
        self.saved_downloads.append(target)
        self.notifier.emit("download_saved", {"path": str(target)})
        return target

    def flush_downloads(self) -> list[Path]:
        saved: list[Path] = []
        still_pending: list[Any] = []
        for download in self._pending_downloads:
            try:
                saved.append(self.save_download(download))
            except (PlaywrightError, OSError, PermissionError) as exc:
                log.warning("download failed: %s", exc)
                still_pending.append(download)
        self._pending_downloads = still_pending
        return saved

    @property
    def context(self):
        if self._ctx is None:
            raise AgentError("مرورگر هنوز اجرا نشده است")
        return self._ctx

    def pages(self) -> list[Page]:
        return [p for p in self.context.pages if not p.is_closed()]

    def new_page(self, url: str | None = None) -> Page:
        blank = next((p for p in self.pages() if _is_blank(p)), None)
        page = blank if blank is not None else self.context.new_page()
        if url:
            page.goto(url, wait_until="domcontentloaded")
        try:
            page.bring_to_front()
        except PlaywrightError:
            pass
        return page

    def active_page(self) -> Page:
        candidates = self.pages()
        if not candidates:
            return self.new_page()
        for page in candidates:
            try:
                if page.evaluate("() => document.hasFocus() && document.visibilityState === 'visible'"):
                    return page
            except PlaywrightError:
                continue
        for page in candidates:
            try:
                if page.evaluate("() => document.visibilityState === 'visible'"):
                    return page
            except PlaywrightError:
                continue
        return candidates[-1]

    def screenshot(self, label: str = "shot") -> Path | None:
        if not cfg("screenshot_on_error"):
            return None
        try:
            page = self.active_page()
            target = assert_writable(SHOT_DIR / f"{safe_name(label)}_{int(time.time())}.png")
            page.screenshot(path=str(target), full_page=False)
            return target
        except (PlaywrightError, OSError, PermissionError) as exc:
            log.warning("screenshot failed: %s", exc)
            return None

    def close(self) -> None:
        if self._ctx is not None:
            if not self._attached:
                for page in self.pages():
                    try:
                        page.wait_for_timeout(150)
                    except PlaywrightError:
                        pass
                    break
                try:
                    self.flush_downloads()
                except (PlaywrightError, OSError):
                    pass
                try:
                    self._ctx.close()
                except PlaywrightError:
                    pass
        self._ctx = None
        if self._pw is not None:
            try:
                self._pw.stop()
            except PlaywrightError:
                pass
            self._pw = None