"""Mission runner: queue, repeat, retry, guard, downloads and reports."""

from __future__ import annotations

import json
import time
from urllib.parse import urlsplit

from playwright.sync_api import Error as PlaywrightError

from . import fileops, guard, locator
from .browser import Browser
from .config import REPORT_DIR, assert_writable, get_logger
from .dsl import Mission, Step, interpolate, parse_duration
from .errors import AgentError, UserAbort
from .notify import Notifier
from .store import Store

log = get_logger("runner")


class Runner:
    def __init__(self, store: Store, notifier: Notifier, browser: Browser | None = None):
        self.store = store
        self.notifier = notifier
        self._browser = browser
        self._owns_browser = browser is None

    def run_queue(self, missions: list[Mission]) -> list[dict]:
        reports = []
        for mission in missions:
            self.notifier.check_stop()
            reports.append(self.run_mission(mission))
        return reports

    def run_mission(self, mission: Mission) -> dict:
        mission_id = self.store.create_mission(mission.name, mission.source, {
            "repeat": mission.repeat, "downloads": mission.downloads,
            "allow_hosts": mission.allow_hosts, "steps": len(mission.steps),
        })
        run_id = self.store.create_run(mission_id)
        variables = dict(mission.variables)
        step_reports: list[dict] = []
        status, error = "ok", ""
        self.notifier.emit("mission_start", {"name": mission.name, "run": run_id})
        try:
            if self._browser is None:
                self._browser = Browser(self.store, self.notifier)
                self._browser.start()
            browser = self._browser
            page = browser.new_page()
            for cycle in range(mission.repeat):
                for index, step in enumerate(mission.steps, start=1):
                    self.notifier.check_stop()
                    outcome = self._run_step(browser, page, mission, step, variables,
                                             run_id, index, cycle)
                    step_reports.append(outcome)
                    if outcome["status"] == "failed" and not step.continue_on_error:
                        raise AgentError(outcome["detail"])
        except UserAbort as exc:
            status, error = "stopped", str(exc)
        except (AgentError, PlaywrightError, OSError) as exc:
            status, error = "failed", str(exc)
        finally:
            if self._owns_browser and self._browser is not None:
                self._browser.close()
                self._browser = None
        report = {
            "mission": mission.name, "run": run_id, "status": status, "error": error,
            "steps": step_reports, "variables": variables, "finished_ts": time.time(),
        }
        path = assert_writable(REPORT_DIR / f"run_{run_id}.json")
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        report["report_path"] = str(path)
        self.store.finish_run(run_id, status, report)
        self.notifier.emit("mission_done", {"name": mission.name, "status": status,
                                            "report": str(path)})
        return report

    def _run_step(self, browser, page, mission, step: Step, variables: dict,
                  run_id: int, index: int, cycle: int) -> dict:
        args = interpolate(step.args, variables)
        label = json.dumps(args.get("target", args.get("url", args.get("path", ""))),
                           ensure_ascii=False)[:200]
        last = ""
        for attempt in range(1, max(1, step.attempts) + 1):
            self.notifier.check_stop()
            try:
                detail = self._dispatch(browser, page, mission, step.action, args, variables)
                self.store.add_step(run_id, index, step.action, label, "ok", str(detail)[:500])
                self.notifier.emit("step_ok", {"mission": mission.name, "index": index,
                                               "action": step.action, "attempt": attempt})
                return {"index": index, "cycle": cycle, "action": step.action,
                        "status": "ok", "detail": str(detail)[:500], "attempts": attempt}
            except UserAbort:
                raise
            except (AgentError, PlaywrightError, OSError) as exc:
                last = str(exc)
                log.warning("step %s failed (attempt %s): %s", step.action, attempt, last)
                if attempt < step.attempts:
                    time.sleep(step.delay)
                    try:
                        guard.ensure_clear(page, self.notifier, wait=True,
                                           screenshot=browser.screenshot)
                    except AgentError as guard_exc:
                        last = str(guard_exc)
        self.store.add_step(run_id, index, step.action, label, "failed", last[:500])
        self.notifier.emit("step_failed", {"mission": mission.name, "index": index,
                                           "action": step.action, "error": last[:300]})
        return {"index": index, "cycle": cycle, "action": step.action,
                "status": "failed", "detail": last[:500], "attempts": step.attempts}

    def _check_host(self, url: str, mission: Mission) -> None:
        host = (urlsplit(url).hostname or "").lower()
        if not mission.allow_hosts or not host:
            return
        if any(host == allowed or host.endswith("." + allowed) for allowed in mission.allow_hosts):
            return
        if not self.notifier.approval("host", f"دامنهٔ خارج از فهرست مأموریت: {host}", {"url": url}):
            raise AgentError(f"دامنه مجاز نیست: {host}")

    def _download(self, browser, page, mission: Mission, args: dict) -> str:
        timeout_ms = parse_duration(args.get("timeout", "60s")) * 1000
        try:
            page.bring_to_front()
        except PlaywrightError:
            pass
        # expect_download pumps the event loop; a sleep-based poll would miss it.
        with page.expect_download(timeout=timeout_ms) as info:
            locator.click(page, args["target"], self.store)
        found = browser.save_download(info.value)
        final = fileops.promote_download(found, args.get("dest"), self.notifier,
                                         mission.downloads, page.url)
        return str(final or found)

    def _dispatch(self, browser, page, mission: Mission, action: str,
                  args: dict, variables: dict) -> str:
        if action == "goto":
            url = str(args["url"])
            self._check_host(url, mission)
            page.goto(url, wait_until="domcontentloaded")
            guard.ensure_clear(page, self.notifier, wait=True, screenshot=browser.screenshot)
            return page.url
        if action in ("click", "hover", "check"):
            getattr(locator, action)(page, args["target"], self.store)
            return "done"
        if action == "fill":
            locator.fill(page, args["target"], str(args["value"]), self.store)
            return "filled"
        if action == "press":
            locator.press(page, args["target"], str(args["key"]), self.store)
            return str(args["key"])
        if action == "select":
            locator.select(page, args["target"], str(args["value"]), self.store)
            return str(args["value"])
        if action == "wait":
            seconds = parse_duration(args["duration"])
            time.sleep(seconds)
            return f"{seconds}s"
        if action == "wait_for":
            timeout_ms = parse_duration(args.get("timeout", "15s")) * 1000
            if "target" in args:
                locator.resolve(page, args["target"], self.store)
            else:
                page.get_by_text(str(args["text"])).first.wait_for(state="visible", timeout=timeout_ms)
            return "visible"
        if action == "assert_text":
            actual = locator.text_of(page, args["target"], self.store)
            if "equals" in args and actual.strip() != str(args["equals"]).strip():
                raise AgentError(f"متن مطابق نیست: {actual[:120]!r}")
            if "contains" in args and str(args["contains"]) not in actual:
                raise AgentError(f"متن شامل مقدار خواسته‌شده نیست: {actual[:120]!r}")
            return actual[:200]
        if action == "extract":
            value = locator.text_of(page, args["target"], self.store)
            variables[str(args["save_as"])] = value
            return value[:200]
        if action == "screenshot":
            return str(browser.screenshot(str(args.get("name", "step"))))
        if action == "pdf":
            if "html" in args:
                return str(fileops.html_to_pdf(browser, str(args["html"]), str(args["path"])))
            return str(fileops.print_page_to_pdf(page, str(args["path"])))
        if action == "write_file":
            return str(fileops.write_text(str(args["path"]), str(args["content"]),
                                          self.notifier, mission.overwrite))
        if action == "make_dir":
            return str(fileops.make_dir(str(args["path"])))
        if action == "download":
            return self._download(browser, page, mission, args)
        if action == "echo":
            text = str(args["text"])
            self.notifier.emit("hud", {"text": text})
            return text
        raise AgentError(f"اکشن پیاده‌سازی نشده: {action}")