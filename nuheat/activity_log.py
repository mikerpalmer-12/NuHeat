"""Activity log with in-memory ring buffer and buffered JSONL persistence.

Normal mode: entries accumulate in memory and flush to disk every N minutes.
Debug mode: every entry is written to disk immediately (more SD wear, but
            captures everything up to a crash).
"""

import json
import os
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path


@dataclass
class LogEntry:
    timestamp: str
    epoch: float
    category: str  # auth, poll, write, rate_limit, refresh, error, settings
    message: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "category": self.category,
            "message": self.message,
            "details": self.details,
        }

    def to_json(self) -> str:
        return json.dumps(self.to_dict(), default=str)


DEFAULT_LOG_DIR = "/app/logs"
DEFAULT_FLUSH_INTERVAL = 300  # 5 minutes
MAX_LOG_SIZE = 1_048_576  # 1MB
MAX_BACKUPS = 1


class ActivityLog:
    """Thread-safe ring buffer with buffered disk persistence.

    In normal mode, entries buffer in memory and flush to a JSONL file
    periodically. In debug mode, every entry writes to disk immediately.
    """

    def __init__(self, max_entries: int = 500):
        self._entries: deque[LogEntry] = deque(maxlen=max_entries)
        self._unflushed: list[LogEntry] = []
        self._lock = threading.Lock()

        self._log_dir = Path(os.environ.get("NUHEAT_LOG_DIR", DEFAULT_LOG_DIR))
        self._flush_interval = int(os.environ.get("NUHEAT_LOG_FLUSH_INTERVAL", DEFAULT_FLUSH_INTERVAL))
        self._debug_mode = os.environ.get("NUHEAT_DEBUG_LOG", "").lower() in ("1", "true", "yes")
        self._last_flush_time: float = 0

        self._log_dir.mkdir(parents=True, exist_ok=True)
        self._log_file = self._log_dir / "activity.jsonl"

        self._restore_from_disk()

    @property
    def debug_mode(self) -> bool:
        return self._debug_mode

    @debug_mode.setter
    def debug_mode(self, value: bool) -> None:
        self._debug_mode = value
        if value:
            # Flush any buffered entries immediately when entering debug mode
            self.flush()

    @property
    def flush_interval(self) -> int:
        return self._flush_interval

    @flush_interval.setter
    def flush_interval(self, value: int) -> None:
        self._flush_interval = value

    @property
    def log_file(self) -> Path:
        return self._log_file

    def log(self, category: str, message: str, **details: object) -> None:
        now = datetime.now(timezone.utc)
        entry = LogEntry(
            timestamp=now.isoformat(),
            epoch=time.time(),
            category=category,
            message=message,
            details=details,
        )
        with self._lock:
            self._entries.append(entry)
            if self._debug_mode:
                self._write_entry(entry)
            else:
                self._unflushed.append(entry)

    def get_entries(
        self,
        limit: int = 100,
        category: str | None = None,
    ) -> list[dict]:
        with self._lock:
            entries = list(self._entries)
        if category:
            entries = [e for e in entries if e.category == category]
        return [e.to_dict() for e in reversed(entries[-limit:])]

    def flush(self) -> int:
        """Write buffered entries to disk. Returns number of entries flushed."""
        with self._lock:
            to_flush = list(self._unflushed)
            self._unflushed.clear()

        if not to_flush:
            self._last_flush_time = time.time()
            return 0

        self._rotate_if_needed()

        try:
            with open(self._log_file, "a") as f:
                for entry in to_flush:
                    f.write(entry.to_json() + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            # If write fails, put entries back in the buffer
            with self._lock:
                self._unflushed = to_flush + self._unflushed
            return 0

        self._last_flush_time = time.time()
        return len(to_flush)

    def should_flush(self) -> bool:
        """Check if it's time for a periodic flush."""
        if self._debug_mode:
            return False  # debug mode writes immediately
        return time.time() - self._last_flush_time >= self._flush_interval

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()
            self._unflushed.clear()

    def _write_entry(self, entry: LogEntry) -> None:
        """Write a single entry to disk immediately (debug mode)."""
        self._rotate_if_needed()
        try:
            with open(self._log_file, "a") as f:
                f.write(entry.to_json() + "\n")
                f.flush()
                os.fsync(f.fileno())
        except OSError:
            pass

    def _rotate_if_needed(self) -> None:
        """Rotate log file if it exceeds MAX_LOG_SIZE."""
        try:
            if self._log_file.exists() and self._log_file.stat().st_size > MAX_LOG_SIZE:
                backup = self._log_dir / "activity.1.jsonl"
                if backup.exists():
                    backup.unlink()
                self._log_file.rename(backup)
        except OSError:
            pass

    def _restore_from_disk(self) -> None:
        """Load previous log entries from disk on startup."""
        # Load from backup first (older), then current (newer)
        for filename in ["activity.1.jsonl", "activity.jsonl"]:
            path = self._log_dir / filename
            if not path.exists():
                continue
            try:
                with open(path) as f:
                    for line in f:
                        line = line.strip()
                        if not line:
                            continue
                        try:
                            data = json.loads(line)
                            entry = LogEntry(
                                timestamp=data.get("timestamp", ""),
                                epoch=0,
                                category=data.get("category", ""),
                                message=data.get("message", ""),
                                details=data.get("details", {}),
                            )
                            self._entries.append(entry)
                        except (json.JSONDecodeError, KeyError):
                            continue
            except OSError:
                continue

        if self._entries:
            # Don't count restored entries as unflushed
            self._last_flush_time = time.time()


# Singleton
activity_log = ActivityLog()
