"""Captcha / login walls: stop, tell the user, wait, resume.

Detection is deliberately conservative to avoid false alarms: a captcha
needs a strong phrase or a known widget, a login needs a visible password
field plus a second signal.
"""

from __future__ import annotations

import time

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .config import cfg, get_logger
from .errors import BlockedByGuard
from .notify import Notifier

log = get_logger("guard")

CAPTCHA_PHRASES = (
    "access verification",
    "drag to complete the puzzle",
    "verify that you are a real person",
    "verify you are human",
    "slide to verify",
    "i'm not a robot",
    "unusual traffic",
    "کد امنیتی",
    "تأیید امنیتی",
    "ثابت کنید که ربات نیستید",
)
CAPTCHA_SELECTORS = (
    "iframe[src*='captcha']",
    "iframe[src*='challenges.cloudflare.com']",
    "#nocaptcha",
    ".nc-container",
    ".geetest_panel",
    ".g-recaptcha",
    "[data-sitekey]",
)
LOGIN_URL_HINTS = (
    "/login", "/signin", "/sign-in", "/auth", "accounts.google.com",
    "login.microsoftonline.com", "passport.",
)
LOGIN_PHRASES = (
    "sign in to continue", "log in to continue", "welcome back",
    "برای ادامه وارد شوید", "ورود به حساب", "وارد حساب خود شوید",
)


def _body_text(page: Page) -> str:
    try:
        return str(page.evaluate(
            "() => document.body ? document.body.innerText.slice(0, 6000) : ''"
        )).lower()
    except PlaywrightError:
        return ""


def _visible(page: Page, selector: str) -> bool:
    try:
        locator = page.locator(selector)
        return locator.count() > 0 and locator.first.is_visible()
    except PlaywrightError:
        return False


def detect(page: Page) -> dict | None:
    url = page.url.lower()
    text = _body_text(page)

    for phrase in CAPTCHA_PHRASES:
        if phrase in text:
            return {"kind": "captcha", "evidence": phrase, "url": page.url}
    for selector in CAPTCHA_SELECTORS:
        if _visible(page, selector):
            return {"kind": "captcha", "evidence": selector, "url": page.url}

    if _visible(page, "input[type='password']"):
        url_hit = next((h for h in LOGIN_URL_HINTS if h in url), None)
        text_hit = next((p for p in LOGIN_PHRASES if p in text), None)
        if url_hit or text_hit:
            return {"kind": "login", "evidence": url_hit or text_hit, "url": page.url}
    return None


def ensure_clear(page: Page, notifier: Notifier, wait: bool = True,
                 timeout_sec: float | None = None, screenshot=None) -> None:
    state = detect(page)
    if state is None:
        return
    shot = screenshot(state["kind"]) if callable(screenshot) else None
    notifier.emit(state["kind"], {**state, "screenshot": str(shot) if shot else ""})
    log.warning("%s detected: %s", state["kind"], state["evidence"])
    try:
        page.bring_to_front()
    except PlaywrightError:
        pass
    if not wait:
        raise BlockedByGuard(f"{state['kind']} در صفحه دیده شد: {state['evidence']}")

    deadline = time.time() + (timeout_sec if timeout_sec is not None else cfg("manual_wait_sec"))
    while time.time() < deadline:
        notifier.check_stop()
        time.sleep(cfg("guard_poll_sec"))
        if detect(page) is None:
            notifier.emit("guard_cleared", {"url": page.url})
            log.info("guard cleared, resuming")
            return
    raise BlockedByGuard(f"{state['kind']} رفع نشد و زمان انتظار تمام شد")