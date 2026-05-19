from __future__ import annotations

import json
from typing import Any

from scripts.low_token_tools.common import ToolSpec


def _non_empty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _list_of_strings(value: Any) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _require_fields(payload: dict[str, Any], fields: list[str]) -> list[str]:
    missing: list[str] = []
    for field in fields:
        if field not in payload:
            missing.append(f"missing:{field}")
            continue
        value = payload.get(field)
        if isinstance(value, str) and not value.strip():
            missing.append(f"empty:{field}")
        elif value is None:
            missing.append(f"empty:{field}")
    return missing


def _build_prompt(*, tool_name: str, purpose: str, rules: list[str], input_schema: dict[str, Any], output_schema: dict[str, Any], payload: dict[str, Any]) -> str:
    lines = [
        f"Tool name: {tool_name}",
        f"Purpose: {purpose}",
        "You are a bounded transformation worker, not a planner.",
        "Honor the explicit schema and do not add extra top-level keys.",
        "If source data is missing or unclear, use the schema's empty value and record it in warnings or missing fields.",
        "Rules:",
    ]
    for rule in rules:
        lines.append(f"- {rule}")
    lines.extend(
        [
            "",
            "Input schema summary:",
            json.dumps(input_schema, ensure_ascii=True, indent=2),
            "",
            "Required output schema:",
            json.dumps(output_schema, ensure_ascii=True, indent=2),
            "",
            "Input payload:",
            json.dumps(payload, ensure_ascii=True, indent=2),
            "",
            "Return exactly one JSON object matching the output schema.",
        ]
    )
    return "\n".join(lines)


def _validate_text_normalizer_input(payload: dict[str, Any]) -> list[str]:
    errors = _require_fields(payload, ["text", "normalization_profile"])
    profile = payload.get("normalization_profile")
    if not isinstance(profile, dict):
        errors.append("normalization_profile_must_be_object")
    return errors


def _validate_text_normalizer_output(payload: dict[str, Any]) -> list[str]:
    errors = _require_fields(payload, ["normalized_text", "applied_rules", "warnings"])
    if not _non_empty_string(payload.get("normalized_text")):
        errors.append("normalized_text_must_be_non_empty_string")
    if not _list_of_strings(payload.get("applied_rules")):
        errors.append("applied_rules_must_be_string_list")
    if not _list_of_strings(payload.get("warnings")):
        errors.append("warnings_must_be_string_list")
    return errors


TEXT_NORMALIZER = ToolSpec(
    name="text_normalizer",
    description="Normalize messy text into a consistent style without broad rewriting.",
    input_schema={
        "text": "string",
        "normalization_profile": {
            "style": "professional_sentence|compact_note|title_case|slug",
            "preserve_case_for": ["string"],
            "strip_redundant_whitespace": "boolean",
            "expand_sms_shorthand": "boolean",
        },
    },
    output_schema={
        "normalized_text": "string",
        "applied_rules": ["string"],
        "warnings": ["string"],
    },
    sample_list_fields=(),
    prompt_builder=lambda payload: _build_prompt(
        tool_name="text_normalizer",
        purpose="Normalize formatting, spacing, and bounded wording while preserving meaning.",
        rules=[
            "Do not add new facts.",
            "Keep named entities intact unless the input clearly contains OCR noise.",
            "Keep the rewrite compact and deterministic.",
        ],
        input_schema=TEXT_NORMALIZER.input_schema,
        output_schema=TEXT_NORMALIZER.output_schema,
        payload=payload,
    ),
    validate_input=_validate_text_normalizer_input,
    validate_output=_validate_text_normalizer_output,
)


def _validate_json_extractor_input(payload: dict[str, Any]) -> list[str]:
    errors = _require_fields(payload, ["text", "fields"])
    fields = payload.get("fields")
    if not isinstance(fields, list) or not fields:
        errors.append("fields_must_be_non_empty_list")
        return errors
    for idx, field in enumerate(fields):
        if not isinstance(field, dict):
            errors.append(f"fields[{idx}]_must_be_object")
            continue
        if not _non_empty_string(field.get("name")):
            errors.append(f"fields[{idx}].name_required")
        if not _non_empty_string(field.get("type")):
            errors.append(f"fields[{idx}].type_required")
    return errors


def _validate_json_extractor_output(payload: dict[str, Any]) -> list[str]:
    errors = _require_fields(payload, ["record", "missing_required_fields", "warnings"])
    if not isinstance(payload.get("record"), dict):
        errors.append("record_must_be_object")
    if not _list_of_strings(payload.get("missing_required_fields")):
        errors.append("missing_required_fields_must_be_string_list")
    if not _list_of_strings(payload.get("warnings")):
        errors.append("warnings_must_be_string_list")
    return errors


JSON_EXTRACTOR = ToolSpec(
    name="json_extractor",
    description="Extract fixed fields from raw text into one JSON record.",
    input_schema={
        "text": "string",
        "fields": [
            {
                "name": "string",
                "type": "string|number|date|boolean",
                "required": "boolean",
                "description": "string",
            }
        ],
    },
    output_schema={
        "record": {"field_name": "value"},
        "missing_required_fields": ["string"],
        "warnings": ["string"],
    },
    sample_list_fields=("fields",),
    prompt_builder=lambda payload: _build_prompt(
        tool_name="json_extractor",
        purpose="Extract only the requested fields from raw text into a single JSON object.",
        rules=[
            "Do not invent values that are absent.",
            "Use null for missing values inside record.",
            "Dates must be ISO-8601 strings when present.",
        ],
        input_schema=JSON_EXTRACTOR.input_schema,
        output_schema=JSON_EXTRACTOR.output_schema,
        payload=payload,
    ),
    validate_input=_validate_json_extractor_input,
    validate_output=_validate_json_extractor_output,
)


def _validate_label_normalizer_input(payload: dict[str, Any]) -> list[str]:
    errors = _require_fields(payload, ["items", "allowed_labels"])
    if not _list_of_strings(payload.get("items")) or not payload.get("items"):
        errors.append("items_must_be_non_empty_string_list")
    if not _list_of_strings(payload.get("allowed_labels")) or not payload.get("allowed_labels"):
        errors.append("allowed_labels_must_be_non_empty_string_list")
    return errors


def _validate_label_normalizer_output(payload: dict[str, Any]) -> list[str]:
    errors = _require_fields(payload, ["normalized_items", "unknown_items", "allowed_labels_used"])
    normalized_items = payload.get("normalized_items")
    if not isinstance(normalized_items, list):
        errors.append("normalized_items_must_be_list")
    else:
        for idx, item in enumerate(normalized_items):
            if not isinstance(item, dict):
                errors.append(f"normalized_items[{idx}]_must_be_object")
                continue
            if not _non_empty_string(item.get("source")):
                errors.append(f"normalized_items[{idx}].source_required")
            if not _non_empty_string(item.get("status")):
                errors.append(f"normalized_items[{idx}].status_required")
    if not _list_of_strings(payload.get("unknown_items")):
        errors.append("unknown_items_must_be_string_list")
    if not _list_of_strings(payload.get("allowed_labels_used")):
        errors.append("allowed_labels_used_must_be_string_list")
    return errors


LABEL_NORMALIZER = ToolSpec(
    name="label_normalizer",
    description="Map messy labels into a bounded allowed label set.",
    input_schema={
        "items": ["string"],
        "allowed_labels": ["string"],
        "rules": {
            "case_insensitive": "boolean",
            "allow_partial_match": "boolean",
            "prefer_exact_allowed_labels": "boolean",
        },
    },
    output_schema={
        "normalized_items": [
            {
                "source": "string",
                "normalized_label": "string",
                "status": "mapped|unknown",
            }
        ],
        "unknown_items": ["string"],
        "allowed_labels_used": ["string"],
    },
    sample_list_fields=("items",),
    prompt_builder=lambda payload: _build_prompt(
        tool_name="label_normalizer",
        purpose="Map noisy labels into the provided allowed label set.",
        rules=[
            "Never output a normalized_label outside allowed_labels except empty string for unknown.",
            "Keep the number of normalized_items equal to the number of input items.",
            "Use status unknown when no safe mapping exists.",
        ],
        input_schema=LABEL_NORMALIZER.input_schema,
        output_schema=LABEL_NORMALIZER.output_schema,
        payload=payload,
    ),
    validate_input=_validate_label_normalizer_input,
    validate_output=_validate_label_normalizer_output,
)


def _validate_ocr_cleaner_input(payload: dict[str, Any]) -> list[str]:
    errors = _require_fields(payload, ["text", "instructions"])
    if not _list_of_strings(payload.get("instructions")):
        errors.append("instructions_must_be_string_list")
    return errors


def _validate_ocr_cleaner_output(payload: dict[str, Any]) -> list[str]:
    errors = _require_fields(payload, ["cleaned_text", "uncertain_tokens", "warnings"])
    if not _non_empty_string(payload.get("cleaned_text")):
        errors.append("cleaned_text_must_be_non_empty_string")
    if not _list_of_strings(payload.get("uncertain_tokens")):
        errors.append("uncertain_tokens_must_be_string_list")
    if not _list_of_strings(payload.get("warnings")):
        errors.append("warnings_must_be_string_list")
    return errors


OCR_POST_CLEANER = ToolSpec(
    name="ocr_post_cleaner",
    description="Clean OCR-style text while preserving layout as much as possible.",
    input_schema={
        "text": "string",
        "instructions": ["string"],
        "preserve_line_breaks": "boolean",
    },
    output_schema={
        "cleaned_text": "string",
        "uncertain_tokens": ["string"],
        "warnings": ["string"],
    },
    sample_list_fields=("instructions",),
    prompt_builder=lambda payload: _build_prompt(
        tool_name="ocr_post_cleaner",
        purpose="Fix obvious OCR corruption without inventing missing content.",
        rules=[
            "Preserve original line ordering.",
            "Correct obvious O/0, I/1, rn/m style OCR confusions when strongly supported.",
            "When uncertain, keep the original token and list it under uncertain_tokens.",
        ],
        input_schema=OCR_POST_CLEANER.input_schema,
        output_schema=OCR_POST_CLEANER.output_schema,
        payload=payload,
    ),
    validate_input=_validate_ocr_cleaner_input,
    validate_output=_validate_ocr_cleaner_output,
)


def _validate_batch_record_cleaner_input(payload: dict[str, Any]) -> list[str]:
    errors = _require_fields(payload, ["records", "target_fields", "rules"])
    records = payload.get("records")
    target_fields = payload.get("target_fields")
    if not isinstance(records, list) or not records:
        errors.append("records_must_be_non_empty_list")
    if not isinstance(target_fields, list) or not target_fields:
        errors.append("target_fields_must_be_non_empty_list")
    else:
        for idx, field in enumerate(target_fields):
            if not isinstance(field, dict):
                errors.append(f"target_fields[{idx}]_must_be_object")
                continue
            if not _non_empty_string(field.get("name")):
                errors.append(f"target_fields[{idx}].name_required")
    if not isinstance(payload.get("rules"), dict):
        errors.append("rules_must_be_object")
    return errors


def _validate_batch_record_cleaner_output(payload: dict[str, Any]) -> list[str]:
    errors = _require_fields(payload, ["cleaned_records", "rejected_records", "summary"])
    if not isinstance(payload.get("cleaned_records"), list):
        errors.append("cleaned_records_must_be_list")
    if not isinstance(payload.get("rejected_records"), list):
        errors.append("rejected_records_must_be_list")
    summary = payload.get("summary")
    if not isinstance(summary, dict):
        errors.append("summary_must_be_object")
    else:
        for field in ("input_count", "cleaned_count", "rejected_count"):
            if not isinstance(summary.get(field), int):
                errors.append(f"summary.{field}_must_be_int")
    return errors


BATCH_RECORD_CLEANER = ToolSpec(
    name="batch_record_cleaner",
    description="Clean and normalize batches of records into a fixed schema.",
    input_schema={
        "records": [{"field": "value"}],
        "target_fields": [
            {
                "name": "string",
                "type": "string|email|phone|label|date",
                "empty_value": "string|null",
            }
        ],
        "rules": {
            "normalize_phone_e164": "boolean",
            "trim_whitespace": "boolean",
            "allowed_labels": ["string"],
        },
    },
    output_schema={
        "cleaned_records": [{"field": "value"}],
        "rejected_records": [{"index": 0, "reason": "string"}],
        "summary": {
            "input_count": 0,
            "cleaned_count": 0,
            "rejected_count": 0,
        },
    },
    sample_list_fields=("records",),
    prompt_builder=lambda payload: _build_prompt(
        tool_name="batch_record_cleaner",
        purpose="Normalize each record into the target field schema and reject records only when they are unusable.",
        rules=[
            "Keep cleaned_records in the same order as accepted input records.",
            "Do not add fields outside target_fields.",
            "Use empty_value when a field cannot be safely derived.",
            "Put unusable records into rejected_records with the original index and a short reason.",
        ],
        input_schema=BATCH_RECORD_CLEANER.input_schema,
        output_schema=BATCH_RECORD_CLEANER.output_schema,
        payload=payload,
    ),
    validate_input=_validate_batch_record_cleaner_input,
    validate_output=_validate_batch_record_cleaner_output,
)


TOOL_SPECS = {
    TEXT_NORMALIZER.name: TEXT_NORMALIZER,
    JSON_EXTRACTOR.name: JSON_EXTRACTOR,
    LABEL_NORMALIZER.name: LABEL_NORMALIZER,
    OCR_POST_CLEANER.name: OCR_POST_CLEANER,
    BATCH_RECORD_CLEANER.name: BATCH_RECORD_CLEANER,
}
