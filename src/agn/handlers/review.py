"""agn.handlers.review — proxy for scripts/review_orchestrator.py

This module re-exports everything from the original script.
The package structure (agn.*) provides clean import paths while
the implementation remains in scripts/ during gradual migration.
"""
from review_orchestrator import *  # noqa: F401,F403

try:
    from review_orchestrator import main  # noqa: F401
except ImportError:
    pass
