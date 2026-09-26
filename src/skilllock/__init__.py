"""skilllock: a local, deterministic lockfile manager for Agent Skills.

skilllock pins Git-hosted skills to an immutable commit, verifies file
hashes, and reproduces the pinned files on demand. It is intentionally
offline-first: no LLM, no API key, no registry, and no server.
"""

from __future__ import annotations

__version__ = "0.1.1"

__all__ = ["__version__"]
