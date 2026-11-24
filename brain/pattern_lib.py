"""Pattern library: idioms, design patterns, algorithms, data structures."""
from __future__ import annotations

import json
import time
from pathlib import Path
from typing import Any
from agent.config import STATE_DIR, assert_writable, get_logger

log = get_logger("pattern_lib")

PATTERN_DB = STATE_DIR / "patterns.json"


# Pattern library with categorized patterns
PATTERN_LIBRARY = {
    "idioms": {
        "list_comprehension": {
            "name": "List Comprehension",
            "description": "Create lists with concise syntax",
            "level": "beginner",
            "examples": [
                {
                    "code": "squares = [x**2 for x in range(10)]",
                    "description": "Square numbers from 0 to 9"
                },
                {
                    "code": "evens = [x for x in range(20) if x % 2 == 0]",
                    "description": "Filter even numbers"
                },
                {
                    "code": "pairs = [(x, y) for x in range(3) for y in range(3)]",
                    "description": "Cartesian product"
                }
            ],
            "related": ["generator_expression", "dict_comprehension"]
        },
        
        "unpacking": {
            "name": "Unpacking",
            "description": "Extract values from iterables",
            "level": "beginner",
            "examples": [
                {
                    "code": "a, b, c = [1, 2, 3]",
                    "description": "Basic unpacking"
                },
                {
                    "code": "first, *rest = [1, 2, 3, 4, 5]",
                    "description": "Extended unpacking"
                },
                {
                    "code": "a, b = b, a",
                    "description": "Swap variables"
                }
            ],
            "related": ["star_expression", "zip"]
        },
        
        "context_manager": {
            "name": "Context Manager",
            "description": "Resource management with 'with' statement",
            "level": "intermediate",
            "examples": [
                {
                    "code": """with open('file.txt', 'r') as f:
    content = f.read()""",
                    "description": "File handling with automatic cleanup"
                },
                {
                    "code": """from contextlib import contextmanager

@contextmanager
def managed_resource():
    resource = acquire()
    try:
        yield resource
    finally:
        release(resource)""",
                    "description": "Custom context manager"
                }
            ],
            "related": ["decorator", "generator"]
        }
    },
    
    "design_patterns": {
        "singleton": {
            "name": "Singleton",
            "description": "Ensure a class has only one instance",
            "level": "master",
            "examples": [
                {
                    "code": """class Singleton:
    _instance = None
    
    def __new__(cls):
        if cls._instance is None:
            cls._instance = super().__new__(cls)
        return cls._instance""",
                    "description": "Basic singleton implementation"
                },
                {
                    "code": """def singleton(cls):
    instances = {}
    def get_instance(*args, **kwargs):
        if cls not in instances:
            instances[cls] = cls(*args, **kwargs)
        return instances[cls]
    return get_instance

@singleton
class MyClass:
    pass""",
                    "description": "Singleton as decorator"
                }
            ],
            "related": ["factory", "builder"]
        },
        
        "factory": {
            "name": "Factory",
            "description": "Create objects without specifying exact class",
            "level": "master",
            "examples": [
                {
                    "code": """class Dog:
    def speak(self):
        return "Woof!"

class Cat:
    def speak(self):
        return "Meow!"

def animal_factory(animal_type):
    animals = {"dog": Dog, "cat": Cat}
    return animals.get(animal_type, Dog)()""",
                    "description": "Simple factory function"
                },
                {
                    "code": """from abc import ABC, abstractmethod

class Animal(ABC):
    @abstractmethod
    def speak(self):
        pass

class Dog(Animal):
    def speak(self):
        return "Woof!"

class AnimalFactory:
    @staticmethod
    def create(animal_type: str) -> Animal:
        if animal_type == "dog":
            return Dog()
        raise ValueError(f"Unknown animal: {animal_type}")""",
                    "description": "Abstract factory with type hints"
                }
            ],
            "related": ["singleton", "builder", "strategy"]
        },
        
        "observer": {
            "name": "Observer",
            "description": "Notify dependents of state changes",
            "level": "master",
            "examples": [
                {
                    "code": """class Subject:
    def __init__(self):
        self._observers = []
    
    def attach(self, observer):
        self._observers.append(observer)
    
    def detach(self, observer):
        self._observers.remove(observer)
    
    def notify(self, message):
        for observer in self._observers:
            observer.update(message)

class Observer:
    def update(self, message):
        print(f"Received: {message}")""",
                    "description": "Basic observer pattern"
                }
            ],
            "related": ["event_system", "pub_sub"]
        },
        
        "strategy": {
            "name": "Strategy",
            "description": "Define a family of algorithms, interchangeable",
            "level": "master",
            "examples": [
                {
                    "code": """from typing import Protocol

class SortStrategy(Protocol):
    def sort(self, data: list) -> list:
        ...

class BubbleSort:
    def sort(self, data: list) -> list:
        # Implementation
        return sorted(data)

class QuickSort:
    def sort(self, data: list) -> list:
        # Implementation
        return sorted(data)

class Sorter:
    def __init__(self, strategy: SortStrategy):
        self._strategy = strategy
    
    def sort(self, data: list) -> list:
        return self._strategy.sort(data)""",
                    "description": "Strategy pattern with Protocol"
                }
            ],
            "related": ["command", "state"]
        }
    },
    
    "algorithms": {
        "binary_search": {
            "name": "Binary Search",
            "description": "Efficient search in sorted array",
            "level": "advanced",
            "examples": [
                {
                    "code": """def binary_search(arr, target):
    left, right = 0, len(arr) - 1
    while left <= right:
        mid = (left + right) // 2
        if arr[mid] == target:
            return mid
        elif arr[mid] < target:
            left = mid + 1
        else:
            right = mid - 1
    return -1""",
                    "description": "Iterative binary search"
                },
                {
                    "code": """def binary_search(arr, target, left=0, right=None):
    if right is None:
        right = len(arr) - 1
    if left > right:
        return -1
    mid = (left + right) // 2
    if arr[mid] == target:
        return mid
    elif arr[mid] < target:
        return binary_search(arr, target, mid + 1, right)
    else:
        return binary_search(arr, target, left, mid - 1)""",
                    "description": "Recursive binary search"
                }
            ],
            "related": ["divide_and_conquer", "recursion"]
        },
        
        "memoization": {
            "name": "Memoization",
            "description": "Cache results of expensive function calls",
            "level": "advanced",
            "examples": [
                {
                    "code": """from functools import lru_cache

@lru_cache(maxsize=None)
def fibonacci(n):
    if n < 2:
        return n
    return fibonacci(n - 1) + fibonacci(n - 2)""",
                    "description": "Using lru_cache decorator"
                },
                {
                    "code": """def memoize(func):
    cache = {}
    def wrapper(*args):
        if args not in cache:
            cache[args] = func(*args)
        return cache[args]
    return wrapper

@memoize
def fibonacci(n):
    if n < 2:
        return n
    return fibonacci(n - 1) + fibonacci(n - 2)""",
                    "description": "Custom memoization decorator"
                }
            ],
            "related": ["dynamic_programming", "decorator"]
        },
        
        "divide_and_conquer": {
            "name": "Divide and Conquer",
            "description": "Break problem into smaller subproblems",
            "level": "advanced",
            "examples": [
                {
                    "code": """def merge_sort(arr):
    if len(arr) <= 1:
        return arr
    mid = len(arr) // 2
    left = merge_sort(arr[:mid])
    right = merge_sort(arr[mid:])
    return merge(left, right)

def merge(left, right):
    result = []
    i = j = 0
    while i < len(left) and j < len(right):
        if left[i] <= right[j]:
            result.append(left[i])
            i += 1
        else:
            result.append(right[j])
            j += 1
    result.extend(left[i:])
    result.extend(right[j:])
    return result""",
                    "description": "Merge sort implementation"
                }
            ],
            "related": ["recursion", "binary_search"]
        }
    },
    
    "data_structures": {
        "linked_list": {
            "name": "Linked List",
            "description": "Dynamic sequence of nodes",
            "level": "intermediate",
            "examples": [
                {
                    "code": """class Node:
    def __init__(self, value):
        self.value = value
        self.next = None

class LinkedList:
    def __init__(self):
        self.head = None
    
    def append(self, value):
        if not self.head:
            self.head = Node(value)
            return
        current = self.head
        while current.next:
            current = current.next
        current.next = Node(value)""",
                    "description": "Basic singly linked list"
                }
            ],
            "related": ["stack", "queue"]
        },
        
        "stack": {
            "name": "Stack",
            "description": "LIFO data structure",
            "level": "intermediate",
            "examples": [
                {
                    "code": """class Stack:
    def __init__(self):
        self._items = []
    
    def push(self, item):
        self._items.append(item)
    
    def pop(self):
        if self.is_empty():
            raise IndexError("Stack is empty")
        return self._items.pop()
    
    def peek(self):
        if self.is_empty():
            raise IndexError("Stack is empty")
        return self._items[-1]
    
    def is_empty(self):
        return len(self._items) == 0""",
                    "description": "Stack using list"
                }
            ],
            "related": ["queue", "linked_list"]
        },
        
        "queue": {
            "name": "Queue",
            "description": "FIFO data structure",
            "level": "intermediate",
            "examples": [
                {
                    "code": """from collections import deque

class Queue:
    def __init__(self):
        self._items = deque()
    
    def enqueue(self, item):
        self._items.append(item)
    
    def dequeue(self):
        if self.is_empty():
            raise IndexError("Queue is empty")
        return self._items.popleft()
    
    def is_empty(self):
        return len(self._items) == 0""",
                    "description": "Queue using deque"
                }
            ],
            "related": ["stack", "priority_queue"]
        }
    }
}


class PatternLibrary:
    """Manages the pattern library."""
    
    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or PATTERN_DB
        self.data = self._load()
    
    def _load(self) -> dict:
        """Load pattern library from disk."""
        if self.db_path.exists():
            try:
                return json.loads(self.db_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        
        # Initialize with default patterns
        data = {
            "patterns": PATTERN_LIBRARY,
            "learned": {},
            "usage_count": {}
        }
        self._save(data)
        return data
    
    def _save(self, data: dict | None = None) -> None:
        """Save pattern library to disk."""
        data = data or self.data
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8"
        )
    
    def get_pattern(self, category: str, pattern_id: str) -> dict | None:
        """Get a specific pattern."""
        patterns = self.data.get("patterns", {})
        return patterns.get(category, {}).get(pattern_id)
    
    def get_patterns_by_level(self, level: str) -> list[dict]:
        """Get all patterns for a specific level."""
        patterns = self.data.get("patterns", {})
        result = []
        
        for category, category_patterns in patterns.items():
            for pattern_id, pattern in category_patterns.items():
                if pattern.get("level") == level:
                    result.append({
                        "category": category,
                        "id": pattern_id,
                        **pattern
                    })
        
        return result
    
    def search_patterns(self, query: str) -> list[dict]:
        """Search patterns by name or description."""
        patterns = self.data.get("patterns", {})
        results = []
        query_lower = query.lower()
        
        for category, category_patterns in patterns.items():
            for pattern_id, pattern in category_patterns.items():
                name = pattern.get("name", "").lower()
                description = pattern.get("description", "").lower()
                
                if query_lower in name or query_lower in description:
                    results.append({
                        "category": category,
                        "id": pattern_id,
                        **pattern
                    })
        
        return results
    
    def get_related_patterns(self, category: str, pattern_id: str) -> list[dict]:
        """Get patterns related to a specific pattern."""
        pattern = self.get_pattern(category, pattern_id)
        if not pattern:
            return []
        
        related = pattern.get("related", [])
        results = []
        
        for related_id in related:
            # Search in all categories
            for cat, patterns in self.data.get("patterns", {}).items():
                if related_id in patterns:
                    results.append({
                        "category": cat,
                        "id": related_id,
                        **patterns[related_id]
                    })
        
        return results
    
    def record_pattern_usage(self, category: str, pattern_id: str) -> None:
        """Record when a pattern is used."""
        usage = self.data.setdefault("usage_count", {})
        key = f"{category}:{pattern_id}"
        usage[key] = usage.get(key, 0) + 1
        self._save()
    
    def record_pattern_learned(self, category: str, pattern_id: str) -> None:
        """Record when a pattern is learned."""
        learned = self.data.setdefault("learned", {})
        key = f"{category}:{pattern_id}"
        learned[key] = {
            "timestamp": time.time(),
            "usage_count": 0
        }
        self._save()
    
    def get_learning_progress(self) -> dict:
        """Get pattern learning progress."""
        patterns = self.data.get("patterns", {})
        learned = self.data.get("learned", {})
        
        total = 0
        learned_count = 0
        by_category = {}
        
        for category, category_patterns in patterns.items():
            total += len(category_patterns)
            cat_learned = 0
            
            for pattern_id in category_patterns:
                key = f"{category}:{pattern_id}"
                if key in learned:
                    learned_count += 1
                    cat_learned += 1
            
            by_category[category] = {
                "total": len(category_patterns),
                "learned": cat_learned,
                "progress": cat_learned / len(category_patterns) if category_patterns else 0
            }
        
        return {
            "total_patterns": total,
            "learned_patterns": learned_count,
            "progress": learned_count / total if total > 0 else 0,
            "by_category": by_category
        }
    
    def add_pattern(self, category: str, pattern_id: str, pattern: dict) -> None:
        """Add a new pattern to the library."""
        patterns = self.data.setdefault("patterns", {})
        
        if category not in patterns:
            patterns[category] = {}
        
        patterns[category][pattern_id] = pattern
        self._save()


if __name__ == '__main__':
    # Test the pattern library
    lib = PatternLibrary()
    
    print("Beginner patterns:", len(lib.get_patterns_by_level("beginner")))
    print("Master patterns:", len(lib.get_patterns_by_level("master")))
    
    # Search for patterns
    results = lib.search_patterns("singleton")
    print(f"Search 'singleton': {len(results)} results")
    
    # Get related patterns
    related = lib.get_related_patterns("design_patterns", "singleton")
    print(f"Related to singleton: {len(related)}")
    
    # Learning progress
    progress = lib.get_learning_progress()
    print(f"Learning progress: {progress['progress']:.1%}")