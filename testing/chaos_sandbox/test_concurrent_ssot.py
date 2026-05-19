#!/usr/bin/env python3
"""Chaos Experiment 1: Concurrent SSOT write race condition.

Spawns N threads that simultaneously read-modify-write the same SSOT task.
Half use locked_update (safe), half use bare save_task (unsafe).
Measures data loss by checking final field counts.
"""
import json
import sys
import threading
import time
from pathlib import Path
from uuid import uuid4

ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "agn_api"))

from ssot_store import SSOTStore

SANDBOX_SSOT = Path(__file__).parent / "ssot_race_test"
TASK_ID = f"chaos-race-{uuid4().hex[:8]}"
NUM_WRITERS = 10
WRITES_PER_THREAD = 20

results = {"locked_wins": 0, "bare_wins": 0, "errors": []}
barrier = threading.Barrier(NUM_WRITERS)


def locked_writer(store: SSOTStore, thread_id: int):
    """Writer using locked_update (correct path)."""
    for i in range(WRITES_PER_THREAD):
        try:
            with store.locked_update(TASK_ID) as task:
                if task is None:
                    results["errors"].append(f"locked_{thread_id}_{i}: task_is_none")
                    continue
                marks = task.setdefault("locked_marks", [])
                marks.append(f"L{thread_id}:{i}")
                task["last_locked_writer"] = f"L{thread_id}:{i}"
        except Exception as exc:
            results["errors"].append(f"locked_{thread_id}_{i}: {type(exc).__name__}: {exc}")


def bare_writer(store: SSOTStore, thread_id: int):
    """Writer using bare get_task + save_task (unsafe path — simulates old bugs)."""
    for i in range(WRITES_PER_THREAD):
        try:
            task = store.get_task(TASK_ID)
            if task is None:
                results["errors"].append(f"bare_{thread_id}_{i}: task_is_none")
                continue
            # Simulate some work delay to widen the race window
            time.sleep(0.001)
            marks = task.setdefault("bare_marks", [])
            marks.append(f"B{thread_id}:{i}")
            task["last_bare_writer"] = f"B{thread_id}:{i}"
            store.save_task(task)
        except Exception as exc:
            results["errors"].append(f"bare_{thread_id}_{i}: {type(exc).__name__}: {exc}")


def main():
    # Clean up from previous runs
    if SANDBOX_SSOT.exists():
        import shutil
        shutil.rmtree(SANDBOX_SSOT)
    SANDBOX_SSOT.mkdir(parents=True)

    store = SSOTStore(SANDBOX_SSOT)

    # Create initial task
    initial_task = {
        "id": TASK_ID,
        "status": "pending",
        "request_text": "chaos race test",
        "locked_marks": [],
        "bare_marks": [],
    }
    store.save_task(initial_task)

    # Spawn threads: half locked, half bare
    threads = []
    for i in range(NUM_WRITERS // 2):
        threads.append(threading.Thread(target=locked_writer, args=(store, i), name=f"locked-{i}"))
    for i in range(NUM_WRITERS // 2):
        threads.append(threading.Thread(target=bare_writer, args=(store, i + NUM_WRITERS // 2), name=f"bare-{i}"))

    print(f"Starting {NUM_WRITERS} concurrent writers ({NUM_WRITERS//2} locked, {NUM_WRITERS//2} bare)")
    print(f"Each writes {WRITES_PER_THREAD} times = {NUM_WRITERS * WRITES_PER_THREAD} total expected marks")

    start = time.monotonic()
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    elapsed = time.monotonic() - start

    # Read final state
    final_task = store.get_task(TASK_ID)
    locked_marks = final_task.get("locked_marks", [])
    bare_marks = final_task.get("bare_marks", [])

    expected_locked = (NUM_WRITERS // 2) * WRITES_PER_THREAD
    expected_bare = (NUM_WRITERS // 2) * WRITES_PER_THREAD

    print(f"\n{'='*60}")
    print(f"RESULTS (elapsed: {elapsed:.2f}s)")
    print(f"{'='*60}")
    print(f"Locked marks: {len(locked_marks)}/{expected_locked} ({len(locked_marks)/expected_locked*100:.1f}%)")
    print(f"Bare marks:   {len(bare_marks)}/{expected_bare} ({len(bare_marks)/expected_bare*100:.1f}%)")
    print(f"Total marks:  {len(locked_marks) + len(bare_marks)}/{expected_locked + expected_bare}")
    print(f"Errors:       {len(results['errors'])}")

    data_loss = (expected_locked + expected_bare) - (len(locked_marks) + len(bare_marks))
    if data_loss > 0:
        print(f"\n!! DATA LOSS DETECTED: {data_loss} marks lost ({data_loss/(expected_locked+expected_bare)*100:.1f}%)")
        print(f"   Locked loss: {expected_locked - len(locked_marks)}")
        print(f"   Bare loss:   {expected_bare - len(bare_marks)}")
        print(f"\nVERDICT: RACE CONDITION CONFIRMED — bare save_task causes data loss under concurrency")
    else:
        print(f"\nVERDICT: No data loss detected (may need more threads/writes to trigger)")

    if results["errors"]:
        print(f"\nFirst 5 errors:")
        for e in results["errors"][:5]:
            print(f"  - {e}")

    # Cleanup
    import shutil
    shutil.rmtree(SANDBOX_SSOT)

    return 1 if data_loss > 0 else 0


if __name__ == "__main__":
    raise SystemExit(main())
