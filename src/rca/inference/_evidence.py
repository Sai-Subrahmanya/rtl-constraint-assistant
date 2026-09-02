"""Shared helpers for deterministic, stable Evidence IDs.

An Evidence's semantic identity is determined by (rule_id, kind,
description, sorted source_objects). These fields alone MUST be enough
to distinguish two meaningfully different pieces of evidence;
timestamps, object identities, interpreter state, and insertion order
are excluded.
"""

from __future__ import annotations

from ..provenance import Evidence
from ..utils.enums import Confidence
from ..utils.hashing import stable_hash


def evidence_id(rule_id: str, kind: str, description: str,
                source_objects: list[str] | None = None,
                length: int = 12) -> str:
    """Return a deterministic short ID for an Evidence record.

    Uses SHA-256 over canonical JSON of the identifying tuple. Two
    records that are semantically identical (same rule/kind/
    description/source_objects) always receive the same ID, across
    Python processes and insertion orders.
    """
    objs = sorted(source_objects or [])
    digest = stable_hash((rule_id, kind, description, tuple(objs)))
    return f"ev_{digest[:length]}"


def make_evidence(rule_id: str, kind: str, description: str, *,
                  source_objects: list[str] | None = None,
                  confidence: Confidence = Confidence.MEDIUM,
                  created_by: str | None = None,
                  created_at: str | None = None,
                  location: str | None = None,
                  detail: dict | None = None) -> Evidence:
    """Construct an Evidence with a deterministic ID."""
    eid = evidence_id(rule_id, kind, description, source_objects)
    return Evidence(
        id=eid,
        kind=kind,
        description=description,
        source_objects=sorted(source_objects or []),
        confidence=confidence,
        rule_id=rule_id,
        created_by=created_by or rule_id,
        # created_at is intentionally NOT part of identity; callers
        # may set it to a run timestamp for provenance display.
        created_at=created_at or Evidence.model_fields["created_at"].default_factory(),
        location=location,
        detail=detail or {},
    )
