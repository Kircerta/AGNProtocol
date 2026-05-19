#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[2]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from scripts.low_token_tools.runner import run_cli
from scripts.low_token_tools.tool_specs import OCR_POST_CLEANER


if __name__ == "__main__":
    raise SystemExit(run_cli(spec=OCR_POST_CLEANER))
