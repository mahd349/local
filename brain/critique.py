"""Code critique: quality analysis, smell detection, refactoring suggestions."""
from __future__ import annotations

import ast
import json
import time
from pathlib import Path
from typing import Any
from agent.config import STATE_DIR, assert_writable, get_logger

log = get_logger("critique")


class CritiqueReport:
    """Quality critique of a piece of code."""

    def __init__(
        self,
        exercise_id: str,
        score: float,
        issues: list[dict],
        refactors: list[str],
        metrics: dict,
        duration: float = 0.0
    ):
        self.exercise_id = exercise_id
        self.score = score
        self.issues = issues
        self.refactors = refactors
        self.metrics = metrics
        self.duration = duration

    def to_dict(self) -> dict:
        return {
            "exercise_id": self.exercise_id,
            "score": round(self.score, 1),
            "issues": self.issues,
            "refactors": self.refactors,
            "metrics": self.metrics,
            "duration": round(self.duration, 3),
        }


class Critique:
    """Static analysis and code quality critique."""

    def __init__(self):
        self.max_line_length = 100
        self.max_function_lines = 30
        self.max_arguments = 5
        self.max_nesting_depth = 4

    def critique(self, exercise_id: str, code: str) -> CritiqueReport:
        """Analyze code quality and produce a critique report."""
        start = time.time()
        issues: list[dict] = []
        refactors: list[str] = []

        # Parse the code
        try:
            tree = ast.parse(code)
        except SyntaxError as exc:
            return CritiqueReport(
                exercise_id=exercise_id,
                score=0.0,
                issues=[{
                    "severity": "critical",
                    "type": "syntax",
                    "message": f"Syntax error at line {exc.lineno}: {exc.msg}",
                }],
                refactors=["Fix syntax errors first."],
                metrics={},
                duration=time.time() - start,
            )

        lines = code.split("\n")

        # Collect metrics
        metrics = self._collect_metrics(tree, lines)

        # Check line length
        issues.extend(self._check_line_lengths(lines))

        # Check function complexity
        issues.extend(self._check_functions(tree, code))

        # Check naming conventions
        issues.extend(self._check_naming(tree))

        # Check for code smells
        issues.extend(self._check_smells(tree, code))

        # Check for anti-patterns
        issues.extend(self._check_anti_patterns(tree, code))

        # Generate refactoring suggestions
        refactors = self._suggest_refactors(issues, metrics)

        # Calculate score
        score = self._calculate_score(issues, metrics)

        return CritiqueReport(
            exercise_id=exercise_id,
            score=score,
            issues=issues,
            refactors=refactors,
            metrics=metrics,
            duration=time.time() - start,
        )

    def _collect_metrics(self, tree: ast.Module, lines: list[str]) -> dict:
        """Collect code metrics."""
        functions = [
            node for node in ast.walk(tree)
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        ]
        classes = [
            node for node in ast.walk(tree)
            if isinstance(node, ast.ClassDef)
        ]

        return {
            "total_lines": len(lines),
            "code_lines": sum(1 for l in lines if l.strip() and not l.strip().startswith("#")),
            "comment_lines": sum(1 for l in lines if l.strip().startswith("#")),
            "function_count": len(functions),
            "class_count": len(classes),
            "avg_function_lines": (
                sum(self._function_length(f) for f in functions) / len(functions)
                if functions else 0
            ),
            "max_function_lines": max(
                (self._function_length(f) for f in functions), default=0
            ),
            "total_args": sum(len(f.args.args) for f in functions),
        }

    def _function_length(self, node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
        """Get the number of lines in a function."""
        if not node.body:
            return 0
        first_line = node.body[0].lineno
        last_line = max(
            getattr(child, "end_lineno", child.lineno)
            for child in ast.walk(node)
            if hasattr(child, "lineno")
        )
        return last_line - first_line + 1

    def _check_line_lengths(self, lines: list[str]) -> list[dict]:
        """Check for lines that are too long."""
        issues = []
        for i, line in enumerate(lines, start=1):
            if len(line) > self.max_line_length:
                issues.append({
                    "severity": "warning",
                    "type": "line_length",
                    "message": f"Line {i} is {len(line)} chars (max {self.max_line_length})",
                })
        return issues

    def _check_functions(
        self,
        tree: ast.Module,
        code: str
    ) -> list[dict]:
        """Check function complexity."""
        issues = []

        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue

            # Check function length
            length = self._function_length(node)
            if length > self.max_function_lines:
                issues.append({
                    "severity": "warning",
                    "type": "function_length",
                    "message": (
                        f"Function '{node.name}' is {length} lines "
                        f"(max {self.max_function_lines})"
                    ),
                })

            # Check argument count
            arg_count = len(node.args.args) + len(node.args.kwonlyargs)
            if node.args.vararg:
                arg_count += 1
            if node.args.kwarg:
                arg_count += 1
            if arg_count > self.max_arguments:
                issues.append({
                    "severity": "warning",
                    "type": "too_many_args",
                    "message": (
                        f"Function '{node.name}' has {arg_count} arguments "
                        f"(max {self.max_arguments})"
                    ),
                })

            # Check nesting depth
            depth = self._max_nesting(node)
            if depth > self.max_nesting_depth:
                issues.append({
                    "severity": "warning",
                    "type": "deep_nesting",
                    "message": (
                        f"Function '{node.name}' has nesting depth {depth} "
                        f"(max {self.max_nesting_depth})"
                    ),
                })

        return issues

    def _max_nesting(self, node: ast.AST) -> int:
        """Calculate maximum nesting depth in a function."""
        max_depth = 0

        def _walk_depth(n: ast.AST, depth: int = 0):
            nonlocal max_depth
            if isinstance(n, (ast.If, ast.For, ast.While, ast.With, ast.Try)):
                depth += 1
                max_depth = max(max_depth, depth)
            for child in ast.iter_child_nodes(n):
                _walk_depth(child, depth)

        for child in ast.iter_child_nodes(node):
            _walk_depth(child)

        return max_depth

    def _check_naming(self, tree: ast.Module) -> list[dict]:
        """Check naming conventions."""
        issues = []

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                if not node.name.islower() and not node.name.startswith("_"):
                    issues.append({
                        "severity": "info",
                        "type": "naming",
                        "message": f"Function '{node.name}' should be lowercase_with_underscores",
                    })

            elif isinstance(node, ast.ClassDef):
                if not node.name[0].isupper():
                    issues.append({
                        "severity": "info",
                        "type": "naming",
                        "message": f"Class '{node.name}' should be CamelCase",
                    })

        return issues

    def _check_smells(self, tree: ast.Module, code: str) -> list[dict]:
        """Check for common code smells."""
        issues = []

        # Check for bare except
        for node in ast.walk(tree):
            if isinstance(node, ast.ExceptHandler):
                if node.type is None:
                    issues.append({
                        "severity": "warning",
                        "type": "bare_except",
                        "message": f"Line {node.lineno}: bare except clause",
                    })

            # Check for mutable default arguments
            if isinstance(node, ast.FunctionDef):
                for default in node.args.defaults:
                    if isinstance(default, (ast.List, ast.Dict, ast.Set)):
                        issues.append({
                            "severity": "warning",
                            "type": "mutable_default",
                            "message": (
                                f"Function '{node.name}' has mutable default argument"
                            ),
                        })

            # Check for global statements
            if isinstance(node, ast.Global):
                issues.append({
                    "severity": "warning",
                    "type": "global_statement",
                    "message": f"Line {node.lineno}: global statement used",
                })

        # Check for repeated code blocks (simple heuristic)
        lines = code.split("\n")
        seen_blocks: dict[str, int] = {}
        for i, line in enumerate(lines):
            stripped = line.strip()
            if len(stripped) > 20:
                if stripped in seen_blocks:
                    issues.append({
                        "severity": "info",
                        "type": "duplicate_code",
                        "message": (
                            f"Line {i+1} appears similar to line {seen_blocks[stripped]}"
                        ),
                    })
                seen_blocks[stripped] = i + 1

        return issues

    def _check_anti_patterns(self, tree: ast.Module, code: str) -> list[dict]:
        """Check for common anti-patterns."""
        issues = []

        for node in ast.walk(tree):
            # God class: too many methods
            if isinstance(node, ast.ClassDef):
                methods = [
                    child for child in node.body
                    if isinstance(child, ast.FunctionDef)
                ]
                if len(methods) > 15:
                    issues.append({
                        "severity": "warning",
                        "type": "god_class",
                        "message": f"Class '{node.name}' has {len(methods)} methods",
                    })

            # Nested functions deeper than 3 levels
            if isinstance(node, ast.FunctionDef):
                nested = [
                    child for child in ast.walk(node)
                    if isinstance(child, ast.FunctionDef) and child is not node
                ]
                if len(nested) > 3:
                    issues.append({
                        "severity": "info",
                        "type": "deep_nesting",
                        "message": f"Function '{node.name}' has {len(nested)} nested functions",
                    })

        return issues

    def _suggest_refactors(
        self,
        issues: list[dict],
        metrics: dict
    ) -> list[str]:
        """Generate refactoring suggestions based on issues."""
        refactors: list[str] = []

        issue_types = {i["type"] for i in issues}

        if "function_length" in issue_types:
            refactors.append(
                "Split long functions into smaller, focused functions."
            )
        if "too_many_args" in issue_types:
            refactors.append(
                "Reduce argument count using a dataclass, dict, or kwargs."
            )
        if "deep_nesting" in issue_types:
            refactors.append(
                "Reduce nesting with early returns, guard clauses, or extraction."
            )
        if "bare_except" in issue_types:
            refactors.append(
                "Catch specific exceptions instead of bare except."
            )
        if "mutable_default" in issue_types:
            refactors.append(
                "Use None as default and create mutable objects inside the function."
            )
        if "duplicate_code" in issue_types:
            refactors.append(
                "Extract repeated code into a shared helper function."
            )

        return refactors

    def _calculate_score(self, issues: list[dict], metrics: dict) -> float:
        """Calculate a quality score from 0 to 100."""
        score = 100.0

        for issue in issues:
            severity = issue.get("severity", "info")
            if severity == "critical":
                score -= 30
            elif severity == "warning":
                score -= 10
            elif severity == "info":
                score -= 3

        # Bonus for good metrics
        if metrics.get("comment_lines", 0) > 0:
            ratio = metrics["comment_lines"] / max(1, metrics.get("code_lines", 1))
            if 0.05 <= ratio <= 0.3:
                score += 5

        return max(0.0, min(100.0, score))


if __name__ == "__main__":
    critique = Critique()

    # Test with good code
    good_code = """
def add(a: int, b: int) -> int:
    \"\"\"Add two integers.\"\"\"
    return a + b
"""
    report = critique.critique("test_1", good_code)
    print(f"Good code score: {report.score}")
    print(f"Issues: {report.issues}")

    # Test with bad code
    bad_code = """
def BadFunc(a, b, c, d, e, f, g):
    x = []
    for i in range(100):
        for j in range(100):
            for k in range(100):
                if i + j + k > 50:
                    if i * j * k > 100:
                        x.append(i + j + k)
    return x
"""
    report = critique.critique("test_2", bad_code)
    print(f"Bad code score: {report.score}")
    print(f"Issues: {len(report.issues)}")
    for issue in report.issues:
        print(f"  [{issue['severity']}] {issue['message']}")
    print(f"Refactors: {report.refactors}")