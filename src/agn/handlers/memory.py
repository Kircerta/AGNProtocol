"""agn.handlers.memory — proxy for scripts/memory_recorder.py

This module re-exports everything from the original script.
The package structure (agn.*) provides clean import paths while
the implementation remains in scripts/ during gradual migration.
"""
from memory_recorder import *  # noqa: F401,F403
