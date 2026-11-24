"""Child process for a single probe run. Standard library only.

Launched as: python -I -S -B -X utf8 _probe_runner.py <snippet> <scratch> <result> <config>
The audit hook is armed only while the snippet runs, so the harness itself can
still read its inputs and write its result.
"""
from __future__ import annotations

import io
import json
import linecache
import os
import sys
import traceback

MAX_TEXT = 8000

_deny_imports: set[str] = set()
_deny_events: set[str] = set()
_root = ""
_extra: list[str] = []
_armed = False
_blocked: list[str] = []


def _as_path(value):
    try:
        return os.fspath(value)
    except TypeError:
        return None


def _within(value) -> bool:
    path = _as_path(value)
    if path is None:
        return True  # an already-open descriptor
    if isinstance(path, bytes):
        path = path.decode("utf-8", "replace")
    try:
        target = os.path.normcase(os.path.abspath(path))
    except (ValueError, OSError):
        return False
    for base in [_root] + _extra:
        if target == base or target.startswith(base + os.sep):
            return True
    return False


def _is_write(mode, flags) -> bool:
    if isinstance(mode, str) and any(ch in mode for ch in "wax+"):
        return True
    try:
        mask = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND
        return bool(int(flags or 0) & mask)
    except (TypeError, ValueError):
        return False


def _denied_module(name: str) -> bool:
    parts = name.split(".")
    for size in range(1, len(parts) + 1):
        if ".".join(parts[:size]) in _deny_imports:
            return True
    return False


def _stop(reason: str) -> None:
    _blocked.append(reason[:120])
    raise PermissionError("probe guard: " + reason)


def _hook(event, args):
    if not _armed:
        return
    if event == "import":
        name = args[0] or ""
        if _denied_module(name):
            _stop("import " + name)
        return
    if event in _deny_events:
        _stop(event)
        return
    if event == "open":
        path, mode, flags = (list(args) + [None, None, None])[:3]
        if _is_write(mode, flags) and not _within(path):
            _stop("write " + str(path))
        return
    if event in ("os.remove", "os.rmdir", "os.mkdir", "os.rename", "os.link",
                 "os.symlink", "os.truncate", "shutil.rmtree", "shutil.move",
                 "shutil.copyfile"):
        for value in list(args)[:2]:
            if isinstance(value, (str, bytes, os.PathLike)) and not _within(value):
                _stop(event)
                return


def main() -> int:
    global _armed, _root, _extra, _deny_imports, _deny_events

    snippet, scratch, result, config = sys.argv[1:5]
    with open(config, "r", encoding="utf-8") as handle:
        settings = json.load(handle)
    with open(snippet, "r", encoding="utf-8") as handle:
        source = handle.read()

    _root = os.path.normcase(os.path.abspath(scratch))
    _deny_imports = set(settings.get("deny_imports", []))
    _deny_events = set(settings.get("deny_events", []))
    _extra = [os.path.normcase(os.path.abspath(p))
              for p in settings.get("write_roots", [])]

    sys.setrecursionlimit(int(settings.get("recursion_limit", 400)))
    os.chdir(_root)
    sys.addaudithook(_hook)

    out, err = io.StringIO(), io.StringIO()
    real_out, real_err = sys.stdout, sys.stderr
    status, detail = "ok", ""

    sys.stdout, sys.stderr = out, err
    _armed = True
    try:
        namespace = {"__name__": "__main__", "__file__": os.path.abspath(snippet)}
        exec(compile(source, "<probe>", "exec"), namespace)
    except BaseException as exc:  # the snippet may raise anything
        _armed = False
        if isinstance(exc, PermissionError) and str(exc).startswith("probe guard:"):
            status = "blocked"
        else:
            status = "error"
        detail = f"{type(exc).__name__}: {exc}"
        traceback.print_exc(file=err)
    finally:
        _armed = False
        sys.stdout, sys.stderr = real_out, real_err

    payload = {
        "status": status,
        "detail": detail[:400],
        "stdout": out.getvalue()[:MAX_TEXT],
        "stderr": err.getvalue()[:MAX_TEXT],
        "blocked": _blocked[:20],
    }
    with open(result, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, ensure_ascii=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())