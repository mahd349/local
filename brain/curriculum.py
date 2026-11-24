"""Curriculum system with 4 levels: Beginner → Intermediate → Advanced → Master."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from agent.config import STATE_DIR


CURRICULUM_DB = STATE_DIR / "curriculum.json"


class Level:
    """Represents a curriculum level."""
    BEGINNER = "beginner"
    INTERMEDIATE = "intermediate"
    ADVANCED = "advanced"
    MASTER = "master"
    
    ALL = [BEGINNER, INTERMEDIATE, ADVANCED, MASTER]


# Default curriculum structure
DEFAULT_CURRICULUM = {
    Level.BEGINNER: {
        "required_score": 0,
        "topics": [
            {"id": "variables", "name": "Variables and Data Types", "difficulty": 1},
            {"id": "strings", "name": "String Operations", "difficulty": 1},
            {"id": "lists", "name": "Lists and Tuples", "difficulty": 2},
            {"id": "dicts", "name": "Dictionaries and Sets", "difficulty": 2},
            {"id": "conditionals", "name": "Conditionals", "difficulty": 1},
            {"id": "loops", "name": "Loops", "difficulty": 2},
            {"id": "functions", "name": "Functions", "difficulty": 2},
            {"id": "comprehensions", "name": "List Comprehensions", "difficulty": 3},
        ],
        "unlock_requirements": {
            "min_exercises": 10,
            "min_score": 70
        }
    },
    
    Level.INTERMEDIATE: {
        "required_score": 70,
        "topics": [
            {"id": "args_kwargs", "name": "*args and **kwargs", "difficulty": 3},
            {"id": "decorators", "name": "Decorators", "difficulty": 4},
            {"id": "closures", "name": "Closures", "difficulty": 4},
            {"id": "generators", "name": "Generators", "difficulty": 4},
            {"id": "context_managers", "name": "Context Managers", "difficulty": 3},
            {"id": "exceptions", "name": "Exception Handling", "difficulty": 3},
            {"id": "classes", "name": "Classes and OOP", "difficulty": 4},
            {"id": "magic_methods", "name": "Magic Methods", "difficulty": 4},
        ],
        "unlock_requirements": {
            "min_exercises": 15,
            "min_score": 80
        }
    },
    
    Level.ADVANCED: {
        "required_score": 80,
        "topics": [
            {"id": "async_await", "name": "Async/Await", "difficulty": 5},
            {"id": "asyncio", "name": "asyncio Library", "difficulty": 5},
            {"id": "typing", "name": "Type Hints", "difficulty": 3},
            {"id": "dataclasses", "name": "Dataclasses", "difficulty": 3},
            {"id": "metaclass", "name": "Metaclasses", "difficulty": 6},
            {"id": "descriptors", "name": "Descriptors", "difficulty": 5},
            {"id": "itertools", "name": "itertools", "difficulty": 4},
            {"id": "functools", "name": "functools", "difficulty": 4},
        ],
        "unlock_requirements": {
            "min_exercises": 20,
            "min_score": 85
        }
    },
    
    Level.MASTER: {
        "required_score": 85,
        "topics": [
            {"id": "design_patterns", "name": "Design Patterns", "difficulty": 7},
            {"id": "algorithms", "name": "Algorithms", "difficulty": 6},
            {"id": "concurrency", "name": "Concurrency Patterns", "difficulty": 6},
            {"id": "memory_management", "name": "Memory Management", "difficulty": 5},
            {"id": "testing", "name": "Advanced Testing", "difficulty": 4},
            {"id": "profiling", "name": "Performance Profiling", "difficulty": 5},
            {"id": "security", "name": "Security Best Practices", "difficulty": 5},
            {"id": "architecture", "name": "Software Architecture", "difficulty": 7},
        ],
        "unlock_requirements": {
            "min_exercises": 30,
            "min_score": 90
        }
    }
}


class Curriculum:
    """Manages the learning curriculum."""
    
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or CURRICULUM_DB
        self.data = self._load()
    
    def _load(self) -> dict:
        """Load curriculum data from disk."""
        if self.db_path.exists():
            try:
                return json.loads(self.db_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        
        # Initialize with default curriculum
        data = {
            "curriculum": DEFAULT_CURRICULUM,
            "progress": {},
            "scores": {},
            "unlocked_levels": [Level.BEGINNER]
        }
        self._save(data)
        return data
    
    def _save(self, data: dict | None = None) -> None:
        """Save curriculum data to disk."""
        data = data or self.data
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    
    def get_current_level(self) -> str:
        """Get the current level based on progress."""
        unlocked = self.data.get("unlocked_levels", [Level.BEGINNER])
        if not unlocked:
            return Level.BEGINNER
        
        # Return the highest unlocked level
        for level in reversed(Level.ALL):
            if level in unlocked:
                return level
        
        return Level.BEGINNER
    
    def get_topics_for_level(self, level: str) -> list[dict]:
        """Get topics for a specific level."""
        curriculum = self.data.get("curriculum", DEFAULT_CURRICULUM)
        return curriculum.get(level, {}).get("topics", [])
    
    def get_next_topic(self) -> dict | None:
        """Get the next topic to study."""
        current_level = self.get_current_level()
        topics = self.get_topics_for_level(current_level)
        progress = self.data.get("progress", {})
        
        # Find first incomplete topic
        for topic in topics:
            topic_id = topic["id"]
            if topic_id not in progress or not progress[topic_id].get("completed"):
                return topic
        
        # All topics in current level completed, check if can unlock next level
        next_level = self._get_next_level(current_level)
        if next_level and self._can_unlock_level(next_level):
            # Unlock the next level
            self._unlock_level(next_level)
            # Return first topic of next level
            next_topics = self.get_topics_for_level(next_level)
            return next_topics[0] if next_topics else None
        
        return None
    
    def _get_next_level(self, current_level: str) -> str | None:
        """Get the next level after current."""
        try:
            idx = Level.ALL.index(current_level)
            if idx < len(Level.ALL) - 1:
                return Level.ALL[idx + 1]
        except ValueError:
            pass
        return None
    
    def _can_unlock_level(self, level: str) -> bool:
        """Check if a level can be unlocked."""
        curriculum = self.data.get("curriculum", DEFAULT_CURRICULUM)
        requirements = curriculum.get(level, {}).get("unlock_requirements", {})
        
        progress = self.data.get("progress", {})
        scores = self.data.get("scores", {})
        
        # Check minimum exercises
        min_exercises = requirements.get("min_exercises", 0)
        completed_count = sum(1 for p in progress.values() if p.get("completed"))
        if completed_count < min_exercises:
            return False
        
        # Check minimum score
        min_score = requirements.get("min_score", 0)
        if scores:
            avg_score = sum(scores.values()) / len(scores)
            if avg_score < min_score:
                return False
        
        return True
    
    def _unlock_level(self, level: str) -> None:
        """Unlock a level."""
        unlocked = self.data.setdefault("unlocked_levels", [])
        if level not in unlocked:
            unlocked.append(level)
            self._save()
    
    def record_topic_completion(
        self,
        topic_id: str,
        exercises_completed: int,
        score: float,
        time_spent: float
    ) -> None:
        """Record completion of a topic."""
        progress = self.data.setdefault("progress", {})
        scores = self.data.setdefault("scores", {})
        
        progress[topic_id] = {
            "completed": True,
            "exercises_completed": exercises_completed,
            "score": score,
            "time_spent": time_spent,
            "timestamp": time.time()
        }
        
        scores[topic_id] = score
        self._save()
    
    def get_progress(self) -> dict:
        """Get overall progress."""
        progress = self.data.get("progress", {})
        scores = self.data.get("scores", {})
        
        total_topics = sum(
            len(self.get_topics_for_level(level))
            for level in Level.ALL
        )
        
        completed_topics = sum(1 for p in progress.values() if p.get("completed"))
        
        return {
            "current_level": self.get_current_level(),
            "unlocked_levels": self.data.get("unlocked_levels", []),
            "total_topics": total_topics,
            "completed_topics": completed_topics,
            "completion_rate": completed_topics / total_topics if total_topics > 0 else 0,
            "average_score": sum(scores.values()) / len(scores) if scores else 0,
            "progress": progress
        }
    
    def reset(self) -> None:
        """Reset all progress."""
        self.data = {
            "curriculum": DEFAULT_CURRICULUM,
            "progress": {},
            "scores": {},
            "unlocked_levels": [Level.BEGINNER]
        }
        self._save()


import time  # Import at module level


if __name__ == '__main__':
    # Test the curriculum system
    curriculum = Curriculum()
    
    print("Current level:", curriculum.get_current_level())
    print("Next topic:", curriculum.get_next_topic())
    print("Progress:", curriculum.get_progress())