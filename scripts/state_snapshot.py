#!/usr/bin/env python3
from __future__ import annotations

import json
from typing import Any

try:
    from pointer_protocol import ref_to_artifact_entry, write_json_artifact, write_text_artifact
except ImportError:  # pragma: no cover - package import fallback
    from scripts.pointer_protocol import ref_to_artifact_entry, write_json_artifact, write_text_artifact

SNAPSHOT_MAX_CHARS = max(1024, int((__import__('os').environ.get('AGN_SNAPSHOT_MAX_CHARS', '4096') or '4096')))
FIELD_MAX_CHARS = max(256, int((__import__('os').environ.get('AGN_SNAPSHOT_FIELD_MAX_CHARS', '1024') or '1024')))


def _json_len(value: Any) -> int:
    try:
        return len(json.dumps(value, ensure_ascii=True, sort_keys=True))
    except Exception:
        return len(str(value))


def _collect_refs(value: Any) -> list[str]:
    refs: list[str] = []
    if isinstance(value, str):
        if value.startswith('agn://'):
            refs.append(value)
        return refs
    if isinstance(value, list):
        for item in value:
            refs.extend(_collect_refs(item))
        return refs
    if isinstance(value, dict):
        for item in value.values():
            refs.extend(_collect_refs(item))
        return refs
    return refs


def _compact_task_spec(task: dict[str, Any]) -> dict[str, Any]:
    criteria = task.get('acceptance_criteria')
    criteria_count = len(criteria) if isinstance(criteria, list) else 0
    runner_cmd_raw = task.get('runner_cmd')
    runner_cmd: list[str] = []
    if isinstance(runner_cmd_raw, list):
        for item in runner_cmd_raw[:8]:
            runner_cmd.append(str(item)[:160])
    spec = {
        'task_kind': str(task.get('task_kind', '')).strip(),
        'risk_level': str(task.get('risk_level', 'low')).strip(),
        'side_effect_level': str(task.get('side_effect_level', 'read_only')).strip(),
        'review_requested': bool(task.get('review_requested', True)),
        'executor_provider': str(task.get('executor_provider', 'codex')).strip(),
        'reviewer_provider': str(task.get('reviewer_provider', 'gemini')).strip(),
        'repo_id': str(task.get('repo_id', 'main')).strip() or 'main',
        'repo_ref': str(task.get('repo_ref', '')).strip(),
        'work_branch': str(task.get('work_branch', '')).strip(),
        'attempt': int(task.get('attempt', 1) or 1),
        'runner_cmd': runner_cmd,
        'context_read_path': str(task.get('context_read_path', 'README.md')).strip() or 'README.md',
        'request_summary': str(task.get('request_summary', '')).strip()[:480],
        'request_text_ref': str(task.get('request_text_ref', '')).strip(),
        'acceptance_spec_ref': str(task.get('acceptance_spec_ref', '')).strip(),
        'criteria_count': criteria_count,
        'needs_context_read': bool(task.get('needs_context_read', False)),
        'research_axis': str(task.get('research_axis', '')).strip(),
        'question': str(task.get('question', '')).strip()[:480],
        'hypothesis': str(task.get('hypothesis', '')).strip()[:480],
        'baseline': str(task.get('baseline', '')).strip()[:320],
        'single_change': str(task.get('single_change', '')).strip()[:320],
        'budget': task.get('budget', {}),
        'round': int(task.get('round', 0) or 0),
        'proposal_version': int(task.get('proposal_version', 0) or 0),
        'decision_mode': str(task.get('decision_mode', '')).strip(),
        'failure_mode_allowed': bool(task.get('failure_mode_allowed', False)),
    }
    # Explicitly exclude request_text to keep snapshot ref-only for large payloads.
    return spec


def _compact_checkpoint(checkpoint: dict[str, Any]) -> dict[str, Any]:
    allowed = {
        'state',
        'state_reason',
        'updated_at',
        'last_event_time',
        'exec_action_id',
        'review_action_id',
        'read_context_action_id',
        'context_loaded',
        'paused',
        'spec_revision',
        'last_control_type',
        'protocol_blocked',
        'protocol_block_reason',
        'governance_ready',
        'governance_missing',
        'completion_ready',
        'admin_delivery_status',
        'final_report_ref',
        'empirical_execution',
        'truthfulness_status',
        'truthfulness_reason',
        'awaiting_admin_response',
        'admin_hold_reason',
        'admin_hold_until',
        'daily_brief_deadline',
    }
    out: dict[str, Any] = {}
    for key in sorted(allowed):
        if key in checkpoint:
            out[key] = checkpoint.get(key)
    return out


def _compact_pending_actions(pending_actions: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for action in pending_actions[:20]:
        if not isinstance(action, dict):
            continue
        compact.append(
            {
                'action_id': str(action.get('action_id', '')).strip(),
                'action_type': str(action.get('action_type', '')).strip(),
                'state_hint': str(action.get('state_hint', '')).strip(),
                'created_at': str(action.get('created_at', '')).strip(),
            }
        )
    return compact


def _compact_digest_for_snapshot(digests: list[dict[str, Any]]) -> list[dict[str, Any]]:
    compact: list[dict[str, Any]] = []
    for item in digests[-12:]:
        if not isinstance(item, dict):
            continue
        compact.append(
            {
                'event_id': str(item.get('event_id', '')).strip(),
                'event_type': str(item.get('event_type', '')).strip(),
                'action_id': str(item.get('action_id', '')).strip(),
                'ts': str(item.get('ts', '')).strip(),
            }
        )
    return compact


def build_ref_index(
    *,
    task: dict[str, Any],
    checkpoint: dict[str, Any],
    recent_event_digests: list[dict[str, Any]],
    limit: int = 64,
) -> list[dict[str, Any]]:
    entries: list[dict[str, Any]] = []

    def _add(ref: str, source: str) -> None:
        clean = str(ref).strip()
        if not clean.startswith('agn://'):
            return
        entries.append({'ref': clean, 'source': source})

    _add(str(task.get('request_text_ref', '')).strip(), 'task.request_text_ref')
    _add(str(task.get('task_spec_ref', '')).strip(), 'task.task_spec_ref')
    _add(str(task.get('repo_ref', '')).strip(), 'task.repo_ref')

    for key, value in checkpoint.items():
        if isinstance(value, str) and value.startswith('agn://'):
            _add(value, f'checkpoint.{key}')

    for digest in recent_event_digests[-50:]:
        refs = digest.get('refs')
        if isinstance(refs, list):
            for ref in refs:
                _add(str(ref), f"event.{digest.get('event_id', '')}")

    uniq: dict[str, dict[str, Any]] = {}
    for entry in entries:
        uniq.setdefault(entry['ref'], entry)
    return list(uniq.values())[: max(1, int(limit))]


def _offload_value(
    *,
    task_id: str,
    attempt: int,
    field_name: str,
    value: Any,
) -> dict[str, Any]:
    if isinstance(value, str):
        artifact = write_text_artifact(
            task_id=task_id,
            attempt=attempt,
            artifact_id=f'snapshot_{field_name}',
            content=value,
            media_type='text/plain',
            filename=f'snapshot_{field_name}.txt',
            source='state_snapshot',
        )
        return {'ref': artifact.ref, 'bytes': artifact.bytes, 'field': field_name}

    artifact = write_json_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id=f'snapshot_{field_name}',
        payload={'field': field_name, 'value': value},
        filename=f'snapshot_{field_name}.json',
        source='state_snapshot',
    )
    return {'ref': artifact.ref, 'bytes': artifact.bytes, 'field': field_name}


def build_state_snapshot(
    *,
    trace_id: str,
    task_id: str,
    attempt: int,
    task: dict[str, Any],
    checkpoint: dict[str, Any],
    pending_actions: list[dict[str, Any]],
    recent_event_digests: list[dict[str, Any]],
    ref_index: list[dict[str, Any]],
    perf_limits: dict[str, Any],
) -> tuple[dict[str, Any], str]:
    snapshot: dict[str, Any] = {
        'snapshot_version': 'evo4',
        'trace_id': str(trace_id).strip(),
        'task_id': str(task_id).strip(),
        'state': str(checkpoint.get('state', 'CREATED')).strip().upper() or 'CREATED',
        'paused': bool(checkpoint.get('paused', False)),
        'checkpoint': _compact_checkpoint(checkpoint),
        'task_spec': _compact_task_spec(task),
        'pending_actions': _compact_pending_actions(pending_actions),
        'recent_events_digest': _compact_digest_for_snapshot(recent_event_digests),
        'ref_index': ref_index[:64],
        'perf_budget': {
            'max_time_sec': float(perf_limits.get('max_time_sec', 900) or 900),
            'max_disk_mb': float(perf_limits.get('max_disk_mb', 512) or 512),
            'max_log_kb': float(perf_limits.get('max_log_kb', 512) or 512),
        },
        'limits': {
            'field_max_chars': FIELD_MAX_CHARS,
            'snapshot_max_chars': SNAPSHOT_MAX_CHARS,
        },
    }

    offloaded: list[dict[str, Any]] = []
    for field in ('task_spec', 'pending_actions', 'recent_events_digest', 'ref_index'):
        value = snapshot.get(field)
        if _json_len(value) <= FIELD_MAX_CHARS:
            continue
        meta = _offload_value(task_id=task_id, attempt=attempt, field_name=field, value=value)
        offloaded.append(meta)
        snapshot[field] = {'ref': meta['ref'], 'omitted': True, 'bytes': meta['bytes']}

    rendered_len = _json_len(snapshot)
    snapshot_ref = ''
    if rendered_len > SNAPSHOT_MAX_CHARS:
        artifact = write_json_artifact(
            task_id=task_id,
            attempt=attempt,
            artifact_id='state_snapshot_full',
            payload=snapshot,
            filename='state_snapshot_full.json',
            source='state_snapshot',
        )
        snapshot_ref = artifact.ref
        snapshot = {
            'snapshot_version': 'evo4',
            'trace_id': str(trace_id).strip(),
            'task_id': str(task_id).strip(),
            'state': str(checkpoint.get('state', 'CREATED')).strip().upper() or 'CREATED',
            'paused': bool(checkpoint.get('paused', False)),
            'snapshot_ref': snapshot_ref,
            'omitted': True,
            'offloaded_fields': offloaded,
            'ref_index': ref_index[:32],
            'perf_budget': snapshot['perf_budget'],
            'limits': snapshot['limits'],
        }
    else:
        snapshot['offloaded_fields'] = offloaded

    # Defensive scrub: remove any accidental inline long text fields.
    if 'request_text' in snapshot:
        snapshot.pop('request_text', None)

    # Keep discoverable refs for backend.
    refs = sorted(set(_collect_refs(snapshot)))
    snapshot['ref_count'] = len(refs)
    return snapshot, snapshot_ref
