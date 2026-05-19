"""agn.handlers.vision — proxy for scripts/vision_parser.py

This module re-exports everything from the original script.
The package structure (agn.*) provides clean import paths while
the implementation remains in scripts/ during gradual migration.
"""
from vision_parser import *  # noqa: F401,F403

try:
    from vision_parser import main  # noqa: F401
except ImportError:
    pass
