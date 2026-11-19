"""Takeover Mode: a tiny command language against the active tab."""

from __future__ import annotations

import shlex

from . import fileops, guard, locator
from .dsl import parse_duration
from .errors import AgentError
from .notify import Notifier
from .store import Store

HELP = (
    "go <url> | click <target> | fill <target> = <value> | press <target> <Key> | "
    "read <target> | wait <2s> | shot [name] | pdf <path> | title | url | back | "
    "reload | help | exit"
)


def parse_target(token: str) -> dict:
    if token.startswith("css="):
        return {"css": token[4:]}
    if token.startswith("text="):
        return {"text": token[5:]}
    if token.startswith("ph="):
        return {"placeholder": token[3:]}
    if token.startswith(("#", ".")) or "[" in token:
        return {"css": token}
    return {"text": token}


def execute(browser, store: Store, notifier: Notifier, line: str) -> str:
    text = line.strip()
    if not text:
        return ""
    parts = shlex.split(text)
    command = parts[0].lower()
    notifier.check_stop()
    page = browser.active_page()

    if command == "help":
        return HELP
    if command == "url":
        return page.url
    if command == "title":
        return page.title()
    if command == "back":
        page.go_back(wait_until="domcontentloaded")
        return page.url
    if command == "reload":
        page.reload(wait_until="domcontentloaded")
        return page.url
    if command == "go":
        if len(parts) < 2:
            raise AgentError("go به یک آدرس نیاز دارد")
        page.goto(parts[1], wait_until="domcontentloaded")
        guard.ensure_clear(page, notifier, wait=True, screenshot=browser.screenshot)
        return page.url
    if command == "wait":
        seconds = parse_duration(parts[1] if len(parts) > 1 else "1s")
        page.wait_for_timeout(seconds * 1000)
        return f"{seconds}s"
    if command == "shot":
        return str(browser.screenshot(parts[1] if len(parts) > 1 else "takeover"))
    if command == "pdf":
        if len(parts) < 2:
            raise AgentError("pdf به یک مسیر نیاز دارد")
        return str(fileops.print_page_to_pdf(page, parts[1]))
    if command == "fill":
        if " = " not in text:
            raise AgentError("ساختار درست: fill <target> = <value>")
        head, value = text.split(" = ", 1)
        tokens = shlex.split(head)
        if len(tokens) < 2:
            raise AgentError("fill به یک هدف نیاز دارد")
        locator.fill(page, parse_target(tokens[1]), value.strip(), store)
        return "filled"
    if command in ("click", "read", "press"):
        if len(parts) < 2:
            raise AgentError(f"{command} به یک هدف نیاز دارد")
        target = parse_target(parts[1])
        if command == "click":
            locator.click(page, target, store)
            return "clicked"
        if command == "read":
            return locator.text_of(page, target, store)
        key = parts[2] if len(parts) > 2 else "Enter"
        locator.press(page, target, key, store)
        return key
    raise AgentError(f"دستور ناشناس: {command}. help را بزن.")