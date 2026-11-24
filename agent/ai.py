"""Browser-only chat worker: Qwen, then DeepSeek. No API keys are used.

Selectors are best-effort and can be tuned without touching code via
D:\\Work\\agent-state\\chat_selectors.json (see `probe`).
"""

from __future__ import annotations

import argparse
import json
import time
from pathlib import Path

from playwright.sync_api import Error as PlaywrightError

from . import codeblocks, fileops, guard, locator
from .browser import Browser
from .config import STATE_DIR, cfg, get_logger
from .errors import AgentError
from .notify import STOP_PATH, Notifier
from .store import Store

log = get_logger("ai")
SELECTOR_PATH = STATE_DIR / "chat_selectors.json"
PROTOCOL_PATH = STATE_DIR / "protocol.txt"
POLL_MS = 700
STABLE_ROUNDS = 3
RESPONSE_TIMEOUT = 300

DEFAULT_SELECTORS = {
    "input": [{"role": "textbox"}, {"css": "textarea"}, {"css": "div[contenteditable='true']"}],
    "send": [{"css": "button[type='submit']"}, {"css": "button[data-testid*='send']"},
             {"text": "Send"}, {"text": "ارسال"}],
    "stop": [{"css": "button[data-testid*='stop']"}, {"css": "[id*='stop']"},
             {"text": "Stop"}, {"text": "توقف"}],
    "assistant": [{"css": "[data-role='assistant']"}, {"css": ".assistant"},
                  {"css": "div[class*='markdown']"}],
}

PROTOCOL_SUFFIX = (
    "\n\nقرارداد خروجی:\n"
    "- قبل از هر بلوک کد یک خط بگذار: ### FILE: <مسیر نسبی فایل>\n"
    "- کد را داخل بلوک کد بگذار.\n"
    "- اگر پاسخ کامل نیست، در انتها بنویس: برای ادامه بگو «ادامه».\n"
    "- وقتی همهٔ کد تمام شد، در انتها دقیقاً بنویس: اتمام کد\n"
)

_BLOCKS_JS = """
() => {
  const out = [];
  document.querySelectorAll('pre').forEach(pre => {
    const code = pre.querySelector('code') || pre;
    let prev = '', node = pre.previousElementSibling, hops = 0;
    while (node && hops < 3) {
      prev = (node.innerText || '') + '\\n' + prev;
      node = node.previousElementSibling; hops++;
    }
    out.push({ code: code.innerText, preceding: prev.slice(-300) });
  });
  return out;
}
"""


def load_selectors() -> dict:
    merged = {key: list(value) for key, value in DEFAULT_SELECTORS.items()}
    if SELECTOR_PATH.exists():
        try:
            user = json.loads(SELECTOR_PATH.read_text(encoding="utf-8"))
        except ValueError as exc:
            raise AgentError(f"chat_selectors.json نامعتبر: {exc}") from exc
        for key, value in user.items():
            if key in merged and isinstance(value, list):
                merged[key] = value
    return merged


class ChatSession:
    def __init__(self, browser, store: Store, notifier: Notifier, selectors: dict | None = None):
        self.browser = browser
        self.store = store
        self.notifier = notifier
        self.selectors = selectors or load_selectors()
        self.page = None
        self._seen = 0

    def open(self, url: str):
        self.page = self.browser.new_page(url)
        guard.ensure_clear(self.page, self.notifier, wait=True, screenshot=self.browser.screenshot)
        return self.page

    def _find(self, kind: str):
        last = None
        for target in self.selectors.get(kind, []):
            try:
                return locator.resolve(self.page, target, self.store, highlight=False)
            except AgentError as exc:
                last = exc
        raise AgentError(f"عنصر «{kind}» پیدا نشد؛ با دستور probe سلکتورها را تنظیم کن. {last}")

    def _visible(self, kind: str) -> bool:
        for target in self.selectors.get(kind, []):
            try:
                found = self.page.locator(target["css"]) if "css" in target else None
                if found is not None and found.count() and found.first.is_visible():
                    return True
            except PlaywrightError:
                continue
        return False

    def _body(self) -> str:
        try:
            return str(self.page.evaluate("() => document.body ? document.body.innerText : ''"))
        except PlaywrightError:
            return ""

    def send(self, text: str) -> None:
        box = self._find("input")
        box.click()
        # fill() notifies React-controlled inputs; setting .value via JS does not.
        try:
            box.fill(text)
        except PlaywrightError:
            box.press("Control+A")
            box.press("Delete")
            box.press_sequentially(text, delay=1)
        self.page.wait_for_timeout(200)
        try:
            self._find("send").click()
        except AgentError:
            box.press("Enter")

    def _response(self, sent: str) -> str:
        for target in self.selectors.get("assistant", []):
            if "css" not in target:
                continue
            try:
                nodes = self.page.locator(target["css"])
                if nodes.count():
                    return str(nodes.nth(nodes.count() - 1).inner_text())
            except PlaywrightError:
                continue
        body = self._body()
        marker = sent.strip()[:60]
        position = body.rfind(marker) if marker else -1
        return body[position + len(marker):].strip() if position >= 0 else body

    def wait_response(self, sent: str, timeout: float = RESPONSE_TIMEOUT) -> str:
        deadline = time.time() + timeout
        last, stable = "", 0
        while time.time() < deadline:
            self.notifier.check_stop()
            current = self._response(sent)
            if current and current == last and not self._visible("stop"):
                stable += 1
                if stable >= STABLE_ROUNDS:
                    return current
            else:
                stable = 0
            last = current
            self.page.wait_for_timeout(POLL_MS)
        raise AgentError("پاسخ در زمان مقرر کامل نشد")

    def new_blocks(self) -> list[dict]:
        try:
            raw = self.page.evaluate(_BLOCKS_JS) or []
        except PlaywrightError:
            raw = []
        fresh = raw[self._seen:]
        self._seen = len(raw)
        return codeblocks.from_dom(fresh)

    def run_task(self, task: str, out_dir: str | None = None,
                 max_parts: int | None = None, write: bool = True) -> dict:
        if not self.notifier.approval("ai_session", "اجازهٔ گفت‌وجو با سایت هوش مصنوعی داده می‌شود؟",
                                      {"task": task[:200]}):
            raise AgentError("اجازهٔ نشست AI داده نشد")
        guard.ensure_clear(self.page, self.notifier, wait=True, screenshot=self.browser.screenshot)
        limit = int(max_parts or cfg("ai_max_parts"))
        done_marker = cfg("ai_done_marker")
        message = task
        parts, blocks = [], []
        for index in range(limit):
            self.send(message)
            text = self.wait_response(message)
            fresh = self.new_blocks()
            blocks.extend(fresh)
            parts.append({"index": index + 1, "chars": len(text), "blocks": len(fresh)})
            self.notifier.emit("ai_part", {"index": index + 1, "blocks": len(fresh)})
            if done_marker in text:
                break
            message = cfg("ai_continue_text")
        else:
            self.notifier.emit("ai_part_limit", {"limit": limit})
        files = codeblocks.to_files(blocks, out_dir) if blocks else []
        written = []
        if write:
            for item in files:
                written.append(str(fileops.write_text(str(item["path"]), item["content"],
                                                      self.notifier, "ask")))
        return {"parts": parts, "blocks": blocks, "written": written,
                "done": bool(parts) and done_marker in text}


def _protocol(path: str | None) -> str:
    source = Path(path) if path else PROTOCOL_PATH
    body = source.read_text(encoding="utf-8") if source.exists() else ""
    if not body:
        log.warning("protocol file not found: %s (only the output contract is sent)", source)
    return body + PROTOCOL_SUFFIX


def _chat_url(given: str | None) -> str:
    if given:
        return given
    default = cfg("chat_url")
    answer = input(f"لینک چت [Enter = {default}]: ").strip()
    return answer or default


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent.ai", description="کارگر چت مرورگری")
    sub = parser.add_subparsers(dest="command", required=True)
    ask = sub.add_parser("ask", help="پرسیدن یک کار از سایت AI")
    ask.add_argument("--task", required=True)
    ask.add_argument("--url", default=None)
    ask.add_argument("--out", default=None)
    ask.add_argument("--protocol", default=None)
    ask.add_argument("--max-parts", type=int, default=None)
    probe = sub.add_parser("probe", help="نمایش سلکتورهای موجود در صفحه")
    probe.add_argument("--url", default=None)
    args = parser.parse_args(argv)

    if STOP_PATH.exists():
        print("توقف اضطراری فعال است (فایل STOP).")
        print("اول Ctrl+Alt+F11 را بزن یا فایل D:\\Work\\agent-state\\STOP را پاک کن.")
        return 1

    store = Store()
    notifier = Notifier(interactive=True)
    try:
        with Browser(store, notifier) as browser:
            session = ChatSession(browser, store, notifier)
            session.open(_chat_url(args.url))
            if args.command == "probe":
                for kind, targets in session.selectors.items():
                    hits = []
                    for target in targets:
                        try:
                            locator.resolve(session.page, target, store, highlight=False)
                            hits.append(str(target))
                        except AgentError:
                            continue
                    print(f"{kind}: {hits or 'هیچ‌کدام پیدا نشد'}")
                return 0
            task = _protocol(args.protocol) + "\n\nکار:\n" + args.task
            report = session.run_task(task, out_dir=args.out, max_parts=args.max_parts)
            print(f"بخش‌ها: {report['parts']}")
            print(f"پایان با «{cfg('ai_done_marker')}»: {report['done']}")
            for path in report["written"]:
                print("نوشته شد:", path)
        return 0
    except (AgentError, KeyboardInterrupt) as exc:
        print("توقف:", exc)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())