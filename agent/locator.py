"""Self-healing element resolution.

A target is a semantic description, not a brittle CSS path. Candidates are
tried in reliability order; the winner is remembered per origin and the
durable attribute of the matched element is learned for next time.
"""

from __future__ import annotations

import hashlib
import json
import time
from typing import Any
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Locator, Page

from .config import get_logger
from .errors import TargetNotFound
from .store import Store

log = get_logger("locator")

ROUNDS = 3
ROUND_SLEEP = 0.7
_DERIVE_JS = r"""
el => {
  const prefer = ['data-testid', 'data-test', 'data-qa', 'name', 'aria-label', 'id'];
  for (const a of prefer) {
    const v = el.getAttribute(a);
    if (v && v.length < 80 && !/^[0-9]+$/.test(v) && !/[0-9a-f]{8,}/i.test(v)) {
      const sel = el.tagName.toLowerCase() + '[' + a + '=' + JSON.stringify(v) + ']';
      try { if (document.querySelectorAll(sel).length === 1) return sel; } catch (e) {}
    }
  }
  let node = el;
  const parts = [];
  while (node && node.nodeType === 1 && parts.length < 5) {
    let part = node.tagName.toLowerCase();
    const parent = node.parentElement;
    if (parent) {
      const same = Array.from(parent.children).filter(c => c.tagName === node.tagName);
      if (same.length > 1) part += ':nth-of-type(' + (same.indexOf(node) + 1) + ')';
    }
    parts.unshift(part);
    node = parent;
    if (node && node.id) { parts.unshift('#' + node.id); break; }
  }
  return parts.join(' > ');
}
"""
_HIGHLIGHT_JS = """
el => {
  const old = el.style.outline;
  el.style.outline = '3px solid #ff9800';
  setTimeout(() => { el.style.outline = old; }, 800);
  return true;
}
"""


def origin_of(page: Page) -> str:
    parts = urlsplit(page.url)
    return f"{parts.scheme}://{parts.netloc}"


def target_key(target: dict | str) -> str:
    raw = target if isinstance(target, str) else json.dumps(target, sort_keys=True, ensure_ascii=False)
    return hashlib.sha1(raw.encode("utf-8")).hexdigest()[:16]


def normalize(target: dict | str) -> dict:
    if isinstance(target, str):
        return {"css": target}
    return dict(target)


def _build(page: Page, spec: dict) -> list[tuple[str, Locator]]:
    out: list[tuple[str, Locator]] = []
    if spec.get("testid"):
        value = spec["testid"]
        out.append((f"testid:{value}", page.get_by_test_id(value)))
        for attr in ("data-test", "data-qa"):
            out.append((f"css:[{attr}]", page.locator(f'[{attr}="{value}"]')))
    if spec.get("css"):
        out.append((f"css:{spec['css']}", page.locator(spec["css"])))
    if spec.get("role") and spec.get("name"):
        out.append((f"role:{spec['role']}/{spec['name']}",
                    page.get_by_role(spec["role"], name=spec["name"], exact=bool(spec.get("exact")))))
    if spec.get("label"):
        out.append((f"label:{spec['label']}", page.get_by_label(spec["label"])))
    if spec.get("placeholder"):
        out.append((f"placeholder:{spec['placeholder']}", page.get_by_placeholder(spec["placeholder"])))
    if spec.get("name_attr"):
        out.append((f"name:{spec['name_attr']}", page.locator(f'[name="{spec["name_attr"]}"]')))
    if spec.get("aria"):
        out.append((f"aria:{spec['aria']}", page.locator(f'[aria-label="{spec["aria"]}"]')))
    if spec.get("text"):
        out.append((f"text:{spec['text']}", page.get_by_text(spec["text"], exact=bool(spec.get("exact")))))
    if spec.get("xpath"):
        out.append((f"xpath:{spec['xpath']}", page.locator("xpath=" + spec["xpath"])))
    return out


def _first_visible(locator: Locator) -> Locator | None:
    try:
        count = min(locator.count(), 12)
    except PlaywrightError:
        return None
    for index in range(count):
        candidate = locator.nth(index)
        try:
            if candidate.is_visible() and candidate.is_enabled():
                return candidate
        except PlaywrightError:
            continue
    return None


def resolve(page: Page, target: dict | str, store: Store,
            highlight: bool = True) -> Locator:
    spec = normalize(target)
    origin, key = origin_of(page), target_key(target)
    remembered = store.prune_selectors(origin, key)
    tried: list[str] = []

    for attempt in range(ROUNDS):
        candidates: list[tuple[str, Locator]] = []
        for css in remembered:
            candidates.append((f"memory:{css}", page.locator(css)))
        candidates.extend(_build(page, spec))
        for label, locator in candidates:
            tried.append(label)
            found = _first_visible(locator)
            if found is None:
                if label.startswith("memory:"):
                    store.note_candidate(origin, key, label.split(":", 1)[1], hit=False)
                continue
            if highlight:
                try:
                    found.evaluate(_HIGHLIGHT_JS)
                except PlaywrightError:
                    pass
            if label.startswith("memory:"):
                store.note_candidate(origin, key, label.split(":", 1)[1], hit=True)
            _remember(page, store, origin, key, label, found, remembered)
            return found
        if attempt < ROUNDS - 1:
            time.sleep(ROUND_SLEEP)

    raise TargetNotFound(
        "عنصر پیدا نشد. تلاش‌ها: " + " | ".join(dict.fromkeys(tried))[:600]
    )


def _remember(page: Page, store: Store, origin: str, key: str,
              label: str, found: Locator, remembered: list[str]) -> None:
    store.bump_selector(origin, key, hit=True)
    winner = label.split(":", 1)[1] if label.startswith(("memory:", "css:")) else None
    try:
        derived = found.evaluate(_DERIVE_JS)
    except PlaywrightError:
        derived = None
    ordered: list[str] = []
    for item in (derived, winner, *remembered):
        if item and item not in ordered:
            ordered.append(item)
    if ordered:
        store.save_selectors(origin, key, ordered[:6])


def _act(page: Page, target: dict | str, store: Store, action: str, **kwargs) -> Any:
    last_error: Exception | None = None
    for attempt in range(2):
        found = resolve(page, target, store, highlight=kwargs.pop("highlight", True) if attempt == 0 else False)
        try:
            if action == "click":
                return found.click()
            if action == "fill":
                return found.fill(kwargs["value"])
            if action == "press":
                return found.press(kwargs["key"])
            if action == "select":
                return found.select_option(kwargs["value"])
            if action == "check":
                return found.check()
            if action == "text":
                return found.inner_text()
            if action == "hover":
                return found.hover()
            raise ValueError(f"unknown action: {action}")
        except PlaywrightError as exc:
            last_error = exc
            log.info("action %s failed (attempt %s), re-resolving", action, attempt + 1)
            time.sleep(0.5)
    raise TargetNotFound(f"اجرای {action} ناموفق ماند: {last_error}")


def click(page: Page, target: dict | str, store: Store, **kwargs):
    return _act(page, target, store, "click", **kwargs)


def fill(page: Page, target: dict | str, value: str, store: Store, **kwargs):
    return _act(page, target, store, "fill", value=value, **kwargs)


def press(page: Page, target: dict | str, key: str, store: Store, **kwargs):
    return _act(page, target, store, "press", key=key, **kwargs)


def select(page: Page, target: dict | str, value: str, store: Store, **kwargs):
    return _act(page, target, store, "select", value=value, **kwargs)


def text_of(page: Page, target: dict | str, store: Store, **kwargs) -> str:
    return str(_act(page, target, store, "text", **kwargs))