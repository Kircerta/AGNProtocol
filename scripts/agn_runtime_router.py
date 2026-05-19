#!/usr/bin/env python3
from __future__ import annotations

"""Paused compatibility shim for the older runtime-router entry point.

AGN no longer exposes active host-choice routing. Use `scripts/agn_host_info.py`
and `HOST_INFO.md` for the current single-host surface.
"""

from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

import json


def main() -> int:
    print(
        json.dumps(
            {
                "ok": True,
                "status": "paused",
                "reason": "multi_host_runtime_router_paused",
                "detail": "Use scripts/agn_host_info.py and HOST_INFO.md for local host context instead of runtime routing.",
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
