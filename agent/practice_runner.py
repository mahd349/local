"""Practice runner: generate code, execute, verify, critique, iterate."""
from __future__ import annotations

import argparse
import json
import time
from pathlib import Path
from typing import Any

from .config import REPORT_DIR, STATE_DIR, assert_writable, get_logger
from .errors import AgentError
from .notify import Notifier
from .store import Store
from brain.curriculum import Curriculum
from brain.exercise import ExerciseBank
from brain.verifier import Verifier, Verdict
from brain.critique import Critique
from brain.pattern_lib import PatternLibrary
from brain.sandbox import Sandbox
from brain.resource_limiter import ResourceLimiter

log = get_logger("practice_runner")

PRACTICE_DIR = STATE_DIR / "practice"


class CodeGenerator:
    """Generates code from exercise definitions and patterns."""
    
    def __init__(self, pattern_lib: PatternLibrary):
        self.pattern_lib = pattern_lib
    
    def generate_from_exercise(self, exercise: dict) -> str:
        """Generate code from an exercise definition.
        
        Uses the solution if available, otherwise generates from
        starter code and hints.
        """
        # If we have a solution, use it
        solution = exercise.get("solution")
        if solution:
            return solution
        
        # Otherwise, try to generate from starter code
        starter = exercise.get("starter_code", "")
        if not starter:
            raise AgentError("No starter code or solution for exercise")
        
        # For now, return starter code
        # In a full system, this would use AI or pattern matching
        return starter
    
    def generate_from_pattern(
        self,
        pattern: dict,
        exercise: dict | None = None
    ) -> str:
        """Generate code from a pattern definition."""
        examples = pattern.get("examples", [])
        if not examples:
            raise AgentError("Pattern has no examples")
        
        # Return the first example as a base
        return examples[0].get("code", "")
    
    def generate_compositional(
        self,
        patterns: list[dict],
        exercise: dict
    ) -> str:
        """Generate code by composing multiple patterns.
        
        This is the key to creativity: instead of copying one source,
        the agent combines patterns to solve new problems.
        """
        parts = []
        
        for pattern in patterns:
            examples = pattern.get("examples", [])
            if examples:
                parts.append(examples[0].get("code", ""))
        
        if not parts:
            raise AgentError("No patterns to compose")
        
        return "\n\n".join(parts)


class PracticeRunner:
    """Runs practice exercises with code generation, verification, and critique."""
    
    def __init__(
        self,
        store: Store,
        notifier: Notifier,
        limiter: ResourceLimiter | None = None
    ):
        self.store = store
        self.notifier = notifier
        self.limiter = limiter or ResourceLimiter()
        
        self.curriculum = Curriculum()
        self.exercise_bank = ExerciseBank()
        self.verifier = Verifier()
        self.critique = Critique()
        self.pattern_lib = PatternLibrary()
        self.generator = CodeGenerator(self.pattern_lib)
        
        PRACTICE_DIR.mkdir(parents=True, exist_ok=True)
    
    def run_practice_session(
        self,
        count: int = 1,
        level: str | None = None
    ) -> list[dict]:
        """Run a practice session with multiple exercises."""
        results = []
        
        for i in range(count):
            self.notifier.check_stop()
            
            with self.limiter.limit():
                result = self.run_single_practice(level)
                results.append(result)
                
                if result.get("status") == "error":
                    log.warning("Practice error: %s", result.get("message"))
        
        # Save session report
        self._save_session_report(results)
        
        return results
    
    def run_single_practice(self, level: str | None = None) -> dict:
        """Run a single practice exercise."""
        start_time = time.time()
        
        # Step 1: Select exercise
        exercise, topic_id = self._select_exercise(level)
        if not exercise:
            return {"status": "no_exercises", "message": "No exercises available"}
        
        exercise_id = exercise["id"]
        
        # Step 2: Generate code
        try:
            code = self.generator.generate_from_exercise(exercise)
        except AgentError as exc:
            return {
                "status": "error",
                "exercise": exercise_id,
                "message": str(exc)
            }
        
        # Step 3: Verify
        report = self.verifier.verify_exercise(
            exercise=exercise,
            code=code,
            solution=exercise.get("solution")
        )
        
        # Step 4: Critique
        critique_report = self.critique.critique(exercise_id, code)
        
        # Step 5: Record attempt
        success = report.verdict == Verdict.PASS
        
        self.exercise_bank.record_attempt(
            exercise_id=exercise_id,
            code=code,
            success=success,
            time_spent=time.time() - start_time
        )
        
        # Step 6: Build result
        duration = time.time() - start_time
        
        result = {
            "status": "completed",
            "exercise": exercise_id,
            "topic": topic_id,
            "success": success,
            "verdict": report.verdict,
            "passed_tests": report.passed,
            "failed_tests": report.failed,
            "critique_score": critique_report.score,
            "issues": critique_report.issues,
            "refactors": critique_report.refactors,
            "duration": round(duration, 2),
            "timestamp": time.time()
        }
        
        # Step 7: If failed, suggest improvements
        if not success and report.suggestions:
            result["suggestions"] = report.suggestions
        
        return result
    
    def _select_exercise(
        self,
        level: str | None = None
    ) -> tuple[dict | None, str | None]:
        """Select the next exercise to practice."""
        # Get next topic from curriculum
        if level:
            topics = self.curriculum.get_topics_for_level(level)
            for topic in topics:
                exercise = self.exercise_bank.get_next_exercise(topic["id"])
                if exercise:
                    return exercise, topic["id"]
            return None, None
        
        topic = self.curriculum.get_next_topic()
        if not topic:
            return None, None
        
        topic_id = topic["id"]
        exercise = self.exercise_bank.get_next_exercise(topic_id)
        
        return exercise, topic_id
    
    def _save_session_report(self, results: list[dict]) -> Path:
        """Save practice session report."""
        REPORT_DIR.mkdir(parents=True, exist_ok=True)
        
        timestamp = int(time.time())
        path = assert_writable(REPORT_DIR / f"practice_{timestamp}.json")
        
        path.write_text(
            json.dumps(results, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
        
        return path
    
    def get_progress(self) -> dict:
        """Get overall practice progress."""
        curriculum_progress = self.curriculum.get_progress()
        pattern_progress = self.pattern_lib.get_learning_progress()
        
        return {
            "curriculum": curriculum_progress,
            "patterns": pattern_progress,
            "practice_dir": str(PRACTICE_DIR)
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent.practice_runner",
        description="Practice runner for self-directed learning"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    
    run_parser = sub.add_parser("run", help="Run practice exercises")
    run_parser.add_argument("--count", type=int, default=1)
    run_parser.add_argument("--level", default=None)
    
    sub.add_parser("progress", help="Show practice progress")
    
    args = parser.parse_args(argv)
    
    store = Store()
    notifier = Notifier(interactive=True)
    
    try:
        runner = PracticeRunner(store, notifier)
        
        if args.command == "run":
            results = runner.run_practice_session(
                count=args.count,
                level=args.level
            )
            
            for result in results:
                print(f"Exercise: {result.get('exercise')}")
                print(f"  Status: {result.get('status')}")
                print(f"  Success: {result.get('success')}")
                print(f"  Verdict: {result.get('verdict')}")
                print(f"  Critique: {result.get('critique_score')}")
                print()
            
            return 0
        
        elif args.command == "progress":
            progress = runner.get_progress()
            print(json.dumps(progress, ensure_ascii=False, indent=2))
            return 0
        
        return 1
    
    except (AgentError, KeyboardInterrupt) as exc:
        print("Stopped:", exc)
        return 1
    finally:
        store.close()


if __name__ == "__main__":
    raise SystemExit(main())