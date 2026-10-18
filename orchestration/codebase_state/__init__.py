"""Local codebase-state collector for ~/projects/noesis-*.

This package writes a provenance-preserving manifest and current-state
summaries. It does not embed, call a model, or schedule itself.
Semantic retrieval is intentionally not provided.
"""

from .engine import run_ingestion

__all__ = ["run_ingestion"]
SCHEMA = "noesis.codebase-state/v1"
