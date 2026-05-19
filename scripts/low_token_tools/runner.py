from __future__ import annotations

import json
from pathlib import Path

from scripts.low_token_tools.common import ToolSpec, build_parser, run_tool


def run_cli(*, spec: ToolSpec) -> int:
    parser = build_parser(spec.description)
    args = parser.parse_args()
    code, envelope = run_tool(
        spec=spec,
        input_path=Path(args.input).expanduser().resolve(),
        output_path=Path(args.output).expanduser().resolve(),
        provider=str(args.provider).strip(),
        sample_size=max(0, int(args.sample_size or 0)),
        max_tokens=max(256, int(args.max_tokens or 0)),
        timeout_sec=max(30.0, float(args.timeout_sec or 0.0)),
    )
    print(json.dumps({"ok": envelope["ok"], "output": envelope["output_path"], "tool": spec.name}, ensure_ascii=True))
    return code
