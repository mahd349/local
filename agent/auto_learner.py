"""Auto learner daemon: continuous self-directed learning with resource limits."""
from __future__ import annotations

import argparse
import json
import os
import signal
import sys
import time
import threading
from datetime import datetime
from pathlib import Path
from typing import Any

# Add parent directory to path for imports
sys.path.insert(0, str(Path(__file__).parent.parent))

from agent.config import STATE_DIR, assert_writable, get_logger
from brain.curriculum import Curriculum, Level
from brain.exercise import ExerciseBank
from brain.verifier import Verifier, Verdict
from brain.critique import Critique
from brain.pattern_lib import PatternLibrary
from brain.resource_limiter import ResourceLimiter

log = get_logger("auto_learner")

CONFIG_PATH = STATE_DIR / "auto_learner_config.json"
PROGRESS_PATH = STATE_DIR / "auto_learner_progress.json"
LOG_PATH = STATE_DIR / "auto_learner_log.json"


class AutoLearnerConfig:
    """Configuration for the auto learner."""
    
    def __init__(
        self,
        enabled: bool = True,
        cpu_limit_background: float = 50.0,
        cpu_limit_active: float = 100.0,
        memory_limit_mb: int = 512,
        exercise_interval_minutes: int = 60,
        max_exercises_per_day: int = 10,
        pause_when_user_active: bool = False
    ):
        self.enabled = enabled
        self.cpu_limit_background = cpu_limit_background
        self.cpu_limit_active = cpu_limit_active
        self.memory_limit_mb = memory_limit_mb
        self.exercise_interval_minutes = exercise_interval_minutes
        self.max_exercises_per_day = max_exercises_per_day
        self.pause_when_user_active = pause_when_user_active
    
    @classmethod
    def from_dict(cls, data: dict) -> "AutoLearnerConfig":
        return cls(
            enabled=data.get("enabled", True),
            cpu_limit_background=data.get("cpu_limit_background", 50.0),
            cpu_limit_active=data.get("cpu_limit_active", 100.0),
            memory_limit_mb=data.get("memory_limit_mb", 512),
            exercise_interval_minutes=data.get("exercise_interval_minutes", 60),
            max_exercises_per_day=data.get("max_exercises_per_day", 10),
            pause_when_user_active=data.get("pause_when_user_active", False)
        )
    
    def to_dict(self) -> dict:
        return {
            "enabled": self.enabled,
            "cpu_limit_background": self.cpu_limit_background,
            "cpu_limit_active": self.cpu_limit_active,
            "memory_limit_mb": self.memory_limit_mb,
            "exercise_interval_minutes": self.exercise_interval_minutes,
            "max_exercises_per_day": self.max_exercises_per_day,
            "pause_when_user_active": self.pause_when_user_active
        }


class AutoLearnerState:
    """Tracks the state of the auto learner."""
    
    def __init__(self):
        self.total_exercises_completed = 0
        self.total_exercises_attempted = 0
        self.total_time_spent = 0.0
        self.exercises_today = 0
        self.last_exercise_time = 0.0
        self.last_reset_date = datetime.now().strftime("%Y-%m-%d")
        self.current_level = Level.BEGINNER
        self.current_topic = None
        self.current_exercise = None
        self.consecutive_failures = 0
        self.total_critique_score = 0.0
        self.critique_count = 0
    
    @classmethod
    def from_dict(cls, data: dict) -> "AutoLearnerState":
        state = cls()
        state.total_exercises_completed = data.get("total_exercises_completed", 0)
        state.total_exercises_attempted = data.get("total_exercises_attempted", 0)
        state.total_time_spent = data.get("total_time_spent", 0.0)
        state.exercises_today = data.get("exercises_today", 0)
        state.last_exercise_time = data.get("last_exercise_time", 0.0)
        state.last_reset_date = data.get("last_reset_date", datetime.now().strftime("%Y-%m-%d"))
        state.current_level = data.get("current_level", Level.BEGINNER)
        state.current_topic = data.get("current_topic")
        state.current_exercise = data.get("current_exercise")
        state.consecutive_failures = data.get("consecutive_failures", 0)
        state.total_critique_score = data.get("total_critique_score", 0.0)
        state.critique_count = data.get("critique_count", 0)
        return state
    
    def to_dict(self) -> dict:
        return {
            "total_exercises_completed": self.total_exercises_completed,
            "total_exercises_attempted": self.total_exercises_attempted,
            "total_time_spent": self.total_time_spent,
            "exercises_today": self.exercises_today,
            "last_exercise_time": self.last_exercise_time,
            "last_reset_date": self.last_reset_date,
            "current_level": self.current_level,
            "current_topic": self.current_topic,
            "current_exercise": self.current_exercise,
            "consecutive_failures": self.consecutive_failures,
            "total_critique_score": self.total_critique_score,
            "critique_count": self.critique_count
        }
    
    def reset_daily_counter(self) -> None:
        """Reset daily counter if it's a new day."""
        today = datetime.now().strftime("%Y-%m-%d")
        if self.last_reset_date != today:
            self.exercises_today = 0
            self.last_reset_date = today


class AutoLearner:
    """Main auto learner daemon."""
    
    def __init__(self, config: AutoLearnerConfig | None = None):
        self.config = config or self._load_config()
        self.state = self._load_state()
        self.running = False
        self.user_active = False
        self._stop_event = threading.Event()
        
        # Initialize components
        self.curriculum = Curriculum()
        self.exercise_bank = ExerciseBank()
        self.verifier = Verifier()
        self.critique = Critique()
        self.pattern_lib = PatternLibrary()
        
        # Resource limiter
        self.limiter = ResourceLimiter(
            cpu_percent=self.config.cpu_limit_background,
            memory_mb=self.config.memory_limit_mb
        )
    
    def _load_config(self) -> AutoLearnerConfig:
        """Load configuration from disk."""
        if CONFIG_PATH.exists():
            try:
                data = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
                return AutoLearnerConfig.from_dict(data)
            except (json.JSONDecodeError, OSError):
                pass
        
        config = AutoLearnerConfig()
        self._save_config(config)
        return config
    
    def _save_config(self, config: AutoLearnerConfig) -> None:
        """Save configuration to disk."""
        CONFIG_PATH.parent.mkdir(parents=True, exist_ok=True)
        CONFIG_PATH.write_text(
            json.dumps(config.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    
    def _load_state(self) -> AutoLearnerState:
        """Load state from disk."""
        if PROGRESS_PATH.exists():
            try:
                data = json.loads(PROGRESS_PATH.read_text(encoding="utf-8"))
                return AutoLearnerState.from_dict(data)
            except (json.JSONDecodeError, OSError):
                pass
        
        state = AutoLearnerState()
        self._save_state(state)
        return state
    
    def _save_state(self, state: AutoLearnerState) -> None:
        """Save state to disk."""
        PROGRESS_PATH.parent.mkdir(parents=True, exist_ok=True)
        PROGRESS_PATH.write_text(
            json.dumps(state.to_dict(), ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    
    def _log(self, message: str, level: str = "info") -> None:
        """Log a message."""
        timestamp = datetime.now().isoformat()
        log_entry = {
            "timestamp": timestamp,
            "level": level,
            "message": message
        }
        
        log.info(f"[{level.upper()}] {message}")
        
        # Also write to file
        if LOG_PATH.exists():
            try:
                logs = json.loads(LOG_PATH.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                logs = []
        else:
            logs = []
        
        logs.append(log_entry)
        
        # Keep only last 1000 entries
        logs = logs[-1000:]
        
        LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
        LOG_PATH.write_text(
            json.dumps(logs, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    
    def set_user_active(self, active: bool) -> None:
        """Set whether user is actively using the system."""
        self.user_active = active
        
        if active:
            self.limiter.set_cpu_limit(self.config.cpu_limit_active)
            self._log("User active: increased CPU limit to 100%")
        else:
            self.limiter.set_cpu_limit(self.config.cpu_limit_background)
            self._log("User inactive: reduced CPU limit to background level")
    
    def can_run_exercise(self) -> bool:
        """Check if we can run another exercise."""
        if not self.config.enabled:
            return False
        
        # Check daily limit
        self.state.reset_daily_counter()
        if self.state.exercises_today >= self.config.max_exercises_per_day:
            return False
        
        # Check time interval
        now = time.time()
        interval_seconds = self.config.exercise_interval_minutes * 60
        if now - self.state.last_exercise_time < interval_seconds:
            return False
        
        return True
    
    def run_exercise_cycle(self) -> dict:
        """Run one exercise cycle: select, attempt, verify, critique."""
        start_time = time.time()
        
        self._log("Starting exercise cycle")
        
        # Step 1: Get next exercise
        topic = self.curriculum.get_next_topic()
        if not topic:
            self._log("No more topics available", "warning")
            return {"status": "no_topics"}
        
        topic_id = topic["id"]
        self.state.current_topic = topic_id
        
        # Step 2: Get exercise for topic
        exercise = self.exercise_bank.get_next_exercise(topic_id)
        if not exercise:
            self._log(f"No exercises for topic {topic_id}", "warning")
            return {"status": "no_exercises", "topic": topic_id}
        
        exercise_id = exercise["id"]
        self.state.current_exercise = exercise_id
        self.state.total_exercises_attempted += 1
        
        self._log(f"Starting exercise: {exercise_id}")
        
        # Step 3: Attempt the exercise
        # In a real system, this would use AI to generate code
        # For now, we use the solution if available
        solution = exercise.get("solution", "")
        
        if not solution:
            self._log(f"No solution for exercise {exercise_id}", "warning")
            return {"status": "no_solution", "exercise": exercise_id}
        
        # Step 4: Verify the solution
        report = self.verifier.verify_exercise(exercise, solution)
        
        self._log(f"Verification: {report.verdict} "
                  f"({report.passed}/{report.passed + report.failed})")
        
        # Step 5: Critique the solution
        critique_report = self.critique.critique(exercise_id, solution)
        
        self._log(f"Critique score: {critique_report.score:.1f}")
        
        # Step 6: Record results
        success = report.verdict == Verdict.PASS
        
        self.exercise_bank.record_attempt(
            exercise_id=exercise_id,
            code=solution,
            success=success,
            time_spent=time.time() - start_time
        )
        
        # Update state
        self.state.exercises_today += 1
        self.state.last_exercise_time = time.time()
        self.state.total_time_spent += time.time() - start_time
        
        if success:
            self.state.total_exercises_completed += 1
            self.state.consecutive_failures = 0
        else:
            self.state.consecutive_failures += 1
        
        self.state.total_critique_score += critique_report.score
        self.state.critique_count += 1
        
        self._save_state(self.state)
        
        # Step 7: Update curriculum if exercise completed
        if success:
            self.curriculum.record_topic_completion(
                topic_id=topic_id,
                exercises_completed=1,
                score=critique_report.score,
                time_spent=time.time() - start_time
            )
        
        return {
            "status": "completed",
            "exercise": exercise_id,
            "topic": topic_id,
            "success": success,
            "verdict": report.verdict,
            "critique_score": critique_report.score,
            "duration": time.time() - start_time
        }
    
    def run_daemon(self) -> None:
        """Run the auto learner as a daemon."""
        self.running = True
        self._log("Auto learner daemon started")
        
        try:
            while self.running and not self._stop_event.is_set():
                # Check if we can run an exercise
                if self.can_run_exercise():
                    with self.limiter.limit():
                        result = self.run_exercise_cycle()
                        self._log(f"Exercise result: {result.get('status')}")
                
                # Sleep for a bit
                time.sleep(60)
                
        except KeyboardInterrupt:
            self._log("Auto learner stopped by user")
        except Exception as e:
            self._log(f"Auto learner error: {e}", "error")
        finally:
            self.running = False
            self._save_state(self.state)
    
    def stop(self) -> None:
        """Stop the daemon."""
        self._stop_event.set()
        self.running = False
    
    def get_status(self) -> dict:
        """Get current status."""
        self.state.reset_daily_counter()
        
        return {
            "enabled": self.config.enabled,
            "user_active": self.user_active,
            "cpu_limit": self.limiter.cpu_percent,
            "state": self.state.to_dict(),
            "curriculum_progress": self.curriculum.get_progress(),
            "pattern_progress": self.pattern_lib.get_learning_progress(),
            "average_critique_score": (
                self.state.total_critique_score / self.state.critique_count
                if self.state.critique_count > 0 else 0
            )
        }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="agent.auto_learner",
        description="Auto learner daemon"
    )
    sub = parser.add_subparsers(dest="command", required=True)
    
    sub.add_parser("start", help="Start the daemon")
    sub.add_parser("status", help="Show status")
    
    config_parser = sub.add_parser("config", help="Configure the learner")
    config_parser.add_argument("--cpu-background", type=float, default=None)
    config_parser.add_argument("--cpu-active", type=float, default=None)
    config_parser.add_argument("--memory", type=int, default=None)
    config_parser.add_argument("--interval", type=int, default=None)
    config_parser.add_argument("--max-daily", type=int, default=None)
    config_parser.add_argument("--enable", action="store_true")
    config_parser.add_argument("--disable", action="store_true")
    
    sub.add_parser("run-once", help="Run one exercise cycle")
    
    args = parser.parse_args(argv)
    
    learner = AutoLearner()
    
    if args.command == "start":
        learner.run_daemon()
        return 0
    
    elif args.command == "status":
        status = learner.get_status()
        print(json.dumps(status, ensure_ascii=False, indent=2))
        return 0
    
    elif args.command == "config":
        config = learner.config
        
        if args.cpu_background is not None:
            config.cpu_limit_background = args.cpu_background
        if args.cpu_active is not None:
            config.cpu_limit_active = args.cpu_active
        if args.memory is not None:
            config.memory_limit_mb = args.memory
        if args.interval is not None:
            config.exercise_interval_minutes = args.interval
        if args.max_daily is not None:
            config.max_exercises_per_day = args.max_daily
        if args.enable:
            config.enabled = True
        if args.disable:
            config.enabled = False
        
        learner._save_config(config)
        print("Configuration updated")
        return 0
    
    elif args.command == "run-once":
        result = learner.run_exercise_cycle()
        print(json.dumps(result, ensure_ascii=False, indent=2))
        return 0
    
    return 1


if __name__ == "__main__":
    raise SystemExit(main())