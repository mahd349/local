"""Persistent engine: consumes HUD commands from the bridge."""

from __future__ import annotations

import time

from .browser import Browser
from .bridge import BridgeClient
from .dsl import load_mission
from .errors import AgentError
from .learn import Recorder, replay as replay_macro, save_macro
from .notify import STOP_PATH, Notifier
from .runner import Runner
from .store import Store
from .takeover import execute as takeover_execute

from .config import get_logger
log = get_logger("daemon")


class Daemon:
    def __init__(self):
        self.store = Store()
        self.client = BridgeClient()
        self.notifier = Notifier(interactive=False)
        self.browser = None
        self.recorder = None
        self.learning = False

    def ensure_browser(self):
        if self.browser is None:
            self.browser = Browser(self.store, self.notifier)
            self.browser.start()
        return self.browser

    def close_browser(self):
        if self.browser is not None:
            self.browser.close()
            self.browser = None
        self.recorder = None
        self.learning = False

    def handle(self, kind, payload):
        if kind == "takeover":
            browser = self.ensure_browser()
            try:
                result = takeover_execute(browser, self.store, self.notifier,
                                          payload.get("line", ""))
                self.notifier.emit("takeover_result", {"ok": True, "result": result})
            except AgentError as exc:
                self.notifier.emit("takeover_result", {"ok": False, "error": str(exc)})
        elif kind == "learn_start":
            browser = self.ensure_browser()
            self.recorder = Recorder(self.store, self.notifier)
            page = browser.new_page(payload.get("url") or None)
            self.recorder.attach(page)
            browser.context.on("page", self.recorder.attach)
            self.learning = True
            self.notifier.emit("learn_started", {})
        elif kind == "learn_stop":
            if self.learning and self.recorder is not None:
                self.recorder.drain(self.browser.pages())
                name = payload.get("name") or "macro"
                macro = save_macro(self.store, name, self.recorder.events)
                self.notifier.emit("learn_saved", {
                    "name": name, "params": [p["name"] for p in macro["params"]]})
            self.learning = False
        elif kind == "run_mission":
            mission = load_mission(payload["path"])
            report = Runner(self.store, self.notifier,
                            browser=self.ensure_browser()).run_mission(mission)
            self.notifier.emit("mission_result", {
                "status": report["status"], "report": report["report_path"]})
        elif kind == "replay":
            report = replay_macro(self.store, self.notifier, self.ensure_browser(),
                                  payload["name"], payload.get("values", {}))
            self.notifier.emit("mission_result", {
                "status": report["status"], "report": report["report_path"]})
        elif kind == "close_browser":
            self.close_browser()
            self.notifier.emit("browser_closed", {})
        elif kind == "stop":
            STOP_PATH.write_text("stop", encoding="utf-8")
            self.notifier.emit("emergency_stop", {})
        elif kind == "clear_stop":
            Notifier.clear_stop()
            self.notifier.emit("stop_cleared", {})
        else:
            self.notifier.emit("unknown_command", {"kind": kind})

    def run(self):
        log.info("daemon ready")
        self.notifier.emit("daemon_ready", {})
        try:
            while True:
                self.store.kv_set("engine_heartbeat", time.time())
                if self.learning and self.recorder is not None and self.browser is not None:
                    self.recorder.drain(self.browser.pages())
                    self.browser.flush_downloads()
                try:
                    command = self.client.poll_command()
                except RuntimeError:
                    command = None
                    time.sleep(1)
                if command:
                    try:
                        self.handle(command["kind"], command["payload"])
                    except (AgentError, OSError) as exc:
                        self.notifier.emit("command_error", {
                            "kind": command["kind"], "error": str(exc)})
                time.sleep(0.4)
        except KeyboardInterrupt:
            log.info("daemon stopped")
        finally:
            self.close_browser()
            self.store.close()


def main():
    Daemon().run()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())