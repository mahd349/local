"""Strong verification: parameterized tests, output comparison, error classification."""
from __future__ import annotations

import ast
import json
import re
import time
from pathlib import Path
from typing import Any
from agent.config import STATE_DIR, assert_writable, get_logger

from .sandbox import Sandbox

log = get_logger("verifier")

REPORT_DIR = STATE_DIR / "verify_reports"


class Verdict:
    """Result of a verification run."""
    PASS = "pass"
    FAIL = "fail"
    ERROR = "error"
    TIMEOUT = "timeout"
    PARTIAL = "partial"


class ErrorCategory:
    SYNTAX = "syntax"
    RUNTIME = "runtime"
    LOGIC = "logic"
    TYPE = "type"
    TIMEOUT = "timeout"
    MISSING = "missing"


class VerificationReport:
    """Detailed verification report."""

    def __init__(
        self,
        exercise_id: str,
        verdict: str,
        passed: int = 0,
        failed: int = 0,
        errors: list[dict] | None = None,
        duration: float = 0.0,
        code: str = "",
        suggestions: list[str] | None = None
    ):
        self.exercise_id = exercise_id
        self.verdict = verdict
        self.passed = passed
        self.failed = failed
        self.errors = errors or []
        self.duration = duration
        self.code = code
        self.suggestions = suggestions or []

    def to_dict(self) -> dict:
        return {
            "exercise_id": self.exercise_id,
            "verdict": self.verdict,
            "passed": self.passed,
            "failed": self.failed,
            "errors": self.errors,
            "duration": round(self.duration, 3),
            "suggestions": self.suggestions,
        }


class Verifier:
    """Runs parameterized tests against generated code."""

    def __init__(self, sandbox: Sandbox | None = None):
        self.sandbox = sandbox or Sandbox(timeout=5.0, memory_mb=256)

    def verify_exercise(
        self,
        exercise: dict,
        code: str,
        solution: str | None = None
    ) -> VerificationReport:
        """Verify a solution against an exercise definition."""
        start = time.time()

        # Step 1: syntax check
        syntax_error = self._check_syntax(code)
        if syntax_error:
            return VerificationReport(
                exercise_id=exercise.get("id", "unknown"),
                verdict=Verdict.ERROR,
                errors=[{"category": ErrorCategory.SYNTAX, "message": syntax_error}],
                duration=time.time() - start,
                code=code,
                suggestions=["Fix the syntax error before running tests."],
            )

        # Step 2: check required function exists
        missing = self._check_required_names(exercise, code)
        if missing:
            return VerificationReport(
                exercise_id=exercise.get("id", "unknown"),
                verdict=Verdict.ERROR,
                errors=[{
                    "category": ErrorCategory.MISSING,
                    "message": f"Missing definitions: {', '.join(missing)}",
                }],
                duration=time.time() - start,
                code=code,
                suggestions=[f"Define these names: {', '.join(missing)}"],
            )

        # Step 3: run test cases
        test_cases = exercise.get("test_cases", [])
        if not test_cases:
            return self._verify_no_tests(exercise, code, start)

        passed, failed, errors = 0, 0, []
        for index, test in enumerate(test_cases, start=1):
            result = self._run_single_test(exercise, code, test, index)
            if result["success"]:
                passed += 1
            else:
                failed += 1
                errors.append(result["error"])

        total = passed + failed
        if total > 0 and passed == total:
            verdict = Verdict.PASS
        elif passed > 0:
            verdict = Verdict.PARTIAL
        else:
            verdict = Verdict.FAIL

        suggestions = self._build_suggestions(errors, code, solution)

        return VerificationReport(
            exercise_id=exercise.get("id", "unknown"),
            verdict=verdict,
            passed=passed,
            failed=failed,
            errors=errors,
            duration=time.time() - start,
            code=code,
            suggestions=suggestions,
        )

    def _check_syntax(self, code: str) -> str:
        """Return syntax error message or empty string."""
        try:
            ast.parse(code)
            return ""
        except SyntaxError as exc:
            return f"Line {exc.lineno}: {exc.msg}"

    def _check_required_names(self, exercise: dict, code: str) -> list[str]:
        """Check that required function/class names are defined."""
        starter = exercise.get("starter_code", "")
        if not starter:
            return []

        try:
            tree = ast.parse(starter)
        except SyntaxError:
            return []

        required: set[str] = set()
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                required.add(node.name)
            elif isinstance(node, ast.ClassDef):
                required.add(node.name)

        try:
            code_tree = ast.parse(code)
        except SyntaxError:
            return sorted(required)

        defined: set[str] = set()
        for node in ast.walk(code_tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                defined.add(node.name)
            elif isinstance(node, ast.ClassDef):
                defined.add(node.name)
            elif isinstance(node, ast.Assign):
                for target in node.targets:
                    if isinstance(target, ast.Name):
                        defined.add(target.id)

        return sorted(required - defined)

    def _run_single_test(
        self,
        exercise: dict,
        code: str,
        test: dict,
        index: int
    ) -> dict:
        """Run one test case."""
        inputs = test.get("input", [])
        expected = test.get("expected")

        # Build a test harness that calls the main function
        func_name = self._extract_main_func(exercise)
        if not func_name:
            return {
                "success": False,
                "error": {
                    "category": ErrorCategory.MISSING,
                    "message": f"Test {index}: cannot find function to call",
                },
            }

        # Build test code
        args_repr = ", ".join(repr(a) for a in inputs)
        test_code = f"__result__ = {func_name}({args_repr})"

        result = self.sandbox.execute(code + "\n" + test_code)

        if not result.success:
            category = self._classify_error(result.error)
            return {
                "success": False,
                "error": {
                    "category": category,
                    "message": f"Test {index}: {result.error[:300]}",
                },
            }

        # Compare result
        # We need to extract __result__ from the execution
        # Since sandbox doesn't return locals, we use a print-based approach
        check_code = (
            f"{code}\n"
            f"__result__ = {func_name}({args_repr})\n"
            f"print(repr(__result__))"
        )
        result = self.sandbox.execute(check_code)

        if not result.success:
            return {
                "success": False,
                "error": {
                    "category": ErrorCategory.RUNTIME,
                    "message": f"Test {index}: {result.error[:300]}",
                },
            }

        actual_str = result.output.strip().split("\n")[-1] if result.output.strip() else ""
        expected_str = repr(expected)

        if actual_str == expected_str:
            return {"success": True}

        return {
            "success": False,
            "error": {
                "category": ErrorCategory.LOGIC,
                "message": (
                    f"Test {index}: expected {expected_str}, "
                    f"got {actual_str}"
                ),
            },
        }

    def _extract_main_func(self, exercise: dict) -> str | None:
        """Extract the main function name from starter code."""
        starter = exercise.get("starter_code", "")
        try:
            tree = ast.parse(starter)
        except SyntaxError:
            return None

        for node in ast.walk(tree):
            if isinstance(node, ast.FunctionDef):
                return node.name
        return None

    def _classify_error(self, error: str) -> str:
        """Classify an error into a category."""
        if "SyntaxError" in error:
            return ErrorCategory.SYNTAX
        if "TypeError" in error:
            return ErrorCategory.TYPE
        if "Timeout" in error:
            return ErrorCategory.TIMEOUT
        return ErrorCategory.RUNTIME

    def _verify_no_tests(
        self,
        exercise: dict,
        code: str,
        start: float
    ) -> VerificationReport:
        """For exercises without test cases, just run the code."""
        result = self.sandbox.execute(code)

        if result.success:
            verdict = Verdict.PASS
        else:
            verdict = Verdict.ERROR

        return VerificationReport(
            exercise_id=exercise.get("id", "unknown"),
            verdict=verdict,
            passed=1 if result.success else 0,
            failed=0 if result.success else 1,
            errors=[] if result.success else [{
                "category": self._classify_error(result.error),
                "message": result.error[:300],
            }],
            duration=time.time() - start,
            code=code,
        )

    def _build_suggestions(
        self,
        errors: list[dict],
        code: str,
        solution: str | None
    ) -> list[str]:
        """Build improvement suggestions from errors."""
        suggestions: list[str] = []

        categories = {e.get("category") for e in errors}

        if ErrorCategory.LOGIC in categories:
            suggestions.append(
                "Logic error: check your algorithm and edge cases."
            )
        if ErrorCategory.TYPE in categories:
            suggestions.append(
                "Type error: check argument types and return values."
            )
        if ErrorCategory.TIMEOUT in categories:
            suggestions.append(
                "Timeout: optimize your algorithm or reduce complexity."
            )
        if ErrorCategory.RUNTIME in categories:
            suggestions.append(
                "Runtime error: add bounds checking and handle edge cases."
            )

        return suggestions


if __name__ == "__main__":
    verifier = Verifier()

    # Test with a simple exercise
    exercise = {
        "id": "test_1",
        "starter_code": "def add(a, b):\n    pass",
        "solution": "def add(a, b):\n    return a + b",
        "test_cases": [
            {"input": [2, 3], "expected": 5},
            {"input": [-1, 1], "expected": 0},
        ],
    }

    # Test passing code
    report = verifier.verify_exercise(exercise, "def add(a, b):\n    return a + b")
    print(f"Verdict: {report.verdict}")
    print(f"Passed: {report.passed}, Failed: {report.failed}")

    # Test failing code
    report = verifier.verify_exercise(exercise, "def add(a, b):\n    return a - b")
    print(f"Verdict: {report.verdict}")
    print(f"Passed: {report.passed}, Failed: {report.failed}")
    for err in report.errors:
        print(f"  Error: {err['message']}")