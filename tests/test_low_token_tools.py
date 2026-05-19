from __future__ import annotations

from scripts.low_token_tools.common import _apply_sample
from scripts.low_token_tools.tool_specs import BATCH_RECORD_CLEANER, LABEL_NORMALIZER


def test_apply_sample_truncates_known_list_fields() -> None:
    payload = {"records": [{"id": 1}, {"id": 2}, {"id": 3}]}
    sampled, applied = _apply_sample(payload, ("records",), 2)
    assert applied is True
    assert sampled["records"] == [{"id": 1}, {"id": 2}]
    assert payload["records"] == [{"id": 1}, {"id": 2}, {"id": 3}]


def test_label_normalizer_requires_allowed_labels() -> None:
    errors = LABEL_NORMALIZER.validate_input({"items": ["Bio"], "rules": {}})
    assert "missing:allowed_labels" in errors


def test_batch_record_cleaner_requires_target_fields() -> None:
    errors = BATCH_RECORD_CLEANER.validate_input({"records": [{"x": 1}], "rules": {}})
    assert "missing:target_fields" in errors
