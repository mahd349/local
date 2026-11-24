"""Extract code blocks from a chat answer and map them to sandboxed files."""

from __future__ import annotations

import re
from pathlib import Path

from .config import CODE_ROOT, assert_writable
from .errors import AgentError

_FILE_HINT = re.compile(r"(?:FILE|فایل)\s*[:：]\s*([^\s`*#]+)", re.IGNORECASE)
_INLINE_HINT = re.compile(r"^\s*(?:#|//|;|<!--)\s*(?:file|فایل)\s*[:：]\s*([^\s*]+)", re.IGNORECASE)
_FENCE = re.compile(r"```[\w+-]*\n(.*?)```", re.DOTALL)


def _clean(raw: str) -> str:
    return raw.strip().strip("`*\"'<>").replace("\\", "/")


def guess_name(preceding: str, code: str, index: int) -> str:
    hits = _FILE_HINT.findall(preceding or "")
    if hits:
        return _clean(hits[-1])
    for line in (code or "").splitlines()[:3]:
        match = _INLINE_HINT.match(line)
        if match:
            return _clean(match.group(1))
    return f"part_{index + 1}.txt"


def from_markdown(text: str) -> list[dict]:
    blocks = []
    for index, match in enumerate(_FENCE.finditer(text or "")):
        preceding = (text or "")[:match.start()][-300:]
        code = match.group(1)
        blocks.append({"name": guess_name(preceding, code, index), "code": code})
    return blocks


def from_dom(blocks: list[dict]) -> list[dict]:
    out = []
    for index, block in enumerate(blocks or []):
        code = block.get("code", "")
        out.append({"name": guess_name(block.get("preceding", ""), code, index), "code": code})
    return out


def to_files(blocks: list[dict], default_dir: str | Path | None = None) -> list[dict]:
    base = Path(default_dir) if default_dir else CODE_ROOT
    files = []
    for block in blocks:
        name = _clean(block["name"])
        candidate = Path(name)
        target = candidate if candidate.is_absolute() else base / candidate
        try:
            resolved = assert_writable(target)
        except PermissionError as exc:
            raise AgentError(f"مسیر فایل بیرون سندباکس است: {name}") from exc
        files.append({"path": resolved, "content": block["code"]})
    return files