from __future__ import annotations

import json
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS_DIR = ROOT / "scripts"
if str(SCRIPTS_DIR) not in sys.path:
    sys.path.insert(0, str(SCRIPTS_DIR))

from pointer_protocol import (
    TASKS_DIR,
    parse_ref,
    read_ref_text,
    ref_to_artifact_entry,
    resolve_ref_path,
    search_ref_text,
    write_file_artifact,
    write_text_artifact,
)


def test_pointer_protocol_roundtrip_text_artifact() -> None:
    task_id = "test-pointer-roundtrip"
    attempt = 1

    ref = write_text_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id="error_trace",
        content="line-1\nline-2\nline-3\nERROR boom\n",
        media_type="text/plain",
        filename="error_trace.log",
        source="test",
    )

    parsed = parse_ref(ref.ref)
    assert parsed["task_id"] == task_id
    assert int(parsed["attempt"]) == attempt

    resolved = resolve_ref_path(ref.ref)
    assert resolved.exists()
    assert resolved.name == "error_trace.log"

    tail = read_ref_text(ref.ref, mode="tail", tail_lines=2)
    assert "ERROR boom" in tail

    matches = search_ref_text(ref.ref, pattern="ERROR", max_matches=5)
    assert len(matches) == 1
    assert matches[0]["line"] == 4

    entry = ref_to_artifact_entry(ref)
    assert entry["artifact_id"] == "error_trace"
    assert entry["ref"] == ref.ref


def test_manifest_written_for_attempt() -> None:
    task_id = "test-pointer-manifest"
    attempt = 2

    _ = write_text_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id="instructions",
        content="# test\n",
        media_type="text/markdown",
        filename="instructions.md",
        source="test",
    )

    manifest_path = TASKS_DIR / task_id / f"attempt_{attempt}" / "manifest.json"
    assert manifest_path.exists()
    payload = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert isinstance(payload.get("artifacts"), dict)
    assert "instructions" in payload["artifacts"]


def test_pointer_protocol_roundtrip_file_artifact(tmp_path: Path) -> None:
    task_id = "test-pointer-file"
    attempt = 1
    source = tmp_path / "sample.bin"
    source.write_bytes(b"\x89PNG\r\nfake")

    ref = write_file_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id="sample_binary",
        source_path=source,
        filename="sample.bin",
        media_type="application/octet-stream",
        source="test",
    )

    resolved = resolve_ref_path(ref.ref)
    assert resolved.exists()
    assert resolved.read_bytes() == b"\x89PNG\r\nfake"
    assert ref.media_type == "application/octet-stream"
