#!/usr/bin/env python3
from __future__ import annotations

import argparse
from collections import Counter
import csv
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
from typing import Any

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "src") not in sys.path:
    sys.path.insert(0, str(ROOT / "src"))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from pointer_protocol import resolve_ref_path, write_file_artifact, write_json_artifact, write_text_artifact
from agn.handlers.visual_security import (
    build_security_scan,
    detect_sensitive_ocr_text,
    redact_sensitive_ocr_text,
    sanitize_ocr_words,
)
from agn_handler_cli_guard import render_direct_handler_cli_block, should_block_direct_handler_cli


def _run_command(cmd: list[str]) -> tuple[int, str, str]:
    # P3-BUG-FIX: use bytes mode and decode with errors="replace" to prevent
    # UnicodeDecodeError when tesseract/sips write non-UTF-8 to stderr.
    # On Python 3.14 with text=True, binary stderr crashes the pipeline.
    completed = subprocess.run(
        cmd,
        cwd=str(ROOT),
        capture_output=True,
        timeout=60.0,
        check=False,
    )
    stdout = completed.stdout.decode("utf-8", errors="replace") if completed.stdout else ""
    stderr = completed.stderr.decode("utf-8", errors="replace") if completed.stderr else ""
    return int(completed.returncode), stdout, stderr


def _require_vision_dependencies() -> None:
    missing = [name for name in ("sips", "tesseract") if not shutil.which(name)]
    if missing:
        raise RuntimeError(f"vision_dependencies_missing:{','.join(missing)}")


def _resolve_image_ref(ref: str) -> Path:
    clean = str(ref or "").strip()
    if not clean.startswith("agn://artifact/"):
        raise ValueError("vision_input_must_be_pointer_ref")
    path = resolve_ref_path(clean)
    if not path.exists() or not path.is_file():
        raise ValueError(f"vision_input_missing:{path}")
    return path


def _image_dimensions(path: Path) -> tuple[int, int]:
    rc, stdout, _stderr = _run_command(["sips", "-g", "pixelWidth", "-g", "pixelHeight", str(path)])
    if rc != 0:
        return 0, 0
    width = 0
    height = 0
    for line in stdout.splitlines():
        text = line.strip()
        if "pixelWidth:" in text:
            try:
                width = int(text.split(":", 1)[1].strip())
            except ValueError:
                width = 0
        if "pixelHeight:" in text:
            try:
                height = int(text.split(":", 1)[1].strip())
            except ValueError:
                height = 0
    return width, height


def _ocr_words(path: Path) -> list[dict[str, Any]]:
    rc, stdout, _stderr = _run_command(["tesseract", str(path), "stdout", "tsv"])
    if rc != 0 or not stdout.strip():
        return []
    reader = csv.DictReader(io.StringIO(stdout), delimiter="\t")
    words: list[dict[str, Any]] = []
    for row in reader:
        text = str(row.get("text", "")).strip()
        if not text:
            continue
        try:
            conf = float(str(row.get("conf", "-1")).strip() or "-1")
        except ValueError:
            conf = -1.0
        if conf < 0:
            continue
        def _int_field(field: str) -> int:
            try:
                return int(str(row.get(field, "0")).strip() or 0)
            except (ValueError, TypeError):
                return 0

        words.append(
            {
                "text": text,
                "confidence": conf,
                "left": _int_field("left"),
                "top": _int_field("top"),
                "width": _int_field("width"),
                "height": _int_field("height"),
                "line_num": _int_field("line_num"),
                "block_num": _int_field("block_num"),
            }
        )
    return words


def _entities(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    counter = Counter(str(item.get("text", "")).strip().lower() for item in words if str(item.get("text", "")).strip())
    entities: list[dict[str, Any]] = []
    for token, count in counter.most_common(20):
        entities.append({"value": token, "count": count})
    return entities


def _ocr_text(words: list[dict[str, Any]]) -> str:
    ordered = sorted(
        words,
        key=lambda item: (
            int(item.get("block_num", 0) or 0),
            int(item.get("line_num", 0) or 0),
            int(item.get("left", 0) or 0),
        ),
    )
    lines: list[str] = []
    current_key: tuple[int, int] | None = None
    current_tokens: list[str] = []
    for item in ordered:
        key = (int(item.get("block_num", 0) or 0), int(item.get("line_num", 0) or 0))
        if current_key is None:
            current_key = key
        if key != current_key:
            if current_tokens:
                lines.append(" ".join(current_tokens))
            current_tokens = []
            current_key = key
        token = str(item.get("text", "")).strip()
        if token:
            current_tokens.append(token)
    if current_tokens:
        lines.append(" ".join(current_tokens))
    return "\n".join(lines).strip() + ("\n" if lines else "")


def _regions(words: list[dict[str, Any]]) -> list[dict[str, Any]]:
    regions: list[dict[str, Any]] = []
    for idx, item in enumerate(words):
        regions.append(
            {
                "id": f"word-{idx+1}",
                "kind": "ocr_word",
                "text": str(item.get("text", "")).strip(),
                "bounds": {
                    "left": int(item.get("left", 0) or 0),
                    "top": int(item.get("top", 0) or 0),
                    "width": int(item.get("width", 0) or 0),
                    "height": int(item.get("height", 0) or 0),
                },
                "confidence": float(item.get("confidence", 0.0) or 0.0),
            }
        )
    return regions


def _ui_tree(words: list[dict[str, Any]], *, width: int, height: int) -> dict[str, Any]:
    blocks: dict[int, dict[str, Any]] = {}
    for item in words:
        block_num = int(item.get("block_num", 0) or 0)
        line_num = int(item.get("line_num", 0) or 0)
        text = str(item.get("text", "")).strip()
        if not text:
            continue
        left = int(item.get("left", 0) or 0)
        top = int(item.get("top", 0) or 0)
        w = int(item.get("width", 0) or 0)
        h = int(item.get("height", 0) or 0)
        block = blocks.setdefault(
            block_num,
            {"block_num": block_num, "kind": "ocr_block", "lines": {}, "bounds": {"left": left, "top": top, "right": left + w, "bottom": top + h}},
        )
        bounds = block["bounds"]
        bounds["left"] = min(int(bounds["left"]), left)
        bounds["top"] = min(int(bounds["top"]), top)
        bounds["right"] = max(int(bounds["right"]), left + w)
        bounds["bottom"] = max(int(bounds["bottom"]), top + h)
        line = block["lines"].setdefault(
            line_num,
            {"line_num": line_num, "kind": "ocr_line", "tokens": [], "bounds": {"left": left, "top": top, "right": left + w, "bottom": top + h}},
        )
        line_bounds = line["bounds"]
        line_bounds["left"] = min(int(line_bounds["left"]), left)
        line_bounds["top"] = min(int(line_bounds["top"]), top)
        line_bounds["right"] = max(int(line_bounds["right"]), left + w)
        line_bounds["bottom"] = max(int(line_bounds["bottom"]), top + h)
        line["tokens"].append(
            {
                "text": text,
                "confidence": float(item.get("confidence", 0.0) or 0.0),
                "bounds": {"left": left, "top": top, "width": w, "height": h},
            }
        )
    children: list[dict[str, Any]] = []
    for block_num in sorted(blocks):
        block = blocks[block_num]
        lines: list[dict[str, Any]] = []
        for line_num in sorted(block["lines"]):
            line = block["lines"][line_num]
            line["text"] = " ".join(token["text"] for token in line["tokens"]).strip()
            line["bounds"] = {
                "left": int(line["bounds"]["left"]),
                "top": int(line["bounds"]["top"]),
                "width": max(0, int(line["bounds"]["right"]) - int(line["bounds"]["left"])),
                "height": max(0, int(line["bounds"]["bottom"]) - int(line["bounds"]["top"])),
            }
            lines.append(line)
        block["text"] = "\n".join(line["text"] for line in lines if str(line.get("text", "")).strip()).strip()
        block["bounds"] = {
            "left": int(block["bounds"]["left"]),
            "top": int(block["bounds"]["top"]),
            "width": max(0, int(block["bounds"]["right"]) - int(block["bounds"]["left"])),
            "height": max(0, int(block["bounds"]["bottom"]) - int(block["bounds"]["top"])),
        }
        block["children"] = lines
        del block["lines"]
        children.append(block)
    return {
        "kind": "ui_tree",
        "bounds": {"left": 0, "top": 0, "width": width, "height": height},
        "children": children,
    }


def register_image_path(
    *,
    task_id: str,
    attempt: int,
    image_path: str,
    artifact_id: str = "vision_input",
    source: str = "vision_parser_cli",
) -> dict[str, Any]:
    path = Path(str(image_path)).expanduser().resolve()
    suffix = path.suffix.lower()
    media_type = {
        ".png": "image/png",
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".webp": "image/webp",
        ".tiff": "image/tiff",
        ".tif": "image/tiff",
        ".pdf": "application/pdf",
    }.get(suffix, "application/octet-stream")
    artifact = write_file_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id=artifact_id,
        source_path=path,
        filename=path.name,
        media_type=media_type,
        source=source,
    )
    return {
        "artifact_id": artifact.artifact_id,
        "image_ref": artifact.ref,
        "image_path": str(path),
        "media_type": artifact.media_type,
    }


def parse_vision_ref(
    *,
    task_id: str,
    attempt: int,
    image_ref: str,
    source: str = "vision_parser",
) -> dict[str, Any]:
    _require_vision_dependencies()
    image_path = _resolve_image_ref(image_ref)
    width, height = _image_dimensions(image_path)
    raw_words = _ocr_words(image_path)
    raw_ocr_text = _ocr_text(raw_words)
    sensitive_findings = detect_sensitive_ocr_text(raw_ocr_text)
    words = sanitize_ocr_words(raw_words) if sensitive_findings else raw_words
    entities = _entities(words)
    regions = _regions(words)
    ocr_text = redact_sensitive_ocr_text(raw_ocr_text) if sensitive_findings else raw_ocr_text
    ui_tree = _ui_tree(words, width=width, height=height)
    security_scan = build_security_scan(findings=sensitive_findings, source=source)
    summary_lines = [
        f"image_ref={image_ref}",
        f"image_path={image_path}",
        f"dimensions={width}x{height}",
        f"ocr_word_count={len(raw_words)}",
        f"ocr_redacted={str(bool(sensitive_findings)).lower()}",
    ]
    if entities:
        summary_lines.append("top_entities=" + ", ".join(f"{item['value']}({item['count']})" for item in entities[:5]))
    else:
        summary_lines.append("top_entities=none")

    summary_ref = write_text_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id="vision_summary",
        content="\n".join(summary_lines) + "\n",
        media_type="text/plain",
        filename="summary.txt",
        source=source,
    )
    entities_ref = write_json_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id="vision_entities",
        payload={"entities": entities},
        filename="entities.json",
        source=source,
    )
    regions_ref = write_json_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id="vision_regions",
        payload={"regions": regions},
        filename="regions.json",
        source=source,
    )
    ocr_ref = write_json_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id="vision_ocr",
        payload={"words": words, "redacted": bool(sensitive_findings)},
        filename="ocr.json",
        source=source,
    )
    ocr_text_ref = write_text_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id="vision_ocr_text",
        content=ocr_text,
        media_type="text/plain",
        filename="ocr.txt",
        source=source,
    )
    security_ref = write_json_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id="vision_security_scan",
        payload=security_scan,
        filename="security.json",
        source=source,
    )
    evidence_refs: dict[str, str] = {}
    if sensitive_findings:
        raw_regions_ref = write_json_artifact(
            task_id=task_id,
            attempt=attempt,
            artifact_id="vision_regions_evidence",
            payload={"regions": _regions(raw_words), "redacted": False},
            filename="regions.evidence.json",
            source=source,
        )
        raw_ocr_ref = write_json_artifact(
            task_id=task_id,
            attempt=attempt,
            artifact_id="vision_ocr_evidence",
            payload={"words": raw_words, "redacted": False},
            filename="ocr.evidence.json",
            source=source,
        )
        raw_ocr_text_ref = write_text_artifact(
            task_id=task_id,
            attempt=attempt,
            artifact_id="vision_ocr_text_evidence",
            content=raw_ocr_text,
            media_type="text/plain",
            filename="ocr.evidence.txt",
            source=source,
        )
        raw_ui_tree_ref = write_json_artifact(
            task_id=task_id,
            attempt=attempt,
            artifact_id="vision_ui_tree_evidence",
            payload=_ui_tree(raw_words, width=width, height=height),
            filename="ui_tree.evidence.json",
            source=source,
        )
        evidence_refs = {
            "regions_ref": raw_regions_ref.ref,
            "ocr_ref": raw_ocr_ref.ref,
            "ocr_text_ref": raw_ocr_text_ref.ref,
            "ui_tree_ref": raw_ui_tree_ref.ref,
        }
    ui_tree_ref = write_json_artifact(
        task_id=task_id,
        attempt=attempt,
        artifact_id="vision_ui_tree",
        payload={**ui_tree, "redacted": bool(sensitive_findings)},
        filename="ui_tree.json",
        source=source,
    )
    return {
        "ok": True,
        "image_ref": image_ref,
        "summary_ref": summary_ref.ref,
        "entities_ref": entities_ref.ref,
        "regions_ref": regions_ref.ref,
        "ocr_ref": ocr_ref.ref,
        "ocr_text_ref": ocr_text_ref.ref,
        "security_ref": security_ref.ref,
        "ui_tree_ref": ui_tree_ref.ref,
        "word_count": len(raw_words),
        "dimensions": {"width": width, "height": height},
        "security_scan": security_scan,
        "ocr_redacted": bool(sensitive_findings),
        "evidence_refs": evidence_refs,
    }


def main() -> int:
    parser = argparse.ArgumentParser(description="Parse an image artifact ref into structured OCR-friendly outputs")
    parser.add_argument(
        "--internal-handler-cli",
        action="store_true",
        help="Acknowledge that scripts/vision_parser.py is an internal handler CLI, not the preferred active AGN surface.",
    )
    parser.add_argument("--task-id", required=True)
    parser.add_argument("--attempt", type=int, default=1)
    inputs = parser.add_mutually_exclusive_group(required=True)
    inputs.add_argument("--image-ref")
    inputs.add_argument("--image-path")
    parser.add_argument("--artifact-id", default="vision_input")
    args = parser.parse_args()
    if should_block_direct_handler_cli(bool(getattr(args, "internal_handler_cli", False))):
        print(
            render_direct_handler_cli_block(
                handler_id="vision_parser",
                purpose="Structured visual parsing handler behind governed AGN evidence surfaces.",
                recommended_entrypoints=[
                    "python3 scripts/agn_governed_execution.py vision --task-id <id> --image-ref agn://artifact/<sha256>",
                    "python3 scripts/agn_visual_operator.py inspect --image-path <path>",
                ],
                notes=[
                    "Use the explicit override flag only for validation, compatibility, or implementation-level inspection.",
                    "Active AGN visual work should prefer governed facades and audit-linked evidence flow.",
                ],
            )
        )
        return 2
    attempt = max(1, int(args.attempt))
    image_ref = str(args.image_ref or "").strip()
    registered: dict[str, Any] | None = None
    if not image_ref:
        registered = register_image_path(
            task_id=str(args.task_id),
            attempt=attempt,
            image_path=str(args.image_path),
            artifact_id=str(args.artifact_id),
        )
        image_ref = registered["image_ref"]
    try:
        payload = parse_vision_ref(task_id=str(args.task_id), attempt=attempt, image_ref=image_ref)
    except Exception as exc:
        print(json.dumps({"ok": False, "error": f"{type(exc).__name__}: {exc}"}, ensure_ascii=True, indent=2))
        return 1
    if registered:
        payload["registered_input"] = registered
    print(json.dumps(payload, ensure_ascii=True, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
