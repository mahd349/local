"""Mission DSL: load, validate and normalize YAML missions."""

from __future__ import annotations

import re
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

from .config import cfg
from .errors import AgentError

_DURATION = re.compile(r"^(\d+(?:\.\d+)?)(ms|s|m|h)$")
_SCALAR_ARG = {
    "goto": "url", "wait": "duration", "screenshot": "name", "pdf": "path",
    "make_dir": "path", "echo": "text", "click": "target", "hover": "target",
    "check": "target", "wait_for": "text",
}
_REQUIRED = {
    "goto": ("url",), "click": ("target",), "hover": ("target",), "check": ("target",),
    "fill": ("target", "value"), "press": ("target", "key"), "select": ("target", "value"),
    "wait": ("duration",), "assert_text": ("target",), "extract": ("target", "save_as"),
    "pdf": ("path",), "write_file": ("path", "content"), "make_dir": ("path",),
    "download": ("target",), "echo": ("text",),
}
ACTIONS = set(_REQUIRED) | {"wait_for", "screenshot"}


class MissionError(AgentError):
    """The mission file is not valid."""


def parse_duration(value: Any) -> float:
    if isinstance(value, (int, float)):
        return float(value)
    match = _DURATION.match(str(value).strip())
    if not match:
        raise MissionError(f"زمان نامعتبر: {value} (نمونه: 500ms، 2s، 5m، 1h)")
    number, unit = float(match.group(1)), match.group(2)
    return number / 1000 if unit == "ms" else number if unit == "s" else number * 60 if unit == "m" else number * 3600


def parse_schedule(spec: Any) -> dict:
    if not spec:
        return {}
    if not isinstance(spec, dict) or not ({"every", "at"} & set(spec)):
        raise MissionError("schedule باید every یا at داشته باشد")
    if "every" in spec:
        return {"every": parse_duration(spec["every"])}
    clock = str(spec["at"]).strip()
    if not re.match(r"^\d{1,2}:\d{2}$", clock):
        raise MissionError(f"at باید HH:MM باشد: {clock}")
    hour, minute = (int(part) for part in clock.split(":"))
    if not (0 <= hour < 24 and 0 <= minute < 60):
        raise MissionError(f"ساعت نامعتبر: {clock}")
    return {"at": (hour, minute)}


def next_due(schedule: dict, last_ts: float | None, now: float) -> float | None:
    if not schedule:
        return None
    if "every" in schedule:
        return now if last_ts is None else last_ts + schedule["every"]
    hour, minute = schedule["at"]
    today = time.localtime(now)
    due = time.mktime((today.tm_year, today.tm_mon, today.tm_mday, hour, minute, 0, 0, 0, -1))
    return due if due > now else due + 86400


@dataclass
class Step:
    action: str
    args: dict
    attempts: int
    delay: float
    continue_on_error: bool


@dataclass
class Mission:
    name: str
    steps: list[Step]
    repeat: int = 1
    allow_hosts: list[str] = field(default_factory=list)
    downloads: str = "approve"
    overwrite: str = "ask"
    variables: dict = field(default_factory=dict)
    schedule: dict = field(default_factory=dict)
    source: str = ""


def _normalize_step(raw: Any, attempts: int, delay: Any) -> Step:
    # Accept both `click: {text: ...}` (selector form) and `click: {target: {...}}`.
    selector_keys = {"text", "css", "role", "name", "label", "placeholder",
                     "testid", "aria", "xpath", "name_attr", "exact"}
    option_keys = {"attempts", "delay", "continue_on_error", "timeout"}
    target_actions = {"click", "hover", "check"}
    if not isinstance(raw, dict) or len(raw) != 1:
        raise MissionError(f"هر مرحله باید دقیقاً یک کلید داشته باشد: {raw}")
    action, payload = next(iter(raw.items()))
    if action not in ACTIONS:
        raise MissionError(f"اکشن ناشناس: {action}. مجاز: {', '.join(sorted(ACTIONS))}")
    if isinstance(payload, dict):
        args = dict(payload)
        if action in target_actions and "target" not in args:
            selector = {k: v for k, v in args.items() if k in selector_keys}
            if selector:
                args = {"target": selector,
                        **{k: v for k, v in args.items() if k in option_keys}}
    else:
        key = _SCALAR_ARG.get(action)
        if key is None:
            raise MissionError(f"اکشن {action} به یک شیء نیاز دارد، نه مقدار ساده")
        args = {key: payload}
    for key in _REQUIRED.get(action, ()):
        if key not in args:
            raise MissionError(f"اکشن {action} به فیلد {key} نیاز دارد")
    if action == "assert_text" and not ({"equals", "contains"} & set(args)):
        raise MissionError("assert_text به equals یا contains نیاز دارد")
    if action == "wait_for" and not ({"text", "target"} & set(args)):
        raise MissionError("wait_for به text یا target نیاز دارد")
    return Step(
        action=action,
        args=args,
        attempts=int(args.get("attempts", attempts)),
        delay=parse_duration(args.get("delay", delay)),
        continue_on_error=bool(args.get("continue_on_error", False)),
    )


def build_mission(spec: Any, source: str = "") -> Mission:
    if not isinstance(spec, dict):
        raise MissionError("فایل مأموریت باید یک شیء YAML باشد")
    unknown = set(spec) - {"name", "steps", "repeat", "retry", "allow_hosts",
                           "downloads", "overwrite", "vars", "schedule"}
    if unknown:
        raise MissionError("کلیدهای ناشناس: " + ", ".join(sorted(unknown)))
    steps_raw = spec.get("steps")
    if not isinstance(steps_raw, list) or not steps_raw:
        raise MissionError("steps باید فهرستی ناتهی باشد")
    retry = spec.get("retry") if isinstance(spec.get("retry"), dict) else {}
    attempts = int(retry.get("attempts", cfg("mission_retry_attempts")))
    delay = retry.get("delay", cfg("mission_retry_delay_sec"))
    downloads = str(spec.get("downloads", cfg("download_mode")))
    overwrite = str(spec.get("overwrite", cfg("overwrite_mode")))
    if downloads not in ("approve", "quarantine", "deny"):
        raise MissionError("downloads باید approve/quarantine/deny باشد")
    if overwrite not in ("ask", "allow"):
        raise MissionError("overwrite باید ask/allow باشد")
    return Mission(
        name=str(spec.get("name") or Path(source).stem or "mission"),
        steps=[_normalize_step(item, attempts, delay) for item in steps_raw],
        repeat=max(1, int(spec.get("repeat", 1))),
        allow_hosts=[str(h).lower() for h in spec.get("allow_hosts", [])],
        downloads=downloads,
        overwrite=overwrite,
        variables=dict(spec.get("vars", {})),
        schedule=parse_schedule(spec.get("schedule")),
        source=source,
    )


def load_mission(path: str | Path) -> Mission:
    source = Path(path)
    if not source.exists():
        raise MissionError(f"فایل مأموریت پیدا نشد: {source}")
    try:
        spec = yaml.safe_load(source.read_text(encoding="utf-8"))
    except yaml.YAMLError as exc:
        raise MissionError(f"YAML نامعتبر: {exc}") from exc
    return build_mission(spec, str(source))


def interpolate(value: Any, variables: dict) -> Any:
    if isinstance(value, str):
        for key, item in variables.items():
            value = value.replace("{{" + key + "}}", str(item))
        return value
    if isinstance(value, dict):
        return {k: interpolate(v, variables) for k, v in value.items()}
    if isinstance(value, list):
        return [interpolate(v, variables) for v in value]
    return value