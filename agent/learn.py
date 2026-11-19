"""Learn Mode: record actions, parameterize typed values, save and replay macros."""

from __future__ import annotations

import re
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import Page

from .config import get_logger
from .dsl import build_mission
from .errors import AgentError
from .notify import Notifier
from .runner import Runner
from .store import Store

log = get_logger("learn")
_JS_PATH = Path(__file__).with_name("recorder.js")
_DRAIN = ("() => { const R = window.__agentRecorder; if (!R) return []; "
          "const e = R.events; R.events = []; return e; }")


def _script() -> str:
    if not _JS_PATH.exists():
        raise AgentError(f"فایل ضبط‌کننده پیدا نشد: {_JS_PATH}")
    return _JS_PATH.read_text(encoding="utf-8")


class Recorder:
    def __init__(self, store: Store, notifier: Notifier):
        self.store = store
        self.notifier = notifier
        self.events: list[dict] = []
        self._script = _script()
        self._last_url: dict[int, str] = {}

    def attach(self, page: Page) -> None:
        page.add_init_script(script=self._script)
        try:
            page.evaluate(self._script)
        except PlaywrightError:
            pass
        page.on("framenavigated", lambda frame: self._on_frame(page, frame))

    def _on_frame(self, page: Page, frame) -> None:
        if frame != page.main_frame:
            return
        url = frame.url
        if not url or url == "about:blank":
            return
        key = id(page)
        if self._last_url.get(key) == url:
            return
        self._last_url[key] = url
        self.events.append({"t": "goto", "url": url})

    def drain(self, pages) -> int:
        added = 0
        for page in pages:
            try:
                raw = page.evaluate(_DRAIN)
            except PlaywrightError:
                continue
            for item in raw or []:
                self.events.append(item)
                added += 1
        return added


def parameterize(events: list[dict]) -> tuple[list[dict], list[dict]]:
    steps: list[dict] = []
    params: list[dict] = []
    seen: dict[str, bool] = {}
    for event in events:
        kind = event.get("t")
        if kind == "goto":
            steps.append({"goto": event["url"]})
        elif kind == "click":
            steps.append({"click": {"target": event["target"]}})
        elif kind == "press":
            steps.append({"press": {"target": event["target"], "key": event["key"]}})
        elif kind == "fill":
            base = re.sub(r"\W+", "_", (event.get("label") or "").strip(),
                          flags=re.UNICODE).strip("_")
            base = base or f"value_{len(params) + 1}"
            name = base if base not in seen else f"{base}_{len(params) + 1}"
            seen[name] = True
            secret = bool(event.get("secret"))
            params.append({"name": name, "secret": secret,
                           "sample": None if secret else event.get("value")})
            steps.append({"fill": {"target": event["target"], "value": "{{" + name + "}}"}})
    return steps, params


def save_macro(store: Store, name: str, events: list[dict]) -> dict:
    steps, params = parameterize(events)
    if not steps:
        raise AgentError("هیچ حرکتی ضبط نشد")
    store.save_macro(name, params, steps)
    return {"name": name, "params": params, "steps": steps}


def replay(store: Store, notifier: Notifier, browser, name: str, values: dict) -> dict:
    macro = store.get_macro(name)
    if macro is None:
        raise AgentError(f"ماکرو پیدا نشد: {name}")
    missing = [p["name"] for p in macro["params"] if p["name"] not in values]
    if missing:
        raise AgentError("مقدار این پارامترها لازم است: " + ", ".join(missing))
    mission = build_mission(
        {"name": f"macro:{name}", "steps": macro["steps"], "vars": values},
        source=f"macro:{name}",
    )
    return Runner(store, notifier, browser=browser).run_mission(mission)