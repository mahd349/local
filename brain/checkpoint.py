"""Checkpoint system: save and restore progress across sessions.

When the laptop shuts down or the process is interrupted,
the system remembers where it left off and continues from there.
"""
from __future__ import annotations

import json
import time
from pathlib import Path
from agent.config import STATE_DIR, assert_writable, get_logger

log = get_logger("checkpoint")

CHECKPOINT_DB = STATE_DIR / "checkpoints.json"


class Checkpoint:
    """Manages progress checkpoints for resumable operations."""

    def __init__(self, db_path: Path | None = None):
        self.db_path = db_path or CHECKPOINT_DB
        self.data = self._load()

    def _load(self) -> dict:
        """Load checkpoints from disk."""
        if self.db_path.exists():
            try:
                return json.loads(self.db_path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        return {}

    def _save(self) -> None:
        """Save checkpoints to disk."""
        self.db_path.parent.mkdir(parents=True, exist_ok=True)
        self.db_path.write_text(
            json.dumps(self.data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def get(self, key: str) -> dict | None:
        """Get a checkpoint by key."""
        return self.data.get(key)

    def set(self, key: str, value: dict) -> None:
        """Set a checkpoint."""
        value["_updated_at"] = time.time()
        self.data[key] = value
        self._save()

    def delete(self, key: str) -> None:
        """Delete a checkpoint."""
        self.data.pop(key, None)
        self._save()

    def clear_all(self) -> None:
        """Clear all checkpoints."""
        self.data = {}
        self._save()

    def list_all(self) -> dict:
        """List all checkpoints."""
        return dict(self.data)


class IngestCheckpoint:
    """Checkpoint for file scanning operations."""

    KEY = "ingest_scan"

    def __init__(self, checkpoint: Checkpoint):
        self.cp = checkpoint

    def get_last_scanned(self) -> dict | None:
        """Get the last scanned file info."""
        return self.cp.get(self.KEY)

    def save_progress(
        self,
        current_file: str,
        file_index: int,
        total_files: int,
        roots: list[str],
        stats: dict | None = None,
    ) -> None:
        """Save scan progress."""
        self.cp.set(self.KEY, {
            "current_file": current_file,
            "file_index": file_index,
            "total_files": total_files,
            "roots": roots,
            "stats": stats or {},
        })

    def clear(self) -> None:
        """Clear scan checkpoint."""
        self.cp.delete(self.KEY)


class LessonCheckpoint:
    """Checkpoint for lesson operations."""

    KEY = "lesson_progress"

    def __init__(self, checkpoint: Checkpoint):
        self.cp = checkpoint

    def get_last_lesson(self) -> dict | None:
        """Get the last lesson info."""
        return self.cp.get(self.KEY)

    def save_progress(
        self,
        topic_id: str,
        topic_name: str,
        level: str,
        examples_verified: int = 0,
        examples_total: int = 0,
    ) -> None:
        """Save lesson progress."""
        self.cp.set(self.KEY, {
            "topic_id": topic_id,
            "topic_name": topic_name,
            "level": level,
            "examples_verified": examples_verified,
            "examples_total": examples_total,
        })

    def clear(self) -> None:
        """Clear lesson checkpoint."""
        self.cp.delete(self.KEY)


class PracticeCheckpoint:
    """Checkpoint for practice operations."""

    KEY = "practice_progress"

    def __init__(self, checkpoint: Checkpoint):
        self.cp = checkpoint

    def get_last_exercise(self) -> dict | None:
        """Get the last exercise info."""
        return self.cp.get(self.KEY)

    def save_progress(
        self,
        exercise_id: str,
        topic_id: str,
        level: str,
        success: bool,
        score: float,
    ) -> None:
        """Save practice progress."""
        self.cp.set(self.KEY, {
            "exercise_id": exercise_id,
            "topic_id": topic_id,
            "level": level,
            "success": success,
            "score": score,
        })

    def clear(self) -> None:
        """Clear practice checkpoint."""
        self.cp.delete(self.KEY)


class InboxCheckpoint:
    """Checkpoint for AI inbox processing."""

    KEY = "inbox_progress"

    def __init__(self, checkpoint: Checkpoint):
        self.cp = checkpoint

    def get_processed_files(self) -> set[str]:
        """Get set of already processed file names."""
        data = self.cp.get(self.KEY)
        if not data:
            return set()
        return set(data.get("processed_files", []))

    def mark_processed(self, filename: str) -> None:
        """Mark a file as processed."""
        data = self.cp.get(self.KEY) or {"processed_files": []}
        if filename not in data["processed_files"]:
            data["processed_files"].append(filename)
        self.cp.set(self.KEY, data)

    def clear(self) -> None:
        """Clear inbox checkpoint."""
        self.cp.delete(self.KEY)


if __name__ == "__main__":
    # Test the checkpoint system
    cp = Checkpoint()

    # Test ingest checkpoint
    ingest_cp = IngestCheckpoint(cp)
    ingest_cp.save_progress(
        current_file=r"D:\Python\agent\learn.py",
        file_index=42,
        total_files=100,
        roots=[r"D:\Python"],
    )
    print("Ingest checkpoint:", ingest_cp.get_last_scanned())

    # Test lesson checkpoint
    lesson_cp = LessonCheckpoint(cp)
    lesson_cp.save_progress(
        topic_id="strings",
        topic_name="String Operations",
        level="beginner",
        examples_verified=3,
        examples_total=5,
    )
    print("Lesson checkpoint:", lesson_cp.get_last_lesson())

    # Test practice checkpoint
    practice_cp = PracticeCheckpoint(cp)
    practice_cp.save_progress(
        exercise_id="str_1",
        topic_id="strings",
        level="beginner",
        success=True,
        score=85.0,
    )
    print("Practice checkpoint:", practice_cp.get_last_exercise())

    # List all
    print("All checkpoints:", cp.list_all())