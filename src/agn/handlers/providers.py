"""agn.handlers.providers — proxy for scripts/model_router.py

This module re-exports everything from the original script.
The package structure (agn.*) provides clean import paths while
the implementation remains in scripts/ during gradual migration.
"""
from model_router import *  # noqa: F401,F403

try:
    from model_router import main  # noqa: F401
except ImportError:
    pass
