"""Structural reading of Python source: AST -> normalized IR -> signature.

Local names and literal values are abstracted away so one pattern covers many
concrete uses; API names, keywords and structure stay as anchors. The original
source is kept verbatim, Persian text included.
"""

from __future__ import annotations

import ast
import builtins
import copy
import hashlib
import re
from dataclasses import dataclass, field

PERSIAN = re.compile(r"[\u0600-\u06FF]")
BUILTINS = set(dir(builtins))
RISKY_CALLS = {
    "eval": "eval", "exec": "exec", "compile": "compile",
    "pickle.load": "pickle", "pickle.loads": "pickle",
    "os.remove": "delete-file", "os.unlink": "delete-file",
    "shutil.rmtree": "delete-tree", "os.system": "shell",
}
TAG_BY_API = (
    (("json.",), "json"),
    (("open", "pathlib.", "os.path", "shutil."), "file-io"),
    (("requests.", "urllib.", "http.", "socket."), "http"),
    (("tkinter", "ttk"), "gui"),
    (("sqlite3.",), "sqlite"),
    (("subprocess.", "os.system"), "process"),
    (("re.",), "regex"),
    (("threading.", "concurrent."), "threading"),
    (("asyncio.",), "async"),
    (("csv.",), "csv"),
    (("datetime", "time."), "time"),
)
TAG_BY_NODE = (
    (ast.Try, "try-except"), (ast.With, "with-context"), (ast.AsyncWith, "with-context"),
    (ast.ListComp, "comprehension"), (ast.DictComp, "comprehension"),
    (ast.SetComp, "comprehension"), (ast.GeneratorExp, "comprehension"),
    (ast.For, "loop"), (ast.While, "loop"), (ast.AsyncFor, "loop"),
    (ast.If, "branch"), (ast.Lambda, "lambda"), (ast.Await, "async"),
    (ast.Yield, "generator"), (ast.YieldFrom, "generator"),
    (ast.Raise, "raise"), (ast.Assert, "assert"), (ast.ClassDef, "class-def"),
)


@dataclass
class Unit:
    kind: str
    name: str
    shape: str
    signature: str
    apis: list[str] = field(default_factory=list)
    tags: list[str] = field(default_factory=list)
    flags: list[str] = field(default_factory=list)
    slots: list[dict] = field(default_factory=list)
    source: str = ""
    lineno: int = 0

    def as_dict(self) -> dict:
        return {
            "kind": self.kind, "name": self.name, "shape": self.shape,
            "signature": self.signature, "apis": self.apis, "tags": self.tags,
            "flags": self.flags, "slots": self.slots, "source": self.source,
            "lineno": self.lineno,
        }


def _role(value) -> str:
    if value is None:
        return "NONE"
    if isinstance(value, bool):
        return "BOOL"
    if isinstance(value, int):
        return "INT"
    if isinstance(value, float):
        return "FLOAT"
    if isinstance(value, complex):
        return "COMPLEX"
    if isinstance(value, bytes):
        return "BYTES"
    if isinstance(value, str):
        if value.startswith(("http://", "https://", "ftp://")):
            return "URL"
        if re.match(r"^[A-Za-z]:[\\/]", value) or value.startswith(("/", "\\\\")):
            return "PATH"
        if "\n" in value or len(value) > 40:
            return "TEXT"
        return "STR"
    return "OTHER"


def _dotted(node) -> str | None:
    parts: list[str] = []
    current = node
    while isinstance(current, ast.Attribute):
        parts.append(current.attr)
        current = current.value
    if isinstance(current, ast.Name):
        parts.append(current.id)
        return ".".join(reversed(parts))
    return None


class _Normalizer(ast.NodeTransformer):
    """Alpha-rename local names and replace literals with typed role slots."""

    def __init__(self, keep: set[str]):
        self.keep = keep
        self.names: dict[str, str] = {}
        self.slots: list[dict] = []

    def _alias(self, name: str) -> str:
        if name in self.keep or name.startswith("__"):
            return name
        return self.names.setdefault(name, f"v{len(self.names) + 1}")

    def visit_Name(self, node):
        return ast.copy_location(ast.Name(id=self._alias(node.id), ctx=node.ctx), node)

    def visit_arg(self, node):
        node.arg = self._alias(node.arg)
        node.annotation = None
        return node

    def visit_Constant(self, node):
        role = _role(node.value)
        self.slots.append({"role": role, "type": type(node.value).__name__})
        return ast.copy_location(ast.Name(id=f"<{role}>", ctx=ast.Load()), node)

    def visit_FunctionDef(self, node):
        node.name = self._alias(node.name)
        node.returns = None
        node.type_comment = None
        self.generic_visit(node)
        return node

    visit_AsyncFunctionDef = visit_FunctionDef

    def visit_ClassDef(self, node):
        node.name = self._alias(node.name)
        self.generic_visit(node)
        return node

    def visit_Global(self, node):
        node.names = [self._alias(name) for name in node.names]
        return node

    visit_Nonlocal = visit_Global


def _keep_names(tree: ast.AST) -> set[str]:
    keep = set(BUILTINS)
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                keep.add(alias.asname or alias.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for alias in node.names:
                keep.add(alias.asname or alias.name)
    return keep


def _apis(node: ast.AST) -> list[str]:
    found: list[str] = []
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            name = _dotted(child.func)
            if name and name not in found:
                found.append(name)
    return sorted(found)


def _tags(node: ast.AST, apis: list[str], source: str) -> list[str]:
    tags: list[str] = []
    for node_type, tag in TAG_BY_NODE:
        if tag not in tags and any(isinstance(c, node_type) for c in ast.walk(node)):
            tags.append(tag)
    for prefixes, tag in TAG_BY_API:
        if tag not in tags and any(api.startswith(prefixes) for api in apis):
            tags.append(tag)
    if PERSIAN.search(source or ""):
        tags.append("persian-text")
    return sorted(tags)


def _flags(node: ast.AST, apis: list[str]) -> list[str]:
    flags: list[str] = []
    for api in apis:
        label = RISKY_CALLS.get(api)
        if label and label not in flags:
            flags.append(label)
    for child in ast.walk(node):
        if isinstance(child, ast.Call):
            for keyword in child.keywords:
                value = keyword.value
                if not isinstance(value, ast.Constant):
                    continue
                if keyword.arg == "shell" and value.value is True and "shell-true" not in flags:
                    flags.append("shell-true")
                if keyword.arg == "verify" and value.value is False and "tls-off" not in flags:
                    flags.append("tls-off")
        if isinstance(child, ast.ExceptHandler) and child.type is None \
                and "bare-except" not in flags:
            flags.append("bare-except")
    return sorted(flags)


def _make_unit(kind: str, name: str, node: ast.AST, keep: set[str], source: str) -> Unit:
    segment = source if kind == "module" else (ast.get_source_segment(source, node) or "")
    apis = _apis(node)
    normalizer = _Normalizer(keep)
    # Deep-copy instead of re-parsing: method segments keep their indentation.
    normalized = normalizer.visit(copy.deepcopy(node))
    shape = ast.dump(normalized, annotate_fields=False, include_attributes=False)
    tags = _tags(node, apis, segment)
    signature = hashlib.sha1(
        (shape + "|" + ",".join(apis) + "|" + ",".join(tags)).encode("utf-8")
    ).hexdigest()[:16]
    return Unit(kind=kind, name=name, shape=shape, signature=signature, apis=apis,
                tags=tags, flags=_flags(node, apis), slots=normalizer.slots,
                source=segment, lineno=getattr(node, "lineno", 0))


def parse(source: str) -> list[Unit]:
    """Structural units of a module. Invalid syntax yields [], never raises."""
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError):
        return []
    keep = _keep_names(tree)
    units: list[Unit] = []
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            units.append(_make_unit("function", node.name, node, keep, source))
        elif isinstance(node, ast.ClassDef):
            units.append(_make_unit("class", node.name, node, keep, source))
            for child in node.body:
                if isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    units.append(_make_unit("method", child.name, child, keep, source))
    if not units:
        units.append(_make_unit("module", "<module>", tree, keep, source))
    return units