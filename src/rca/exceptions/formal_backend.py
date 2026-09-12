"""
Formal verification backend interface (Manual §31, Step 8).

The ``FormalBackend`` abstraction returns structured ``VerificationResult``
records rather than plain dicts so that counterexamples, tool metadata,
and source-constraint identity are preserved.

The default backend (``ConservativeFormalBackend``) is available in every
environment and returns ``UNRESOLVED`` for every query — it never claims
VERIFIED without an actual proof.  Real backends (SymbiYosys, etc.)
subclass ``FormalBackend`` and override the prove methods.
"""

from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any

from ..constraint_model import Constraint
from ..utils.enums import VerificationStatus
from ..utils.hashing import stable_hash


@dataclass
class VerificationResult:
    """Result of formally verifying a single timing exception.

    Unlike the legacy tuple/dict representation, this carries enough
    metadata for provenance, explanation, and counterexample replay.
    """

    constraint_id: str = ""
    status: VerificationStatus = VerificationStatus.UNCHECKED
    property_checked: str | None = None
    evidence: dict[str, Any] = field(default_factory=dict)
    counterexample: dict[str, Any] | None = None
    tool: str = "none"
    tool_version: str | None = None
    runtime_seconds: float | None = None
    message: str = ""
    source_constraint_id: str | None = None
    # Bound only by ``bind_verification_result`` after the backend returns.
    # It distinguishes a proof for the current canonical exception from an
    # identically named stale result without using a timestamp as identity.
    constraint_semantic_identity: str = ""
    formal_evidence_identity: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "constraint_id": self.constraint_id,
            "source_constraint_id": self.source_constraint_id or self.constraint_id,
            "constraint_semantic_identity": self.constraint_semantic_identity or None,
            "formal_evidence_identity": self.formal_evidence_identity or None,
            "status": self.status.value,
            "property_checked": self.property_checked,
            "evidence": dict(self.evidence),
            "counterexample": dict(self.counterexample) if self.counterexample else None,
            "tool": self.tool,
            "tool_version": self.tool_version,
            "runtime_seconds": self.runtime_seconds,
            "message": self.message,
        }


class FormalBackend(ABC):
    """Abstract formal verification backend.

    Implementations MUST NOT return VERIFIED unless an actual proof was
    successfully executed.  When the backend cannot complete a proof it
    should return UNRESOLVED (not VERIFIED).
    """

    name: str = "none"
    version: str | None = None

    @abstractmethod
    def prove_false_path(self, constraint_id: str,
                         path_spec: dict[str, Any]) -> VerificationResult: ...

    @abstractmethod
    def prove_multicycle(self, constraint_id: str,
                         path_spec: dict[str, Any],
                         cycles: int) -> VerificationResult: ...

    def check_assumption(self, assumption: str) -> VerificationResult:
        return VerificationResult(
            constraint_id="",
            status=VerificationStatus.UNRESOLVED,
            tool=self.name,
            tool_version=self.version,
            message=f"Assumption checking not supported by backend '{self.name}'.")


class ConservativeFormalBackend(FormalBackend):
    """Always-UNRESOLVED backend used when no formal tool is available.

    This backend is deliberately conservative: it records structural
    evidence but never returns VERIFIED, ensuring RCA never claims a
    proof that didn't happen.
    """

    name = "conservative"
    version = "1.0"

    def prove_false_path(self, constraint_id: str,
                         path_spec: dict[str, Any]) -> VerificationResult:
        return VerificationResult(
            constraint_id=constraint_id,
            source_constraint_id=constraint_id,
            status=VerificationStatus.UNRESOLVED,
            property_checked=f"false_path:{constraint_id}",
            tool=self.name,
            tool_version=self.version,
            evidence={"path_spec": dict(path_spec),
                      "reason": "formal verification unavailable"},
            message="Formal verification unavailable; exception requires manual review or formal proof.",
        )

    def prove_multicycle(self, constraint_id: str,
                         path_spec: dict[str, Any],
                         cycles: int) -> VerificationResult:
        return VerificationResult(
            constraint_id=constraint_id,
            source_constraint_id=constraint_id,
            status=VerificationStatus.UNRESOLVED,
            property_checked=f"multicycle:{constraint_id}:{cycles}",
            tool=self.name,
            tool_version=self.version,
            evidence={"path_spec": dict(path_spec), "cycles": cycles,
                      "reason": "formal verification unavailable"},
            message="Formal verification unavailable; multicycle requires manual review or formal proof.",
        )


class MockFormalBackend(FormalBackend):
    """Deterministic mock used for unit tests.

    Configure by populating ``verdicts`` keyed by constraint_id with a
    ``VerificationResult``; any constraint without an entry returns
    UNRESOLVED.
    """

    name = "mock"
    version = "test"

    def __init__(self, verdicts: dict[str, VerificationResult] | None = None):
        self.verdicts = dict(verdicts or {})

    def _fill(self, vr: VerificationResult, cid: str) -> VerificationResult:
        vr.constraint_id = cid
        vr.source_constraint_id = cid
        vr.tool = self.name
        vr.tool_version = self.version
        if vr.runtime_seconds is None:
            vr.runtime_seconds = 0.0
        return vr

    def prove_false_path(self, constraint_id: str,
                         path_spec: dict[str, Any]) -> VerificationResult:
        v = self.verdicts.get(constraint_id)
        if v is None:
            return self._fill(VerificationResult(
                status=VerificationStatus.UNRESOLVED,
                property_checked=f"false_path:{constraint_id}",
                message="mock: no verdict configured → UNRESOLVED"), constraint_id)
        return self._fill(v, constraint_id)

    def prove_multicycle(self, constraint_id: str,
                         path_spec: dict[str, Any],
                         cycles: int) -> VerificationResult:
        v = self.verdicts.get(constraint_id)
        if v is None:
            return self._fill(VerificationResult(
                status=VerificationStatus.UNRESOLVED,
                property_checked=f"multicycle:{constraint_id}:{cycles}",
                evidence={"cycles": cycles},
                message="mock: no verdict configured → UNRESOLVED"), constraint_id)
        return self._fill(v, constraint_id)


def bind_verification_result(result: VerificationResult, constraint: Constraint) -> VerificationResult:
    """Bind returned formal evidence to immutable current UCM semantics.

    A formal backend receives a selector, not a mutable constraint object. This
    post-backend binding keeps the canonical UCM source authoritative and makes
    replay/staleness checks possible. It does not upgrade any status.
    """
    semantic = stable_hash(constraint.to_canonical_dict())
    result.constraint_id = constraint.id
    result.source_constraint_id = constraint.id
    result.constraint_semantic_identity = semantic
    result.formal_evidence_identity = stable_hash({
        "schema": "rca-formal-evidence-v1",
        "constraint_id": constraint.id,
        "constraint_semantic_identity": semantic,
        "status": result.status.value,
        "property_checked": result.property_checked,
        "tool": result.tool,
        "tool_version": result.tool_version,
        # Evidence is a retained observation; the hash is not a proof claim.
        "evidence": _portable_evidence(result.evidence),
        "counterexample": _portable_evidence(result.counterexample),
    })
    return result


def formal_result_is_current(result: VerificationResult, constraint: Constraint) -> bool:
    """Return whether an evidence result is explicitly bound to this UCM item."""
    return bool(result.formal_evidence_identity and result.constraint_id == constraint.id
                and result.constraint_semantic_identity == stable_hash(constraint.to_canonical_dict()))


def _portable_evidence(value: Any) -> Any:
    """Strip host-specific absolute locations from a formal evidence identity."""
    if isinstance(value, dict):
        return {str(key): _portable_evidence(item) for key, item in sorted(value.items())}
    if isinstance(value, (list, tuple)):
        return [_portable_evidence(item) for item in value]
    if isinstance(value, str):
        # Evidence retains its actual locator separately. Its deterministic
        # identity must not differ solely because a checkout root differs.
        from pathlib import Path
        path = Path(value)
        return path.name if path.is_absolute() else value
    return value
