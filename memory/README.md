# AGN Multi-Instance Memory

This directory is a git-synced memory plane for running AGN on multiple machines.

Layout:
- `events/<instance_id>/<YYYY-MM-DD>.jsonl`: append-only memory events (source of truth).
- `state/<instance_id>.json`: local logical clock per instance for deterministic event ordering.
- `instances/<instance_id>.json`: instance metadata and heartbeat marker.
- `conflicts/<instance_id>.json`: latest conflict snapshot after LWW merge.

Merge policy:
- Last-write-wins (`lww`) over `(ts, logical_clock, instance_id, event_id)`.
- Conflicts are preserved in `conflicts/<instance_id>.json` for audit/review.

Notes:
- Keep writes append-only in `events/`.
- Do not hand-edit event logs unless repairing corruption.
