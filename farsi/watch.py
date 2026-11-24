"""Active suggestions rendered inside the chat page itself, never on the desktop.

Chat text is read from the page and classified locally. Only a confident action
becomes a suggestion. The page never supplies a command: it returns an opaque id
and the watcher keeps the argv it generated itself, so page scripts cannot inject
commands. Execution is added in the next part; here the user copies the command.
"""
from __future__ import annotations

import argparse
import json
import time
import uuid
from pathlib import Path
from urllib.parse import urlsplit

from agent.config import STATE_DIR, cfg, ensure_dirs, get_logger
from agent.errors import AgentError

from . import corpus, intent, nlg, usage
from .text import normalize

log = get_logger("farsi.watch")

SUGGEST_OFF = STATE_DIR / "SUGGEST_OFF"
PANEL_JS = Path(__file__).with_name("panel.js")
MIN_SCORE = 0.6
MIN_INTERVAL = 20.0
MAX_PER_SESSION = 20
DISMISS_COOLDOWN = 180.0
POLL_MS = 1200


def suggestions_enabled() -> bool:
    return not SUGGEST_OFF.exists()


def set_suggestions(enabled: bool) -> None:
    ensure_dirs()
    if enabled:
        SUGGEST_OFF.unlink(missing_ok=True)
    else:
        SUGGEST_OFF.write_text("off", encoding="utf-8")


def panel_script() -> str:
    if not PANEL_JS.exists():
        raise AgentError(f"فایل پنل پیدا نشد: {PANEL_JS}")
    return PANEL_JS.read_text(encoding="utf-8")


def _read_js() -> str:
    """Selectors come from the tunable chat config, with a plain-text fallback."""
    from agent.ai import load_selectors

    css = [t["css"] for t in load_selectors().get("assistant", []) if "css" in t]
    return """
() => {
  const sels = %s;
  for (const s of sels) {
    let nodes;
    try { nodes = document.querySelectorAll(s); } catch (e) { continue; }
    if (nodes && nodes.length) {
      const t = (nodes[nodes.length - 1].innerText || '').trim();
      if (t) return t.slice(-4000);
    }
  }
  const body = document.body ? document.body.innerText : '';
  return body.slice(-2000);
}
""" % json.dumps(css or [".assistant"])


_DRAIN_JS = """
() => {
  const P = window.__farsiPanel;
  if (!P) return [];
  const out = P.outbox.slice();
  P.outbox.length = 0;
  return out;
}
"""


class Watcher:
    def __init__(self, browser, store, notifier, *, paths: dict | None = None,
                 runner=None):
        paths = paths or {}
        self.browser = browser
        self.store = store
        self.notifier = notifier
        self.runner = runner
        self.idb = intent.prepare(paths.get("intent"))
        self.udb = usage.connect(paths.get("usage"))
        self.ndb = nlg.prepare(paths.get("nlg"))
        self.cdb = corpus.connect(paths.get("corpus"))
        self._script = panel_script()
        self._reader = _read_js()
        self._last_text = ""
        self._seen: set[str] = set()
        self._pending: dict[str, dict] = {}
        self._cooldown: dict[str, float] = {}
        self._last_push = 0.0
        self._pushed = 0

    def close(self) -> None:
        for db in (self.idb, self.udb, self.ndb, self.cdb):
            try:
                db.close()
            except Exception:  # a closed handle must not mask the real error
                pass

    def attach(self, page) -> None:
        page.add_init_script(script=self._script)
        self._ensure(page)

    def _ensure(self, page) -> bool:
        present = page.evaluate("() => !!window.__farsiPanel")
        if not present:
            page.evaluate(self._script)
        return bool(present)

    def _status(self, page, note: str = "") -> None:
        page.evaluate(
            "(s) => window.__farsiPanel && window.__farsiPanel.status(s)",
            {"learning": corpus.learning_enabled(),
             "suggesting": suggestions_enabled(), "note": note},
        )

    def _text(self, page) -> str:
        try:
            return str(page.evaluate(self._reader) or "").strip()
        except Exception:
            return ""

    def _events(self, page) -> list[dict]:
        try:
            return list(page.evaluate(_DRAIN_JS) or [])
        except Exception:
            return []

    def _handle(self, page, event: dict) -> None:
        kind = event.get("type")
        if kind == "toggle":
            value = bool(event.get("value"))
            if event.get("name") == "learn":
                corpus.set_learning(value)
            elif event.get("name") == "suggest":
                set_suggestions(value)
            return
        item = self._pending.pop(str(event.get("id") or ""), None)
        if item is None:
            return  # unknown id: the page cannot invent work
        if kind == "dismiss":
            self._cooldown[item["route"]] = time.time() + DISMISS_COOLDOWN
            return
        if kind == "run":
            self._run_item(page, item)

    def _run_item(self, page, item: dict) -> None:
        from .execute import run_argv

        summary = "این دستور اجرا شود؟\n" + " ".join(str(p) for p in item["argv"][1:])
        if not self.notifier.approval("farsi_suggest", summary, {"argv": item["argv"]}):
            self._result(page, {"ok": False, "status": "denied",
                                "detail": "اجازه داده نشد"})
            return
        report = (self.runner or run_argv)(item["argv"])
        self._result(page, report)
        # A denial is not a routing mistake, so only execution feeds the weight.
        try:
            intent.feedback(self.idb, item["route"], item["text"], bool(report["ok"]))
        except (AgentError, KeyError):
            pass

    def _result(self, page, report: dict) -> None:
        try:
            page.evaluate("(r) => window.__farsiPanel && window.__farsiPanel.result(r)",
                          report)
        except Exception:
            pass  # the page may navigate away mid-call; the watcher must survive


    def _observe(self, page, text: str) -> None:
        if corpus.learning_enabled():
            corpus.add_text(self.cdb, text, source="chat", speaker="assistant")
        if not suggestions_enabled() or self._pushed >= MAX_PER_SESSION:
            return
        if time.time() - self._last_push < MIN_INTERVAL:
            return
        decision = intent.classify(text, self.idb, self.udb)
        if decision["kind"] != "action" or not decision["argv"]:
            return
        if decision["score"] < MIN_SCORE:
            return
        route = decision["route"]
        if time.time() < self._cooldown.get(route, 0.0):
            return
        key = route + "|" + normalize(text)[:120]
        if key in self._seen:
            return
        answer = nlg.reply(self.ndb, self.udb, decision)
        token = uuid.uuid4().hex[:12]
        self._pending[token] = {"route": route, "argv": decision["argv"], "text": text}
        self._seen.add(key)
        self._last_push = time.time()
        self._pushed += 1
        page.evaluate(
            "(i) => window.__farsiPanel && window.__farsiPanel.show(i)",
            {"id": token, "title": decision["title"] or route,
             "text": answer["text"], "confidence": answer["confidence"],
             "command": " ".join(str(part) for part in decision["argv"][1:])},
        )

    def step(self, page) -> dict:
        self.notifier.check_stop()
        self._ensure(page)
        events = self._events(page)
        for event in events:
            self._handle(page, event)
        self._status(page, f"{self._pushed}/{MAX_PER_SESSION}")
        text = self._text(page)
        if text and text != self._last_text:
            self._last_text = text
            self._observe(page, text)
        return {"chars": len(text), "events": len(events), "suggested": self._pushed}

    def run(self, page, seconds: float = 900.0) -> None:
        self.attach(page)
        deadline = time.time() + seconds
        while time.time() < deadline:
            self.step(page)
            page.wait_for_timeout(POLL_MS)


def chat_page(browser):
    host = (urlsplit(cfg("chat_url")).hostname or "").lower()
    for page in browser.pages():
        try:
            if host and host in (urlsplit(page.url).hostname or "").lower():
                return page
        except Exception:
            continue
    return browser.active_page()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="farsi.watch", description="پیشنهاد داخل صفحهٔ چت")
    sub = parser.add_subparsers(dest="command", required=True)
    run_parser = sub.add_parser("run", help="نظارت بر صفحهٔ چت")
    run_parser.add_argument("--seconds", type=float, default=900.0)
    sub.add_parser("on", help="روشن کردن پیشنهاد")
    sub.add_parser("off", help="خاموش کردن پیشنهاد")
    args = parser.parse_args(argv)

    if args.command in ("on", "off"):
        set_suggestions(args.command == "on")
        print("پیشنهاد:", "روشن" if args.command == "on" else "خاموش")
        return 0

    from agent.browser import Browser
    from agent.notify import Notifier
    from agent.store import Store

    store = Store()
    notifier = Notifier(interactive=True)
    watcher = None
    try:
        with Browser(store, notifier) as browser:
            page = chat_page(browser)
            watcher = Watcher(browser, store, notifier)
            print("پنل داخل صفحهٔ چت فعال شد:", page.url)
            print("برای توقف Ctrl+C؛ کلیدهای یادگیری/پیشنهاد داخل خود پنل هستند.")
            watcher.run(page, seconds=args.seconds)
        return 0
    except (AgentError, KeyboardInterrupt) as exc:
        print("توقف:", exc)
        return 0 if isinstance(exc, KeyboardInterrupt) else 1
    finally:
        if watcher is not None:
            watcher.close()
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())