#!/usr/bin/env python3
from __future__ import annotations

from pathlib import Path
import sys


ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
if str(SRC) not in sys.path:
    sys.path.insert(0, str(SRC))
if str(ROOT / "scripts") not in sys.path:
    sys.path.insert(0, str(ROOT / "scripts"))

from agn.architecture.evolution_pipeline import *  # noqa: F401,F403


if __name__ == "__main__":
    from agn.architecture.evolution_pipeline import main

    raise SystemExit(main())
