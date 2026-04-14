"""In-memory activity log for troubleshooting."""

import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone


@dataclass
class LogEntry:
    timestamp: str
    epoch: float
    category: str  # auth, poll, read, write, rate_limit, refresh, error
    message: str
    details: dict = field(default_factory=dict)

    def to_dict(self) -> dict:
        return {
            "timestamp": self.timestamp,
            "category": self.category,
            "message": self.message,
            "details": self.details,
        }


class ActivityLog:
    """Thread-safe ring buffer of activity log entries."""

    def __init__(self, max_entries: int = 500):
        self._entries: deque[LogEntry] = deque(maxlen=max_entries)
        self._lock = threading.Lock()

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

    def clear(self) -> None:
        with self._lock:
            self._entries.clear()


# Singleton
activity_log = ActivityLog()
