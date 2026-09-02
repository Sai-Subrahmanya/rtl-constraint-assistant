"""
Deterministic hashing utilities for reproducibility (Manual §66, §67).

Provides stable hashes for RTL source sets, configuration, UCMs, and SDC
outputs so cached results and run manifests can detect when inputs changed.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Iterable


def stable_hash(obj: Any) -> str:
    """Return a deterministic SHA-256 hex digest for a JSON-serialisable object.

    Sorts keys and uses compact separators so equivalent data always hashes
    identically regardless of insertion order or whitespace.
    """
    payload = json.dumps(obj, sort_keys=True, separators=(",", ":"), default=str)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def hash_file(path: str | Path) -> str:
    """Return the SHA-256 hex digest of a single file."""
    h = hashlib.sha256()
    p = Path(path)
    with p.open("rb") as f:
        for chunk in iter(lambda: f.read(65536), b""):
            h.update(chunk)
    return h.hexdigest()


def hash_source_set(paths: Iterable[str | Path]) -> dict[str, str]:
    """Return {path: hash} for every source file, sorted by path."""
    result: dict[str, str] = {}
    for p in sorted(paths, key=lambda x: str(x)):
        result[str(p)] = hash_file(p)
    return result


def hash_directory(path: str | Path, pattern: str = "*") -> str:
    """Hash every file matching ``pattern`` under ``path`` (recursively)."""
    base = Path(path)
    files = sorted(base.rglob(pattern))
    return stable_hash({str(f.relative_to(base)): hash_file(f) for f in files if f.is_file()})


def hash_text(text: str) -> str:
    """Return SHA-256 hex digest of a text string (UTF-8)."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()
