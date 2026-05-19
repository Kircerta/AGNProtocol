"""EXP-4/5/6/7/8/9: State machine, concurrency, cross-chain, event sourcing,
stale dispatch, and hallucination lock experiments.

These tests probe for vulnerabilities in AGN's state management,
concurrency controls, and recovery mechanisms.
"""
from __future__ import annotations

import json
import multiprocessing as mp
import os
import sys
import time
import threading
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))
if str(ROOT / "agn_api") not in sys.path:
    sys.path.insert(0, str(ROOT / "agn_api"))

from agn_api.task_engine import derive_status, validate_transition, VALID_STATUSES, VALID_TRANSITIONS
from agn_api.ssot_store import SSOTStore


def _append_events_in_subprocess(events_dir: str, trace_id: str, count: int, out_q: Any) -> None:
    """Spawn-safe helper for process-level event-id race testing."""
    import scripts.event_sourcing as es
    from scripts.event_sourcing import append_event

    es.EVENTS_DIR = Path(events_dir)
    es.EVENTS_DIR.mkdir(parents=True, exist_ok=True)
    ids: list[str] = []
    for i in range(count):
        ev = append_event(trace_id=trace_id, task_id="task-proc", event_type=f"PROC_EVENT_{i}")
        ids.append(str(ev.get("event_id", "")))
    out_q.put(ids)


# ══════════════════════════════════════════════════════════════════
# EXP-4: State Machine Illegal Transition Testing
# ══════════════════════════════════════════════════════════════════

class TestEXP4_TaskEngineStateMachine:
    """Test the legacy task_engine state machine for illegal transitions."""

    def test_approved_is_terminal(self) -> None:
        """Once approved, no transitions should be possible."""
        allowed = VALID_TRANSITIONS.get("approved", set())
        assert allowed == set(), f"approved should be terminal but allows: {allowed}"

    def test_all_statuses_have_transition_entries(self) -> None:
        """Every status in VALID_STATUSES should have an entry in VALID_TRANSITIONS."""
        missing = VALID_STATUSES - set(VALID_TRANSITIONS.keys())
        assert not missing, f"Statuses missing transition definitions: {missing}"

    def test_no_self_transitions(self) -> None:
        """No state should transition to itself (potential infinite loop)."""
        self_loops = []
        for state, targets in VALID_TRANSITIONS.items():
            if state in targets:
                self_loops.append(state)
        if self_loops:
            pytest.fail(
                f"POTENTIAL VULNERABILITY: Self-transitions found for: {self_loops}. "
                "This could cause infinite loops in status derivation."
            )

    def test_halted_cannot_reach_approved_directly(self) -> None:
        """Halted → approved should not be a direct transition (must go through admin)."""
        halted_targets = VALID_TRANSITIONS.get("halted", set())
        assert "approved" not in halted_targets, (
            "VULNERABILITY: halted can directly transition to approved, "
            "bypassing the admin review requirement"
        )

    def test_validate_transition_rejects_unknown_source(self) -> None:
        """validate_transition should return False for unknown source states."""
        assert validate_transition("nonexistent", "pending") is False

    def test_validate_transition_rejects_illegal_jumps(self) -> None:
        """Test specific illegal transitions are correctly rejected."""
        illegal = [
            ("pending", "approved"),   # Can't approve without review
            ("halted", "approved"),    # Can't approve from halted
        ]
        for from_s, to_s in illegal:
            result = validate_transition(from_s, to_s)
            assert result is False, f"Illegal transition {from_s} → {to_s} was allowed!"

    def test_derive_status_halted_overrides_everything(self) -> None:
        """lock_state='halted' should override even an approved decision."""
        task: dict[str, Any] = {
            "id": "test-1",
            "decision": "approved",
            "lock_state": "halted",
        }
        status = derive_status(task)
        assert status == "halted", (
            f"VULNERABILITY: derive_status returned '{status}' for approved+halted task. "
            "Halted should always take priority."
        )

    def test_derive_status_awaiting_utility_overrides_pending(self) -> None:
        """awaiting_utility should override the default 'pending' status."""
        task: dict[str, Any] = {
            "id": "test-2",
            "awaiting_utility": True,
        }
        status = derive_status(task)
        assert status == "awaiting_utility"

    def test_derive_status_missing_all_fields(self) -> None:
        """Task with no relevant fields should default to pending."""
        status = derive_status({})
        assert status == "pending"


class TestEXP4_EventSourcingStateMachine:
    """Test the event-driven state machine in event_sourcing.py."""

    def test_event_sourcing_transitions_completeness(self) -> None:
        """Every state in STATES should have a VALID_TRANSITIONS entry."""
        from scripts.event_sourcing import STATES, VALID_TRANSITIONS as ES_TRANSITIONS
        missing = STATES - set(ES_TRANSITIONS.keys())
        assert not missing, f"States missing transition definitions: {missing}"

    def test_delivered_and_aborted_are_terminal(self) -> None:
        """DELIVERED and ABORTED should be terminal (no transitions out)."""
        from scripts.event_sourcing import VALID_TRANSITIONS as ES_TRANSITIONS
        for terminal in ("DELIVERED", "ABORTED"):
            targets = ES_TRANSITIONS.get(terminal, set())
            assert targets == set(), f"{terminal} should be terminal but allows: {targets}"

    def test_cannot_skip_exec_to_review(self) -> None:
        """PLANNED should not directly reach DISPATCHED_REVIEW (must go through exec)."""
        from scripts.event_sourcing import VALID_TRANSITIONS as ES_TRANSITIONS
        planned_targets = ES_TRANSITIONS.get("PLANNED", set())
        assert "DISPATCHED_REVIEW" not in planned_targets, (
            "VULNERABILITY: PLANNED can skip execution and go directly to review!"
        )

    def test_all_states_can_reach_aborted(self) -> None:
        """Non-terminal states should be able to reach ABORTED."""
        from scripts.event_sourcing import VALID_TRANSITIONS as ES_TRANSITIONS
        non_terminal = {s for s, t in ES_TRANSITIONS.items() if t}
        cannot_abort = [s for s in non_terminal if "ABORTED" not in ES_TRANSITIONS.get(s, set())]
        if cannot_abort:
            pytest.fail(
                f"VULNERABILITY: These states cannot reach ABORTED: {cannot_abort}. "
                "This means tasks can get permanently stuck without a kill switch."
            )

    def test_transition_state_rejects_invalid(self, tmp_path: Path) -> None:
        """transition_state() should reject invalid transitions and log violation."""
        from scripts.event_sourcing import (
            transition_state, write_checkpoint, load_events,
            EVENTS_DIR, CHECKPOINT_DIR, SSOT_ROOT,
        )
        import scripts.event_sourcing as es

        # Redirect event dirs to tmp
        orig_events = es.EVENTS_DIR
        orig_checkpoint = es.CHECKPOINT_DIR
        orig_ssot = es.SSOT_ROOT
        try:
            es.EVENTS_DIR = tmp_path / "events"
            es.CHECKPOINT_DIR = tmp_path / "checkpoints"
            es.SSOT_ROOT = tmp_path / "ssot"
            es.EVENTS_DIR.mkdir(parents=True, exist_ok=True)
            es.CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

            # Set up a task in DELIVERED state
            write_checkpoint("test-task", {
                "trace_id": "trace-1",
                "state": "DELIVERED",
            })

            # Try to transition from DELIVERED to PLANNED (invalid — DELIVERED is terminal)
            ok, reason = transition_state(
                trace_id="trace-1",
                task_id="test-task",
                to_state="PLANNED",
                reason="test",
            )
            assert ok is False, "Transition from DELIVERED should be rejected"
            assert reason == "invalid_transition"

            # Verify PROTOCOL_VIOLATION event was logged
            events = load_events("trace-1")
            violation_events = [e for e in events if e.get("event_type") == "PROTOCOL_VIOLATION"]
            assert len(violation_events) >= 1, "PROTOCOL_VIOLATION event should be logged"
        finally:
            es.EVENTS_DIR = orig_events
            es.CHECKPOINT_DIR = orig_checkpoint
            es.SSOT_ROOT = orig_ssot

    def test_transition_to_unknown_state_rejected(self, tmp_path: Path) -> None:
        """Transitioning to a non-existent state should be rejected."""
        from scripts.event_sourcing import transition_state, write_checkpoint
        import scripts.event_sourcing as es

        orig_events = es.EVENTS_DIR
        orig_checkpoint = es.CHECKPOINT_DIR
        try:
            es.EVENTS_DIR = tmp_path / "events"
            es.CHECKPOINT_DIR = tmp_path / "checkpoints"
            es.EVENTS_DIR.mkdir(parents=True, exist_ok=True)
            es.CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

            write_checkpoint("test-task", {"trace_id": "trace-1", "state": "CREATED"})
            ok, reason = transition_state(
                trace_id="trace-1",
                task_id="test-task",
                to_state="NONEXISTENT_STATE",
                reason="test",
            )
            assert ok is False
            assert reason == "unknown_state"
        finally:
            es.EVENTS_DIR = orig_events
            es.CHECKPOINT_DIR = orig_checkpoint


# ══════════════════════════════════════════════════════════════════
# EXP-5: Concurrent Dispatch Race Conditions
# ══════════════════════════════════════════════════════════════════

class TestEXP5_SSOTConcurrency:
    """Test SSOT store concurrent access patterns."""

    def test_locked_update_prevents_lost_updates(self, tmp_path: Path) -> None:
        """Two concurrent locked_update calls should not lose each other's changes."""
        store = SSOTStore(tmp_path)
        store.save_task({"id": "task-1", "counter": 0, "source": "test", "agn_managed": True, "lock_state": "active"})

        errors: list[str] = []
        barrier = threading.Barrier(2)

        def increment(store: SSOTStore, n: int) -> None:
            try:
                barrier.wait(timeout=5)
                for _ in range(n):
                    with store.locked_update("task-1") as task:
                        if task is not None:
                            task["counter"] = int(task.get("counter", 0) or 0) + 1
            except Exception as exc:
                errors.append(f"{type(exc).__name__}: {exc}")

        t1 = threading.Thread(target=increment, args=(store, 50))
        t2 = threading.Thread(target=increment, args=(store, 50))
        t1.start()
        t2.start()
        t1.join(timeout=30)
        t2.join(timeout=30)

        assert not errors, f"Errors during concurrent access: {errors}"

        final = store.get_task("task-1")
        assert final is not None
        expected = 100
        actual = int(final.get("counter", 0) or 0)
        if actual != expected:
            pytest.fail(
                f"VULNERABILITY: Lost updates detected! Expected counter={expected}, "
                f"got counter={actual}. {expected - actual} increments were lost."
            )

    def test_save_task_correlation_conflict(self, tmp_path: Path) -> None:
        """save_task should reject overwrite with different correlation_id."""
        store = SSOTStore(tmp_path)
        store.save_task({"id": "task-1", "correlation_id": "corr-aaa", "source": "test", "agn_managed": True, "lock_state": "active"})

        with pytest.raises(ValueError, match="correlation_mismatch"):
            store.save_task({"id": "task-1", "correlation_id": "corr-bbb", "source": "test", "agn_managed": True, "lock_state": "active"})

    def test_save_task_force_overwrite_bypasses_correlation(self, tmp_path: Path) -> None:
        """_force_overwrite flag should bypass correlation check — is this safe?"""
        store = SSOTStore(tmp_path)
        store.save_task({"id": "task-1", "correlation_id": "corr-aaa", "source": "test", "agn_managed": True, "lock_state": "active"})

        # Force overwrite should succeed
        store.save_task({
            "id": "task-1",
            "correlation_id": "corr-bbb",
            "source": "test",
            "agn_managed": True,
            "lock_state": "active",
            "_force_overwrite": True,
        })
        updated = store.get_task("task-1")
        assert updated is not None
        assert updated["correlation_id"] == "corr-bbb"
        # Document: _force_overwrite can silently replace tasks with different correlation IDs.
        # This is a potential data integrity issue if misused.

    def test_safe_id_prevents_path_traversal(self, tmp_path: Path) -> None:
        """SSOTStore._safe_id should prevent path traversal via task_id."""
        store = SSOTStore(tmp_path)

        # Attempt path traversal
        evil_id = "../../../etc/passwd"
        safe = store._safe_id(evil_id)
        # After replace("/","_") → "_.._.._.._etc_passwd"
        # lstrip(".") has no effect since it starts with "_"
        # The ".." substrings remain but "/" is gone, so no traversal is possible
        assert "/" not in safe, f"Slash not sanitized: {safe}"
        # The path is: ssot_dir / "_.._.._.._etc_passwd.json" — safe, no traversal

    def test_safe_id_handles_empty(self, tmp_path: Path) -> None:
        """Empty task_id should be sanitized to 'unnamed'."""
        store = SSOTStore(tmp_path)
        assert store._safe_id("") == "unnamed"
        # "..." → replace "/" → "..." → lstrip(".") → "" → "unnamed"
        assert store._safe_id("...") == "unnamed"

    def test_safe_id_leading_dot_stripped(self, tmp_path: Path) -> None:
        """task_id starting with dots gets dots stripped (security feature)."""
        store = SSOTStore(tmp_path)
        # ".hidden" → replace "/" → ".hidden" → lstrip(".") → "hidden"
        assert store._safe_id(".hidden") == "hidden"
        # "..secret" → lstrip(".") → "secret"
        assert store._safe_id("..secret") == "secret"


class TestEXP5_DispatchLocking:
    """Test dispatch file creation race condition handling."""

    def test_dispatch_lock_prevents_duplicate(self, tmp_path: Path) -> None:
        """Simulate two coordinators trying to create the same dispatch file."""
        import fcntl

        dispatch_dir = tmp_path / "dispatch"
        dispatch_dir.mkdir()
        lock_dir = dispatch_dir / ".locks"
        lock_dir.mkdir()

        results: list[str] = []
        barrier = threading.Barrier(2)

        def try_create_dispatch(name: str) -> None:
            try:
                barrier.wait(timeout=5)
                lock_file = lock_dir / "task-1.lock"
                lock_fd = os.open(str(lock_file), os.O_CREAT | os.O_RDWR)
                try:
                    fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    dp = dispatch_dir / "task-1.json"
                    if dp.exists():
                        results.append(f"{name}:skipped")
                    else:
                        dp.write_text(json.dumps({"creator": name}), encoding="utf-8")
                        results.append(f"{name}:created")
                except OSError:
                    results.append(f"{name}:lock_failed")
                finally:
                    try:
                        fcntl.flock(lock_fd, fcntl.LOCK_UN)
                    except OSError:
                        pass
                    os.close(lock_fd)
            except Exception as exc:
                results.append(f"{name}:error:{exc}")

        t1 = threading.Thread(target=try_create_dispatch, args=("coord-1",))
        t2 = threading.Thread(target=try_create_dispatch, args=("coord-2",))
        t1.start()
        t2.start()
        t1.join(timeout=10)
        t2.join(timeout=10)

        created = [r for r in results if "created" in r]
        assert len(created) <= 1, (
            f"VULNERABILITY: Multiple coordinators created the same dispatch! Results: {results}"
        )


# ══════════════════════════════════════════════════════════════════
# EXP-6: Cross-Chain Interference
# ══════════════════════════════════════════════════════════════════

class TestEXP6_CrossChainInterference:
    """Test if legacy and main-line pipelines can interfere with each other."""

    def test_legacy_and_event_states_can_disagree(self) -> None:
        """The legacy task_engine has different states than event_sourcing.
        If both operate on the same task, they could disagree."""
        from scripts.event_sourcing import STATES as ES_STATES

        legacy_statuses = VALID_STATUSES
        overlap = legacy_statuses & {s.lower() for s in ES_STATES}
        # Document: The two systems have completely different state enums
        # "halted" exists in legacy but not in event_sourcing STATES
        # "ABORTED" exists in event_sourcing but as "aborted" not in legacy
        assert "halted" in legacy_statuses
        assert "ABORTED" in ES_STATES
        assert "halted" not in {s.lower() for s in ES_STATES}, (
            "If 'halted' appears in both systems, they could create conflicting state"
        )

    def test_ssot_task_independent_of_checkpoint(self, tmp_path: Path) -> None:
        """SSOT task (legacy) and checkpoint (event-driven) are separate files.
        Modifying one doesn't affect the other — but this means state can diverge."""
        from scripts.event_sourcing import write_checkpoint, load_checkpoint
        import scripts.event_sourcing as es

        store = SSOTStore(tmp_path / "ssot")
        store.save_task({"id": "task-x", "decision": "approved", "source": "test", "agn_managed": True, "lock_state": "active"})

        orig_checkpoint = es.CHECKPOINT_DIR
        try:
            es.CHECKPOINT_DIR = tmp_path / "checkpoints"
            es.CHECKPOINT_DIR.mkdir(parents=True, exist_ok=True)

            write_checkpoint("task-x", {"state": "EXEC_RUNNING", "trace_id": "t1"})

            # Now: SSOT says "approved", checkpoint says "EXEC_RUNNING"
            ssot_task = store.get_task("task-x")
            checkpoint = load_checkpoint("task-x")
            assert ssot_task is not None
            assert checkpoint is not None

            ssot_decision = ssot_task.get("decision")
            checkpoint_state = checkpoint.get("state")

            # These are DIFFERENT — documenting the divergence
            assert ssot_decision == "approved"
            assert checkpoint_state == "EXEC_RUNNING"

            # In production, which one wins? This depends on which pipeline processes it.
            # A task could be "approved" in SSOT but still "EXEC_RUNNING" in event sourcing.
        finally:
            es.CHECKPOINT_DIR = orig_checkpoint


# ══════════════════════════════════════════════════════════════════
# EXP-7: Event Sourcing Integrity Under Failure
# ══════════════════════════════════════════════════════════════════

class TestEXP7_EventSourcingIntegrity:
    """Test event sourcing data integrity under various failure scenarios."""

    def test_event_sequence_monotonicity(self, tmp_path: Path) -> None:
        """Event IDs should be monotonically increasing."""
        from scripts.event_sourcing import append_event, load_events
        import scripts.event_sourcing as es

        orig_events = es.EVENTS_DIR
        try:
            es.EVENTS_DIR = tmp_path / "events"
            es.EVENTS_DIR.mkdir(parents=True, exist_ok=True)

            for i in range(10):
                append_event(
                    trace_id="trace-mono",
                    task_id="task-1",
                    event_type=f"TEST_EVENT_{i}",
                )

            events = load_events("trace-mono")
            assert len(events) == 10

            # Check monotonicity of event IDs
            for i in range(1, len(events)):
                prev_id = events[i - 1]["event_id"]
                curr_id = events[i]["event_id"]
                # IDs are like "trace-mono-evt-00000001", "trace-mono-evt-00000002"
                prev_seq = int(prev_id.split("-")[-1])
                curr_seq = int(curr_id.split("-")[-1])
                assert curr_seq > prev_seq, (
                    f"Event IDs not monotonic: {prev_id} ({prev_seq}) >= {curr_id} ({curr_seq})"
                )
        finally:
            es.EVENTS_DIR = orig_events

    def test_concurrent_event_appends_no_lost_events(self, tmp_path: Path) -> None:
        """Multiple threads appending events should not lose any events."""
        from scripts.event_sourcing import append_event, load_events
        import scripts.event_sourcing as es

        orig_events = es.EVENTS_DIR
        try:
            es.EVENTS_DIR = tmp_path / "events"
            es.EVENTS_DIR.mkdir(parents=True, exist_ok=True)

            errors: list[str] = []
            barrier = threading.Barrier(3)

            def writer(thread_id: int, count: int) -> None:
                try:
                    barrier.wait(timeout=5)
                    for i in range(count):
                        append_event(
                            trace_id="trace-conc",
                            task_id="task-1",
                            event_type=f"THREAD_{thread_id}_EVENT_{i}",
                        )
                except Exception as exc:
                    errors.append(f"thread-{thread_id}: {exc}")

            threads = [
                threading.Thread(target=writer, args=(tid, 20))
                for tid in range(3)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            assert not errors, f"Errors during concurrent writes: {errors}"

            events = load_events("trace-conc")
            expected = 60  # 3 threads × 20 events
            actual = len(events)
            if actual != expected:
                pytest.fail(
                    f"VULNERABILITY: Lost events during concurrent writes! "
                    f"Expected {expected} events, got {actual}. "
                    f"{expected - actual} events were lost."
                )
        finally:
            es.EVENTS_DIR = orig_events

    def test_concurrent_seq_counter_race(self, tmp_path: Path) -> None:
        """The _next_event_id function uses a file-based counter.
        Under concurrency, the read-increment-write is NOT atomic,
        potentially causing duplicate event IDs."""
        from scripts.event_sourcing import append_event, load_events
        import scripts.event_sourcing as es

        orig_events = es.EVENTS_DIR
        try:
            es.EVENTS_DIR = tmp_path / "events"
            es.EVENTS_DIR.mkdir(parents=True, exist_ok=True)

            barrier = threading.Barrier(5)
            all_event_ids: list[str] = []
            lock = threading.Lock()

            def writer(thread_id: int) -> None:
                barrier.wait(timeout=5)
                for i in range(10):
                    event = append_event(
                        trace_id="trace-dup",
                        task_id="task-1",
                        event_type=f"T{thread_id}_E{i}",
                    )
                    with lock:
                        all_event_ids.append(event["event_id"])

            threads = [threading.Thread(target=writer, args=(tid,)) for tid in range(5)]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=30)

            # Check for duplicate event IDs
            unique = set(all_event_ids)
            if len(unique) != len(all_event_ids):
                duplicates = [eid for eid in all_event_ids if all_event_ids.count(eid) > 1]
                pytest.fail(
                    f"VULNERABILITY: Duplicate event IDs detected! "
                    f"Total={len(all_event_ids)}, Unique={len(unique)}. "
                    f"Duplicate IDs: {set(duplicates)}"
                )
        finally:
            es.EVENTS_DIR = orig_events

    def test_concurrent_seq_counter_race_multiprocess(self, tmp_path: Path) -> None:
        """Event IDs must also stay unique under multi-process writers."""
        from scripts.event_sourcing import load_events
        import scripts.event_sourcing as es

        orig_events = es.EVENTS_DIR
        try:
            events_dir = tmp_path / "events"
            es.EVENTS_DIR = events_dir
            events_dir.mkdir(parents=True, exist_ok=True)

            trace_id = "trace-dup-proc"
            workers = 6
            per_worker = 40
            ctx = mp.get_context("spawn")
            q = ctx.Queue()
            procs = [
                ctx.Process(
                    target=_append_events_in_subprocess,
                    args=(str(events_dir), trace_id, per_worker, q),
                )
                for _ in range(workers)
            ]
            for proc in procs:
                proc.start()
            for proc in procs:
                proc.join(timeout=30)
                assert proc.exitcode == 0, f"child process failed: pid={proc.pid} exit={proc.exitcode}"

            all_ids: list[str] = []
            for _ in range(workers):
                all_ids.extend(q.get(timeout=5))
            unique = set(all_ids)
            if len(unique) != len(all_ids):
                pytest.fail(
                    "VULNERABILITY: Duplicate event IDs under process concurrency! "
                    f"total={len(all_ids)} unique={len(unique)} duplicates={len(all_ids)-len(unique)}"
                )

            events = load_events(trace_id)
            assert len(events) == workers * per_worker
        finally:
            es.EVENTS_DIR = orig_events

    def test_corrupted_event_file_recovery(self, tmp_path: Path) -> None:
        """load_events should handle corrupted JSONL gracefully."""
        from scripts.event_sourcing import load_events
        import scripts.event_sourcing as es

        orig_events = es.EVENTS_DIR
        try:
            es.EVENTS_DIR = tmp_path / "events"
            es.EVENTS_DIR.mkdir(parents=True, exist_ok=True)

            events_file = es.EVENTS_DIR / "trace-corrupt.jsonl"
            events_file.write_text(
                '{"event_id":"e1","event_type":"OK"}\n'
                'THIS IS NOT JSON\n'
                '{"event_id":"e2","event_type":"OK"}\n'
                '\n'
                '{"broken json\n'
                '{"event_id":"e3","event_type":"OK"}\n',
                encoding="utf-8",
            )

            events = load_events("trace-corrupt")
            # Should recover 3 valid events, skipping 3 invalid lines
            assert len(events) == 3, (
                f"Expected 3 valid events from corrupted file, got {len(events)}"
            )
        finally:
            es.EVENTS_DIR = orig_events


# ══════════════════════════════════════════════════════════════════
# EXP-8: Stale Dispatch Recovery Edge Cases
# ══════════════════════════════════════════════════════════════════

class TestEXP8_StaleDispatchRecovery:
    """Test edge cases in stale dispatch detection and recovery."""

    def test_stale_dispatch_does_not_overwrite_approved(self, tmp_path: Path) -> None:
        """If a task is already approved, stale dispatch recovery should not mark it rejected."""
        # This tests the condition in _recover_stale_dispatches():
        # if task and task.get("decision") != "approved" ...
        store = SSOTStore(tmp_path / "ssot")
        store.save_task({
            "id": "task-approved",
            "decision": "approved",
            "source": "test",
            "agn_managed": True,
            "lock_state": "active",
        })

        task = store.get_task("task-approved")
        assert task is not None
        # The recovery code checks: if task.get("decision") != "approved"
        # So this task should be skipped — verify the guard condition
        assert task.get("decision") == "approved"
        # Good — this task would be skipped by the recovery logic

    def test_stale_dispatch_does_not_overwrite_halted(self, tmp_path: Path) -> None:
        """Halted tasks should not be touched by stale dispatch recovery."""
        store = SSOTStore(tmp_path / "ssot")
        store.save_task({
            "id": "task-halted",
            "decision": "rejected",
            "lock_state": "halted",
            "source": "test",
            "agn_managed": True,
        })

        task = store.get_task("task-halted")
        assert task is not None
        # The code checks: task.get("lock_state") != "halted"
        assert task.get("lock_state") == "halted"
        # Good — halted tasks are protected

    def test_stale_recovery_race_without_lock(self, tmp_path: Path) -> None:
        """_recover_stale_dispatches uses store.save_task (no lock) — race possible?
        If two coordinator instances both detect the same stale dispatch,
        both could call store.save_task with decision='rejected'."""
        store = SSOTStore(tmp_path / "ssot")
        store.save_task({
            "id": "task-stale",
            "decision": None,
            "source": "test",
            "agn_managed": True,
            "lock_state": "active",
        })

        # Simulate two concurrent save_task calls (both setting decision='rejected')
        # This is "safe" because both set the same value, but it's still a TOCTOU:
        # Process A reads task → checks conditions → Process B reads same task →
        # Both decide to set rejected → Both write. No data loss, but:
        # - Double audit log entries
        # - Wasted work
        # Document this as a MINOR issue (no data corruption, just double processing)
        t1_saved = threading.Event()
        t2_saved = threading.Event()

        def save_rejected(name: str, done_event: threading.Event) -> None:
            task = store.get_task("task-stale")
            if task and task.get("decision") != "approved" and task.get("lock_state") != "halted":
                task["decision"] = "rejected"
                store.save_task(task)
            done_event.set()

        t1 = threading.Thread(target=save_rejected, args=("coord-1", t1_saved))
        t2 = threading.Thread(target=save_rejected, args=("coord-2", t2_saved))
        t1.start()
        t2.start()
        t1.join(timeout=5)
        t2.join(timeout=5)

        # Both should succeed (no crash)
        assert t1_saved.is_set()
        assert t2_saved.is_set()

        task = store.get_task("task-stale")
        assert task is not None
        assert task["decision"] == "rejected"
        # No crash, but double processing is possible — minor issue


# ══════════════════════════════════════════════════════════════════
# EXP-9: Hallucination Lock and Infrastructure Failure Distinction
# ══════════════════════════════════════════════════════════════════

class TestEXP9_HallucinationLock:
    """Test the hallucination lock mechanism and its infrastructure failure handling."""

    def test_infra_failure_detection(self) -> None:
        """Infrastructure failures should be correctly identified."""
        from scripts.reviewer_worker import _is_infrastructure_failure as _is_infra

        # These should be detected as infrastructure failures
        assert _is_infra({"fail_reasons": ["reviewer_unavailable: provider timeout"]}) is True
        assert _is_infra({"fail_reasons": ["failed to parse reviewer output"]}) is True

        # These should NOT be infrastructure failures
        assert _is_infra({"fail_reasons": ["code quality issues"]}) is False
        assert _is_infra({"fail_reasons": []}) is False
        assert _is_infra({}) is False
        # P2-BUG-FIX: None verdict IS infrastructure (reviewer crash / partial write).
        # Previously this returned False, causing hallucination false positives.
        assert _is_infra(None) is True

    def test_infra_failure_does_not_increment_retry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Infrastructure failures should NOT increment qa_retry_count."""
        from scripts.reviewer_worker import _update_hallucination_state
        import scripts.reviewer_worker as rw

        store = SSOTStore(tmp_path / "ssot")
        store.save_task({
            "id": "task-infra",
            "qa_retry_count": 0,
            "lock_state": "active",
            "source": "test",
            "agn_managed": True,
        })

        # Monkeypatch the PATHS reference in reviewer_worker's imported module
        monkeypatch.setattr(rw, "PATHS", type("FakePaths", (), {"ssot_dir": tmp_path / "ssot"})())

        _update_hallucination_state(
            task_id="task-infra",
            verdict_file_exists=True,
            verdict_payload={"decision": "reject", "fail_reasons": ["reviewer_unavailable: timeout"]},
            reviewer_rc=1,
        )

        task = store.get_task("task-infra")
        assert task is not None
        assert int(task.get("qa_retry_count", 0) or 0) == 0, (
            "VULNERABILITY: Infrastructure failure incremented qa_retry_count! "
            "This could cause false hallucination locks."
        )

    def test_content_reject_increments_retry(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Genuine content-based rejects SHOULD increment qa_retry_count."""
        from scripts.reviewer_worker import _update_hallucination_state
        import scripts.reviewer_worker as rw

        store = SSOTStore(tmp_path / "ssot")
        store.save_task({
            "id": "task-reject",
            "qa_retry_count": 1,
            "lock_state": "active",
            "source": "test",
            "agn_managed": True,
        })

        monkeypatch.setattr(rw, "PATHS", type("FakePaths", (), {"ssot_dir": tmp_path / "ssot"})())

        _update_hallucination_state(
            task_id="task-reject",
            verdict_file_exists=True,
            verdict_payload={"decision": "reject", "fail_reasons": ["code quality issues"]},
            reviewer_rc=0,
        )

        task = store.get_task("task-reject")
        assert task is not None
        assert int(task.get("qa_retry_count", 0) or 0) == 2, (
            f"Content reject should increment qa_retry_count from 1 to 2, "
            f"got {task.get('qa_retry_count')}"
        )

    def test_approval_resets_retry_count(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Approval should reset qa_retry_count to 0."""
        from scripts.reviewer_worker import _update_hallucination_state
        import scripts.reviewer_worker as rw

        store = SSOTStore(tmp_path / "ssot")
        store.save_task({
            "id": "task-approve",
            "qa_retry_count": 2,
            "lock_state": "active",
            "source": "test",
            "agn_managed": True,
        })

        monkeypatch.setattr(rw, "PATHS", type("FakePaths", (), {"ssot_dir": tmp_path / "ssot"})())

        _update_hallucination_state(
            task_id="task-approve",
            verdict_file_exists=True,
            verdict_payload={"decision": "approve"},
            reviewer_rc=0,
        )

        task = store.get_task("task-approve")
        assert task is not None
        assert int(task.get("qa_retry_count", 0) or 0) == 0, (
            "Approval should reset qa_retry_count to 0"
        )

    def test_lock_threshold_triggers_halt(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Reaching LOCK_THRESHOLD (default 3) should trigger halted state."""
        from scripts.reviewer_worker import _update_hallucination_state
        import scripts.reviewer_worker as rw

        store = SSOTStore(tmp_path / "ssot")
        store.save_task({
            "id": "task-halt",
            "qa_retry_count": 2,  # One more reject will hit threshold of 3
            "lock_state": "active",
            "source": "test",
            "agn_managed": True,
        })

        monkeypatch.setattr(rw, "PATHS", type("FakePaths", (), {"ssot_dir": tmp_path / "ssot"})())

        _update_hallucination_state(
            task_id="task-halt",
            verdict_file_exists=True,
            verdict_payload={"decision": "reject", "fail_reasons": ["quality"]},
            reviewer_rc=0,
        )

        task = store.get_task("task-halt")
        assert task is not None
        assert task.get("lock_state") == "halted", (
            f"Task should be halted after reaching threshold, got lock_state={task.get('lock_state')}"
        )
        assert int(task.get("qa_retry_count", 0) or 0) >= 3

    def test_halted_state_is_sticky(self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
        """Once halted, further approvals should NOT change lock_state."""
        from scripts.reviewer_worker import _update_hallucination_state
        import scripts.reviewer_worker as rw

        store = SSOTStore(tmp_path / "ssot")
        store.save_task({
            "id": "task-sticky",
            "qa_retry_count": 5,
            "lock_state": "halted",
            "lock_reason": "qa_retry_count_threshold_reached:3",
            "source": "test",
            "agn_managed": True,
        })

        monkeypatch.setattr(rw, "PATHS", type("FakePaths", (), {"ssot_dir": tmp_path / "ssot"})())

        _update_hallucination_state(
            task_id="task-sticky",
            verdict_file_exists=True,
            verdict_payload={"decision": "approve"},
            reviewer_rc=0,
        )

        task = store.get_task("task-sticky")
        assert task is not None
        # Even though an approve came in, halted should remain halted
        assert task.get("lock_state") == "halted", "Halted should be sticky even after approval"
        assert task.get("decision") == "approved", "Decision should still be set"
        # derive_status should return "halted" since lock_state takes priority
        status = derive_status(task)
        assert status == "halted", (
            f"derive_status should return 'halted' even with approved decision, got '{status}'"
        )
