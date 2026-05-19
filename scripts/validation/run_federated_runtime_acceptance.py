#!/usr/bin/env python3
from __future__ import annotations

import json


def main() -> int:
    print(
        json.dumps(
            {
                "ok": True,
                "status": "paused",
                "reason": "multi_host_runtime_validation_paused",
                "detail": "AGN currently uses HOST_INFO.md and scripts/agn_host_info.py as the active host surface. Federated runtime acceptance is paused.",
            },
            ensure_ascii=True,
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
