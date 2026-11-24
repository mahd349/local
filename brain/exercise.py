"""Exercise bank and management system."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from agent.config import STATE_DIR
from .curriculum import Level


EXERCISE_DB = STATE_DIR / "exercises.json"


# Exercise bank with pre-defined exercises
EXERCISE_BANK = {
    # BEGINNER LEVEL
    "variables": [
        {
            "id": "var_1",
            "title": "Variable Assignment",
            "description": "Create a function that takes two numbers and returns their sum.",
            "difficulty": 1,
            "starter_code": "def add(a, b):\n    # Your code here\n    pass",
            "solution": "def add(a, b):\n    return a + b",
            "test_cases": [
                {"input": [2, 3], "expected": 5},
                {"input": [-1, 1], "expected": 0},
                {"input": [0, 0], "expected": 0}
            ],
            "hints": ["Use the + operator to add two numbers."]
        },
        {
            "id": "var_2",
            "title": "String Length",
            "description": "Write a function that returns the length of a string.",
            "difficulty": 1,
            "starter_code": "def string_length(s):\n    # Your code here\n    pass",
            "solution": "def string_length(s):\n    return len(s)",
            "test_cases": [
                {"input": ["hello"], "expected": 5},
                {"input": [""], "expected": 0},
                {"input": ["python"], "expected": 6}
            ],
            "hints": ["Use the len() function."]
        }
    ],
    
    "lists": [
        {
            "id": "list_1",
            "title": "List Sum",
            "description": "Write a function that returns the sum of all numbers in a list.",
            "difficulty": 2,
            "starter_code": "def list_sum(numbers):\n    # Your code here\n    pass",
            "solution": "def list_sum(numbers):\n    return sum(numbers)",
            "test_cases": [
                {"input": [[1, 2, 3]], "expected": 6},
                {"input": [[10, 20, 30]], "expected": 60},
                {"input": [[]], "expected": 0}
            ],
            "hints": ["You can use the built-in sum() function or a loop."]
        },
        {
            "id": "list_2",
            "title": "List Filter",
            "description": "Write a function that returns only even numbers from a list.",
            "difficulty": 2,
            "starter_code": "def filter_evens(numbers):\n    # Your code here\n    pass",
            "solution": "def filter_evens(numbers):\n    return [n for n in numbers if n % 2 == 0]",
            "test_cases": [
                {"input": [[1, 2, 3, 4, 5, 6]], "expected": [2, 4, 6]},
                {"input": [[1, 3, 5]], "expected": []},
                {"input": [[2, 4, 6]], "expected": [2, 4, 6]}
            ],
            "hints": ["Use list comprehension with a condition."]
        }
    ],
    
    "functions": [
        {
            "id": "func_1",
            "title": "Default Arguments",
            "description": "Write a function greet(name, greeting='Hello') that returns '{greeting}, {name}!'",
            "difficulty": 2,
            "starter_code": "def greet(name, greeting='Hello'):\n    # Your code here\n    pass",
            "solution": "def greet(name, greeting='Hello'):\n    return f'{greeting}, {name}!'",
            "test_cases": [
                {"input": ["Alice"], "expected": "Hello, Alice!"},
                {"input": ["Bob", "Hi"], "expected": "Hi, Bob!"}
            ],
            "hints": ["Use f-strings for string formatting."]
        }
    ],
    
    # INTERMEDIATE LEVEL
    "decorators": [
        {
            "id": "dec_1",
            "title": "Simple Decorator",
            "description": "Create a decorator that prints 'Before' before function call and 'After' after.",
            "difficulty": 4,
            "starter_code": "def log_decorator(func):\n    # Your code here\n    pass",
            "solution": """def log_decorator(func):
    def wrapper(*args, **kwargs):
        print('Before')
        result = func(*args, **kwargs)
        print('After')
        return result
    return wrapper""",
            "test_cases": [],
            "hints": ["Use a wrapper function inside the decorator."]
        }
    ],
    
    "generators": [
        {
            "id": "gen_1",
            "title": "Fibonacci Generator",
            "description": "Create a generator that yields Fibonacci numbers up to n.",
            "difficulty": 4,
            "starter_code": "def fibonacci(n):\n    # Your code here\n    pass",
            "solution": """def fibonacci(n):
    a, b = 0, 1
    while a <= n:
        yield a
        a, b = b, a + b""",
            "test_cases": [
                {"input": [10], "expected": [0, 1, 1, 2, 3, 5, 8]}
            ],
            "hints": ["Use yield instead of return."]
        }
    ],
    
    # ADVANCED LEVEL
    "async_await": [
        {
            "id": "async_1",
            "title": "Async Sleep",
            "description": "Create an async function that sleeps for n seconds and returns 'done'.",
            "difficulty": 5,
            "starter_code": """import asyncio

async def async_sleep(n):
    # Your code here
    pass""",
            "solution": """import asyncio

async def async_sleep(n):
    await asyncio.sleep(n)
    return 'done'""",
            "test_cases": [],
            "hints": ["Use await asyncio.sleep(n)"]
        }
    ],
    
    # MASTER LEVEL
    "design_patterns": [
        {
            "id": "pattern_1",
            "title": "Singleton Pattern",
            "description": "Implement the Singleton design pattern.",
            "difficulty": 7,
            "starter_code": "class Singleton:\n    # Your code here\n    pass",
            "solution": """class Singleton:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance""",
            "test_cases": [],
            "hints": ["Override __new__ method."]
        }
    ]
}


class ExerciseBank:
    """Manages the exercise bank."""
    
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or EXERCISE_DB
        self.data = self._load()
    
    def _load(self) -> dict:
        """Load exercise data from disk."""
        if self.db_path.exists():
            try:
                return json.loads(self.db_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        
        # Initialize with default exercises
        data = {
            "exercises": EXERCISE_BANK,
            "completed": {},
            "attempts": {}
        }
        self._save(data)
        return data
    
    def _save(self, data: dict | None = None) -> None:
        """Save exercise data to disk."""
        data = data or self.data
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    
    def get_exercise(self, exercise_id: str) -> dict | None:
        """Get an exercise by ID."""
        exercises = self.data.get("exercises", {})
        
        # Search through all topics
        for topic_exercises in exercises.values():
            for exercise in topic_exercises:
                if exercise["id"] == exercise_id:
                    return exercise
        
        return None
    
    def get_exercises_for_topic(self, topic_id: str) -> list[dict]:
        """Get all exercises for a topic."""
        exercises = self.data.get("exercises", {})
        return exercises.get(topic_id, [])
    
    def get_next_exercise(self, topic_id: str) -> dict | None:
        """Get the next incomplete exercise for a topic."""
        exercises = self.get_exercises_for_topic(topic_id)
        completed = self.data.get("completed", {})
        
        for exercise in exercises:
            if exercise["id"] not in completed:
                return exercise
        
        return None
    
    def record_attempt(
        self,
        exercise_id: str,
        code: str,
        success: bool,
        time_spent: float
    ) -> None:
        """Record an attempt at an exercise."""
        attempts = self.data.setdefault("attempts", {})
        
        if exercise_id not in attempts:
            attempts[exercise_id] = []
        
        attempts[exercise_id].append({
            "code": code,
            "success": success,
            "time_spent": time_spent,
            "timestamp": time.time()
        })
        
        # If successful, mark as completed
        if success:
            completed = self.data.setdefault("completed", {})
            completed[exercise_id] = {
                "timestamp": time.time(),
                "attempts": len(attempts[exercise_id]),
                "total_time": sum(a["time_spent"] for a in attempts[exercise_id])
            }
        
        self._save()
    
    def get_progress(self, topic_id: str) -> dict:
        """Get progress for a topic."""
        exercises = self.get_exercises_for_topic(topic_id)
        completed = self.data.get("completed", {})
        
        total = len(exercises)
        done = sum(1 for e in exercises if e["id"] in completed)
        
        return {
            "total_exercises": total,
            "completed_exercises": done,
            "completion_rate": done / total if total > 0 else 0,
            "exercises": exercises
        }
    
    def add_exercise(self, topic_id: str, exercise: dict) -> None:
        """Add a new exercise to the bank."""
        exercises = self.data.setdefault("exercises", {})
        
        if topic_id not in exercises:
            exercises[topic_id] = []
        
        exercises[topic_id].append(exercise)
        self._save()
    
    def reset_topic(self, topic_id: str) -> None:
        """Reset progress for a topic."""
        completed = self.data.get("completed", {})
        attempts = self.data.get("attempts", {})
        
        exercises = self.get_exercises_for_topic(topic_id)
        
        for exercise in exercises:
            exercise_id = exercise["id"]
            completed.pop(exercise_id, None)
            attempts.pop(exercise_id, None)
        
        self._save()


if __name__ == '__main__':
    # Test the exercise bank
    bank = ExerciseBank()
    
    print("Exercises for 'variables':", bank.get_exercises_for_topic("variables"))
    print("Next exercise for 'variables':", bank.get_next_exercise("variables"))
    print("Progress for 'variables':", bank.get_progress("variables"))