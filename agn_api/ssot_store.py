from __future__ import annotations

from contextlib import contextmanager
import fcntl
from pathlib import Path
import json
import os
import tempfile
from typing import Any, Generator


class SSOTStore:
    def __init__(self, root_dir: Path) -> None:
        self.root_dir = root_dir
        self.root_dir.mkdir(parents=True, exist_ok=True)
        self._index_dir = self.root_dir / ".index"

    def list_tasks(self) -> list[dict[str, Any]]:
        tasks: list[dict[str, Any]] = []
        for path in sorted(self.root_dir.glob("*.json")):
            try:
                tasks.append(self._read_json(path))
            except Exception:
                continue
        return tasks

    def get_task(self, task_id: str) -> dict[str, Any] | None:
        path = self._task_path(task_id)
        if not path.exists():
            return None
        return self._read_json(path)

    def save_task(self, task: dict[str, Any]) -> None:
        task_id = str(task["id"])
        target_path = self._task_path(task_id)
        existing = self.get_task(task_id)
        if existing is not None and not bool(task.get("_force_overwrite", False)):
            old_corr = str(existing.get("correlation_id", "")).strip()
            new_corr = str(task.get("correlation_id", "")).strip()
            if old_corr and new_corr and old_corr != new_corr:
                raise ValueError(f"task_id_conflict_correlation_mismatch:{task_id}:{old_corr}!={new_corr}")
        self._enforce_transition(existing, task)
        self._atomic_write_json(target_path, task)
        self._update_correlation_index(task_id, task)

    @contextmanager
    def locked_update(self, task_id: str) -> Generator[dict[str, Any] | None, None, None]:
        """Context manager for atomic read-modify-write with advisory file locking.

        Usage:
            with store.locked_update(task_id) as task:
                if task is not None:
                    task["decision"] = "approved"
                    # task is automatically saved on context exit

        The lock file is separate from the data file to avoid interfering with
        atomic writes (tmp + rename). Lock is held for the duration of the block.
        """
        lock_path = self._lock_path(task_id)
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        lock_fd = os.open(str(lock_path), os.O_CREAT | os.O_RDWR)
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_EX)
            task = self.get_task(task_id)
            yield task
            if task is not None:
                self.save_task(task)
        finally:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            os.close(lock_fd)

    def get_task_by_correlation(self, correlation_id: str) -> dict[str, Any] | None:
        """O(1) lookup by correlation_id using the index, with glob fallback."""
        correlation_id = str(correlation_id).strip()
        if not correlation_id:
            return None
        index = self._load_correlation_index()
        task_id = index.get(correlation_id, "")
        if task_id:
            task = self.get_task(task_id)
            if task is not None and str(task.get("correlation_id", "")).strip() == correlation_id:
                return task
        # Fallback: linear scan + rebuild index entry.
        for path in self.root_dir.glob("*.json"):
            try:
                payload = self._read_json(path)
            except Exception:
                continue
            if str(payload.get("correlation_id", "")).strip() == correlation_id:
                tid = str(payload.get("id", "")).strip()
                if tid:
                    self._update_correlation_index(tid, payload)
                return payload
        return None

    def _load_correlation_index(self) -> dict[str, str]:
        index_path = self._index_dir / "correlation.json"
        if not index_path.exists():
            return {}
        try:
            data = json.loads(index_path.read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _update_correlation_index(self, task_id: str, task: dict[str, Any]) -> None:
        corr_id = str(task.get("correlation_id", "")).strip()
        if not corr_id:
            return
        try:
            self._index_dir.mkdir(parents=True, exist_ok=True)
            index = self._load_correlation_index()
            index[corr_id] = task_id
            self._atomic_write_json(self._index_dir / "correlation.json", index)
        except Exception:
            pass

    def _enforce_transition(self, existing: dict[str, Any] | None, new_task: dict[str, Any]) -> None:
        """Log warning on invalid state transitions. Soft enforcement — does not block saves."""
        if existing is None:
            return
        try:
            from task_engine import derive_status, validate_transition
        except ImportError:
            try:
                from agn_api.task_engine import derive_status, validate_transition
            except ImportError:
                return
        old_status = derive_status(existing)
        new_status = derive_status(new_task)
        if old_status == new_status:
            return
        if not validate_transition(old_status, new_status):
            import sys
            task_id = str(new_task.get("id", "unknown"))
            print(
                f"[ssot_store] INVALID_TRANSITION task_id={task_id} {old_status}->{new_status}",
                file=sys.stderr,
            )

    def _task_path(self, task_id: str) -> Path:
        safe_id = self._safe_id(task_id)
        return self.root_dir / f"{safe_id}.json"

    def _lock_path(self, task_id: str) -> Path:
        safe_id = self._safe_id(task_id)
        locks_dir = self.root_dir / ".locks"
        locks_dir.mkdir(parents=True, exist_ok=True)
        return locks_dir / f"{safe_id}.lock"

    @staticmethod
    def _safe_id(task_id: str) -> str:
        """P3-23: sanitize task_id to prevent path traversal and length issues."""
        raw = task_id.replace("/", "_").lstrip(".")
        if not raw:
            raw = "unnamed"
        if len(raw) > 200:
            raw = raw[:200]
        return raw

    @staticmethod
    def _read_json(path: Path) -> dict[str, Any]:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)

    @staticmethod
    def _atomic_write_json(path: Path, payload: dict[str, Any]) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp_name = tempfile.mkstemp(prefix=f".{path.name}.", suffix=".tmp", dir=path.parent)
        tmp_path = Path(tmp_name)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                json.dump(payload, f, ensure_ascii=True, indent=2, sort_keys=True)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            os.replace(tmp_path, path)
        finally:
            if tmp_path.exists():
                tmp_path.unlink()
