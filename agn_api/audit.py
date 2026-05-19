from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
import json
import os
import threading

# Default max size before rotation: 10 MB.
DEFAULT_MAX_BYTES = 10 * 1024 * 1024
# Keep at most 5 rotated files.
DEFAULT_BACKUP_COUNT = 5


@dataclass
class AuditLogger:
    log_path: Path
    max_bytes: int = DEFAULT_MAX_BYTES
    backup_count: int = DEFAULT_BACKUP_COUNT
    _lock: threading.Lock = field(default_factory=threading.Lock, init=False, repr=False)

    def __post_init__(self) -> None:
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        if not self.log_path.exists():
            self.log_path.touch()

    def log_event(self, *, route: str, status: int, task_id: str | None, **extra: object) -> None:
        event = {
            "timestamp": datetime.now(tz=timezone.utc).isoformat(),
            "route": route,
            "status": status,
            "task_id": task_id,
        }
        event.update(extra)
        line = json.dumps(event, ensure_ascii=True) + "\n"
        with self._lock:
            self._maybe_rotate()
            with self.log_path.open("a", encoding="utf-8") as f:
                f.write(line)

    def _maybe_rotate(self) -> None:
        """Rotate log file if it exceeds max_bytes."""
        if self.max_bytes <= 0:
            return
        try:
            size = self.log_path.stat().st_size
        except OSError:
            return
        if size < self.max_bytes:
            return
        self._do_rotate()

    def _do_rotate(self) -> None:
        """Rotate: events.jsonl -> events.jsonl.1, .1 -> .2, etc."""
        for i in range(self.backup_count - 1, 0, -1):
            src = Path(f"{self.log_path}.{i}")
            dst = Path(f"{self.log_path}.{i + 1}")
            if src.exists():
                src.rename(dst)
        # Current -> .1
        backup_1 = Path(f"{self.log_path}.1")
        if self.log_path.exists():
            self.log_path.rename(backup_1)
        # Remove oldest if over backup_count.
        oldest = Path(f"{self.log_path}.{self.backup_count + 1}")
        if oldest.exists():
            oldest.unlink()
        # Create fresh log file.
        self.log_path.touch()
