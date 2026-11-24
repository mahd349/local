"""Skill tracker with spaced repetition based on the forgetting curve."""
from __future__ import annotations

import json
import math
import time
from pathlib import Path
from typing import Any
from .config import STATE_DIR, assert_writable, get_logger

log = get_logger("skill_tracker")

SKILL_DB = STATE_DIR / "skills.json"


class MasteryLevel:
    """Mastery levels for a skill."""
    NEW = 0
    FAMILIAR = 1
    PRACTICED = 2
    PROFICIENT = 3
    MASTERED = 4
    
    NAMES = {
        NEW: "New",
        FAMILIAR: "Familiar",
        PRACTICED: "Practiced",
        PROFICIENT: "Proficient",
        MASTERED: "Mastered"
    }


class SpacedRepetition:
    """Spaced repetition algorithm based on SM-2 (SuperMemo 2)."""
    
    # Intervals in days for each repetition
    BASE_INTERVALS = [1, 3, 7, 14, 30]
    
    def __init__(self, ease_factor: float = 2.5):
        self.ease_factor = ease_factor
    
    def next_interval(
        self,
        repetition: int,
        quality: float,
        previous_interval: float = 0
    ) -> float:
        """Calculate the next review interval in days.
        
        quality: 0-5 scale where:
            0-2: complete failure
            3: correct with difficulty
            4: correct with some hesitation
            5: perfect response
        """
        if quality < 3:
            # Failed: reset to first interval
            return self.BASE_INTERVALS[0]
        
        if repetition == 0:
            return self.BASE_INTERVALS[0]
        elif repetition == 1:
            return self.BASE_INTERVALS[1]
        else:
            # Update ease factor
            self.ease_factor = max(
                1.3,
                self.ease_factor + (0.1 - (5 - quality) * (0.08 + (5 - quality) * 0.02))
            )
            
            return previous_interval * self.ease_factor
    
    def quality_from_score(self, score: float) -> int:
        """Convert a 0-100 score to a 0-5 quality rating."""
        if score >= 90:
            return 5
        elif score >= 75:
            return 4
        elif score >= 60:
            return 3
        elif score >= 40:
            return 2
        else:
            return 1


class Skill:
    """Represents a single skill."""
    
    def __init__(
        self,
        name: str,
        category: str,
        level: int = MasteryLevel.NEW,
        repetitions: int = 0,
        ease_factor: float = 2.5,
        interval_days: float = 0,
        last_review: float = 0,
        next_review: float = 0,
        total_attempts: int = 0,
        total_successes: int = 0,
        total_score: float = 0
    ):
        self.name = name
        self.category = category
        self.level = level
        self.repetitions = repetitions
        self.ease_factor = ease_factor
        self.interval_days = interval_days
        self.last_review = last_review
        self.next_review = next_review
        self.total_attempts = total_attempts
        self.total_successes = total_successes
        self.total_score = total_score
    
    @classmethod
    def from_dict(cls, data: dict) -> "Skill":
        return cls(
            name=data["name"],
            category=data["category"],
            level=data.get("level", MasteryLevel.NEW),
            repetitions=data.get("repetitions", 0),
            ease_factor=data.get("ease_factor", 2.5),
            interval_days=data.get("interval_days", 0),
            last_review=data.get("last_review", 0),
            next_review=data.get("next_review", 0),
            total_attempts=data.get("total_attempts", 0),
            total_successes=data.get("total_successes", 0),
            total_score=data.get("total_score", 0)
        )
    
    def to_dict(self) -> dict:
        return {
            "name": self.name,
            "category": self.category,
            "level": self.level,
            "level_name": MasteryLevel.NAMES.get(self.level, "Unknown"),
            "repetitions": self.repetitions,
            "ease_factor": round(self.ease_factor, 2),
            "interval_days": round(self.interval_days, 1),
            "last_review": self.last_review,
            "next_review": self.next_review,
            "total_attempts": self.total_attempts,
            "total_successes": self.total_successes,
            "success_rate": (
                self.total_successes / self.total_attempts
                if self.total_attempts > 0 else 0
            ),
            "average_score": (
                self.total_score / self.total_attempts
                if self.total_attempts > 0 else 0
            )
        }
    
    def is_due_for_review(self) -> bool:
        """Check if the skill is due for review."""
        return time.time() >= self.next_review


class SkillTracker:
    """Tracks skills with spaced repetition."""
    
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or SKILL_DB
        self.skills: dict[str, Skill] = {}
        self.repetition = SpacedRepetition()
        self._load()
    
    def _load(self) -> None:
        """Load skills from disk."""
        if self.db_path.exists():
            try:
                data = json.loads(self.db_path.read_text(encoding="utf-8"))
                for name, skill_data in data.items():
                    self.skills[name] = Skill.from_dict(skill_data)
            except (json.JSONDecodeError, OSError, KeyError):
                pass
    
    def _save(self) -> None:
        """Save skills to disk."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        
        data = {
            name: skill.to_dict()
            for name, skill in self.skills.items()
        }
        
        self.db_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    
    def get_or_create_skill(self, name: str, category: str) -> Skill:
        """Get an existing skill or create a new one."""
        if name not in self.skills:
            self.skills[name] = Skill(name=name, category=category)
        
        return self.skills[name]
    
    def record_practice(
        self,
        name: str,
        category: str,
        score: float,
        success: bool
    ) -> Skill:
        """Record a practice attempt for a skill."""
        skill = self.get_or_create_skill(name, category)
        
        skill.total_attempts += 1
        if success:
            skill.total_successes += 1
        skill.total_score += score
        
        # Calculate quality rating
        quality = self.repetition.quality_from_score(score)
        
        # Update spaced repetition
        skill.interval_days = self.repetition.next_interval(
            repetition=skill.repetitions,
            quality=quality,
            previous_interval=skill.interval_days
        )
        
        skill.ease_factor = self.repetition.ease_factor
        skill.repetitions += 1
        skill.last_review = time.time()
        skill.next_review = time.time() + (skill.interval_days * 86400)
        
        # Update mastery level
        skill.level = self._calculate_mastery(skill)
        
        self._save()
        
        return skill
    
    def _calculate_mastery(self, skill: Skill) -> int:
        """Calculate mastery level based on performance."""
        if skill.total_attempts == 0:
            return MasteryLevel.NEW
        
        success_rate = skill.total_successes / skill.total_attempts
        avg_score = skill.total_score / skill.total_attempts
        
        if skill.repetitions >= 10 and success_rate >= 0.9 and avg_score >= 90:
            return MasteryLevel.MASTERED
        elif skill.repetitions >= 5 and success_rate >= 0.8 and avg_score >= 80:
            return MasteryLevel.PROFICIENT
        elif skill.repetitions >= 3 and success_rate >= 0.6:
            return MasteryLevel.PRACTICED
        elif skill.repetitions >= 1:
            return MasteryLevel.FAMILIAR
        
        return MasteryLevel.NEW
    
    def get_due_skills(self) -> list[Skill]:
        """Get skills that are due for review."""
        due = [
            skill for skill in self.skills.values()
            if skill.is_due_for_review()
        ]
        
        # Sort by next_review (most overdue first)
        due.sort(key=lambda s: s.next_review)
        
        return due
    
    def get_skills_by_category(self, category: str) -> list[Skill]:
        """Get all skills in a category."""
        return [
            skill for skill in self.skills.values()
            if skill.category == category
        ]
    
    def get_progress(self) -> dict:
        """Get overall progress."""
        total_skills = len(self.skills)
        
        by_level = {}
        for level_name in MasteryLevel.NAMES.values():
            by_level[level_name] = 0
        
        total_attempts = 0
        total_successes = 0
        
        for skill in self.skills.values():
            level_name = MasteryLevel.NAMES.get(skill.level, "Unknown")
            if level_name in by_level:
                by_level[level_name] += 1
            
            total_attempts += skill.total_attempts
            total_successes += skill.total_successes
        
        return {
            "total_skills": total_skills,
            "by_level": by_level,
            "total_attempts": total_attempts,
            "total_successes": total_successes,
            "overall_success_rate": (
                total_successes / total_attempts if total_attempts > 0 else 0
            ),
            "due_for_review": len(self.get_due_skills())
        }
    
    def reset(self) -> None:
        """Reset all skills."""
        self.skills = {}
        self._save()


if __name__ == "__main__":
    tracker = SkillTracker()
    
    # Test recording practice
    skill = tracker.record_practice(
        name="list_comprehension",
        category="idioms",
        score=85,
        success=True
    )
    
    print(f"Skill: {skill.name}")
    print(f"Level: {MasteryLevel.NAMES.get(skill.level)}")
    print(f"Next review: {skill.next_review}")
    
    # Test progress
    progress = tracker.get_progress()
    print(f"Progress: {json.dumps(progress, indent=2)}")