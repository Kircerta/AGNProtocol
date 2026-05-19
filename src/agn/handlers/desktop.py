"""agn.handlers.desktop — proxy for scripts/desktop_adapter.py

This module re-exports everything from the original script.
The package structure (agn.*) provides clean import paths while
the implementation remains in scripts/ during gradual migration.
"""
from desktop_adapter import *  # noqa: F401,F403
