"""Offline, typed constraint knowledge and reuse advisory service.

Knowledge records are *not* a second constraint model.  They carry a frozen
canonical UCM template for reference, an applicability contract, and the
existing RCA provenance/evidence records needed to audit a recommendation.
Only :func:`accept_suggestion` materialises an ordinary UCM ``Constraint``
into a caller-owned ``ConstraintSet``; search itself never mutates a project,
the history sidecar, or an artifact.

The implementation deliberately uses the Step-9 semantic normalizer and UCM
semantic keys.  It does not parse SDC, execute knowledge-file content, or
assign a probability/correctness score.  ``relevance`` is a deterministic
lookup/ranking score only.
"""

from __future__ import annotations

import copy
import json
from collections.abc import Iterable
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any

from ..constraint_model import Constraint, ConstraintSet, SnapshotFormatError, stable_hash_cset
from ..equivalence.normalize import (
    has_unsupported_options,
    normalize_constraint,
    semantic_match_key,
)
from ..provenance import Evidence, ProvenanceRecord
from ..qor.repository import SQLiteQoRRepository
from ..utils.enums import Confidence, ConstraintStatus, ConstraintType, SourceKind
from ..utils.hashing import hash_file, stable_hash
from ..validation import validate as run_validation

KNOWLEDGE_SCHEMA_VERSION = 1
MAX_KNOWLEDGE_FILE_BYTES = 1_048_576
_EPOCH = "1970-01-01T00:00:00+00:00"


class KnowledgeError(ValueError):
    """Raised when untrusted advisory data cannot be safely understood."""


class KnowledgeOrigin(str, Enum):
    BUILTIN = "BUILTIN"
    PROJECT_UCM = "PROJECT_UCM"
    HISTORY = "HISTORY"
    USER_FILE = "USER_FILE"
    RELEASE_PACKAGE = "RELEASE_PACKAGE"


class TrustLevel(str, Enum):
    """Evidence/trust classification, intentionally separate from relevance."""

    VERIFIED = "VERIFIED"
    VALIDATED = "VALIDATED"
    OBSERVED = "OBSERVED"
    USER_PROVIDED = "USER_PROVIDED"
    UNVERIFIED = "UNVERIFIED"
    UNKNOWN = "UNKNOWN"


class ApplicabilityStatus(str, Enum):
    APPLICABLE = "APPLICABLE"
    INAPPLICABLE = "INAPPLICABLE"
    INSUFFICIENT_EVIDENCE = "INSUFFICIENT_EVIDENCE"
    UNKNOWN = "UNKNOWN"


@dataclass(frozen=True)
class ApplicabilityConditions:
    """Explicit, declarative conditions; these are never executable rules."""

    required_objects: tuple[str, ...] = ()
    required_clocks: tuple[str, ...] = ()
    required_scenarios: tuple[str, ...] = ()
    required_constraint_types: tuple[str, ...] = ()
    evidence_required: tuple[str, ...] = ()
    rationale: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "required_objects": list(self.required_objects),
            "required_clocks": list(self.required_clocks),
            "required_scenarios": list(self.required_scenarios),
            "required_constraint_types": list(self.required_constraint_types),
            "evidence_required": list(self.evidence_required),
            "rationale": self.rationale,
        }

    @classmethod
    def from_dict(cls, value: dict[str, Any]) -> ApplicabilityConditions:
        _reject_unknown(value, {
            "required_objects", "required_clocks", "required_scenarios",
            "required_constraint_types", "evidence_required", "rationale",
        }, "applicability")
        return cls(
            required_objects=_string_tuple(value.get("required_objects", []), "required_objects"),
            required_clocks=_string_tuple(value.get("required_clocks", []), "required_clocks"),
            required_scenarios=_string_tuple(value.get("required_scenarios", []), "required_scenarios"),
            required_constraint_types=_string_tuple(
                value.get("required_constraint_types", []), "required_constraint_types"
            ),
            evidence_required=_string_tuple(value.get("evidence_required", []), "evidence_required"),
            rationale=_string(value.get("rationale", ""), "applicability.rationale"),
        )

    def evaluate(self, constraint: Constraint | None = None) -> tuple[ApplicabilityStatus, list[str]]:
        """Evaluate only declared facts available on a candidate UCM constraint.

        Missing contextual facts are deliberately ``INSUFFICIENT_EVIDENCE``;
        this routine does not infer names, clocks, or asynchronous intent.
        """
        if constraint is None:
            if self.evidence_required or self.required_objects or self.required_clocks:
                return (ApplicabilityStatus.INSUFFICIENT_EVIDENCE,
                        ["No candidate UCM constraint was supplied for applicability checks."])
            return ApplicabilityStatus.UNKNOWN, ["No candidate UCM constraint was supplied."]
        objects = set(constraint.target_objects) | set(constraint.source_objects) | set(constraint.through_objects)
        if constraint.path_selector:
            objects.update(constraint.path_selector.all_objects())
        clocks = set(constraint.clock_refs)
        values = constraint.values or {}
        if isinstance(values.get("clock"), str):
            clocks.add(values["clock"])
        if isinstance(values.get("master_clock"), str):
            clocks.add(values["master_clock"])
        missing_objects = sorted(set(self.required_objects) - objects)
        missing_clocks = sorted(set(self.required_clocks) - clocks)
        missing_scenarios = sorted(set(self.required_scenarios) - set(constraint.scenario_ids))
        required_types = set(self.required_constraint_types)
        wrong_type = required_types and constraint.type.value not in required_types
        messages: list[str] = []
        if missing_objects:
            messages.append(f"required objects absent: {', '.join(missing_objects)}")
        if missing_clocks:
            messages.append(f"required clocks absent: {', '.join(missing_clocks)}")
        if missing_scenarios:
            messages.append(f"required scenarios absent: {', '.join(missing_scenarios)}")
        if wrong_type:
            messages.append("candidate constraint type is not in the declared applicability types")
        if messages:
            return ApplicabilityStatus.INAPPLICABLE, messages
        if self.evidence_required:
            return (ApplicabilityStatus.INSUFFICIENT_EVIDENCE,
                    ["Required evidence is not evaluable from a UCM constraint: "
                     + ", ".join(self.evidence_required)])
        return ApplicabilityStatus.APPLICABLE, []


@dataclass(frozen=True)
class KnowledgePattern:
    """An advisory pattern with an optional canonical UCM template.

    A pattern is intentionally distinct from a UCM ``Constraint``. The
    template is a copied canonical dictionary used solely for semantic
    comparison and explicit later materialisation.
    """

    id: str
    title: str
    description: str
    origin: KnowledgeOrigin
    trust_level: TrustLevel
    applicability: ApplicabilityConditions
    provenance: ProvenanceRecord
    constraint_template: dict[str, Any] | None = None
    source_constraint_id: str | None = None
    source_constraint_set_hash: str | None = None
    assumptions: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()

    def template_constraint(self) -> Constraint | None:
        if self.constraint_template is None:
            return None
        return Constraint.from_canonical_dict(copy.deepcopy(self.constraint_template), unknown_field_policy="error")

    @property
    def ucm_semantic_digest(self) -> str | None:
        """Digest of the canonical UCM's native semantic identity."""
        constraint = self.template_constraint()
        return stable_hash(constraint.semantic_key()) if constraint else None

    @property
    def semantic_digest(self) -> str | None:
        """Digest from the existing Step-9 SDC/UCM semantic normalizer."""
        constraint = self.template_constraint()
        return stable_hash(normalize_constraint(constraint)) if constraint else None

    @property
    def match_digest(self) -> str | None:
        constraint = self.template_constraint()
        return stable_hash(semantic_match_key(constraint)) if constraint else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "title": self.title,
            "description": self.description,
            "origin": self.origin.value,
            "trust_level": self.trust_level.value,
            "applicability": self.applicability.to_dict(),
            "provenance": self.provenance.to_dict(),
            "constraint_template": copy.deepcopy(self.constraint_template),
            "source_constraint_id": self.source_constraint_id,
            "source_constraint_set_hash": self.source_constraint_set_hash,
            "ucm_semantic_digest": self.ucm_semantic_digest,
            "semantic_digest": self.semantic_digest,
            "match_digest": self.match_digest,
            "assumptions": list(self.assumptions),
            "warnings": list(self.warnings),
        }


@dataclass(frozen=True)
class KnowledgeSuggestion:
    """A ranked advisory recommendation.  It is not accepted UCM state."""

    id: str
    knowledge_item_id: str
    relevance: int
    rank: int
    match_kind: str
    applicability: ApplicabilityStatus
    evidence: tuple[Evidence, ...]
    provenance: ProvenanceRecord
    assumptions: tuple[str, ...]
    warnings: tuple[str, ...]
    trust_level: TrustLevel
    candidate_constraint: dict[str, Any] | None
    rationale: tuple[str, ...] = ()
    acceptance_state: str = "NOT_ACCEPTED"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "knowledge_item_id": self.knowledge_item_id,
            "relevance": self.relevance,
            "rank": self.rank,
            "match_kind": self.match_kind,
            "applicability": self.applicability.value,
            "evidence": [e.to_dict() for e in self.evidence],
            "provenance": self.provenance.to_dict(),
            "assumptions": list(self.assumptions),
            "warnings": list(self.warnings),
            "trust_level": self.trust_level.value,
            "candidate_constraint": copy.deepcopy(self.candidate_constraint),
            "rationale": list(self.rationale),
            "acceptance_state": self.acceptance_state,
        }


@dataclass(frozen=True)
class AcceptanceResult:
    """Outcome of an explicit attempt to add one suggestion to UCM."""

    status: str
    knowledge_item_id: str
    constraint_id: str | None = None
    duplicate_of: str | None = None
    conflict_ids: tuple[str, ...] = ()
    warnings: tuple[str, ...] = ()
    validation_issues: tuple[dict[str, Any], ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status,
            "knowledge_item_id": self.knowledge_item_id,
            "constraint_id": self.constraint_id,
            "duplicate_of": self.duplicate_of,
            "conflict_ids": list(self.conflict_ids),
            "warnings": list(self.warnings),
            "validation_issues": list(self.validation_issues),
        }


@dataclass
class KnowledgeSearchResult:
    query: str | None
    suggestions: list[KnowledgeSuggestion] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "suggestions": [item.to_dict() for item in self.suggestions],
            "diagnostics": sorted(self.diagnostics),
        }


class KnowledgeEngine:
    """In-memory deterministic index over built-in, UCM, history, and file data."""

    def __init__(self, patterns: Iterable[KnowledgePattern] | None = None, *, include_builtins: bool = True) -> None:
        self._patterns: dict[str, KnowledgePattern] = {}
        self.diagnostics: list[str] = []
        if include_builtins:
            self.add_patterns(builtin_patterns())
        if patterns:
            self.add_patterns(patterns)

    def add_patterns(self, patterns: Iterable[KnowledgePattern]) -> None:
        for pattern in sorted(patterns, key=lambda item: item.id):
            if pattern.id in self._patterns:
                raise KnowledgeError(f"Duplicate knowledge item id '{pattern.id}'.")
            self._patterns[pattern.id] = pattern

    def patterns(self) -> list[KnowledgePattern]:
        return [self._patterns[key] for key in sorted(self._patterns)]

    def get(self, item_id: str) -> KnowledgePattern | None:
        return self._patterns.get(item_id)

    def index_constraint_set(self, cset: ConstraintSet, *, origin: KnowledgeOrigin = KnowledgeOrigin.PROJECT_UCM,
                             source_constraint_set_hash: str | None = None,
                             trust_level: TrustLevel | None = None,
                             projection_evidence: Evidence | None = None) -> list[KnowledgePattern]:
        """Read a canonical UCM collection without modifying it."""
        added: list[KnowledgePattern] = []
        cset_hash = source_constraint_set_hash or stable_hash_cset(cset)
        for constraint in sorted(cset, key=lambda item: item.id):
            pattern = pattern_from_constraint(
                constraint,
                origin=origin,
                source_constraint_set_hash=cset_hash,
                trust_level=trust_level or _trust_for_constraint(constraint),
                projection_evidence=projection_evidence,
            )
            self.add_patterns([pattern])
            added.append(pattern)
        return added

    def index_history(self, repository: SQLiteQoRRepository, *, limit: int | None = None) -> list[KnowledgePattern]:
        """Read historical projections only; SQLite is never changed or trusted as authority.

        A historical row without a retained canonical UCM snapshot yields a
        diagnostic rather than a guessed constraint.  Even a successful run is
        at most OBSERVED/VALIDATED and is never promoted to VERIFIED here.
        """
        added: list[KnowledgePattern] = []
        if not repository.db_path.is_file():
            self.diagnostics.append(
                f"No SQLite history sidecar exists at {repository.db_path}; history was not created or modified."
            )
            return added
        for projection in repository.list_constraint_set_projections(limit=limit):
            cset_hash = projection["constraint_set_hash"]
            locator = projection.get("snapshot_artifact_ref")
            if not locator:
                self.diagnostics.append(
                    f"History projection {cset_hash} has no canonical UCM snapshot locator; no pattern indexed."
                )
                continue
            path = Path(locator)
            if not path.is_file():
                self.diagnostics.append(
                    f"History projection {cset_hash} snapshot is unavailable at {path}; no pattern indexed."
                )
                continue
            expected_digest = projection.get("snapshot_sha256")
            try:
                if expected_digest and hash_file(path) != expected_digest:
                    self.diagnostics.append(
                        f"History projection {cset_hash} snapshot hash mismatch; no pattern indexed."
                    )
                    continue
                raw = json.loads(path.read_text(encoding="utf-8"))
                if not isinstance(raw, dict):
                    raise KnowledgeError("canonical snapshot is not an object")
                cset = ConstraintSet.from_snapshot_dict(raw, unknown_field_policy="error")
            except (OSError, ValueError, SnapshotFormatError, json.JSONDecodeError) as exc:
                self.diagnostics.append(
                    f"History projection {cset_hash} cannot be restored safely: {type(exc).__name__}: {exc}"
                )
                continue
            trust = (TrustLevel.VALIDATED if projection.get("validated_run_count", 0) else TrustLevel.OBSERVED)
            evidence = Evidence(
                id=f"KN-HISTORY-{stable_hash([cset_hash, projection.get('evaluation_ids', [])])[:16]}",
                kind="tool",
                description="Read-only local historical QoR projection; it does not establish correctness.",
                detail={
                    "constraint_set_hash": cset_hash,
                    "evaluation_ids": projection.get("evaluation_ids", []),
                    "run_count": projection.get("run_count", 0),
                    "validated_run_count": projection.get("validated_run_count", 0),
                    "mock_run_count": projection.get("mock_run_count", 0),
                },
                confidence=Confidence.MEDIUM if trust == TrustLevel.VALIDATED else Confidence.LOW,
                rule_id="KNOWLEDGE-HISTORY",
                created_by="rca.knowledge",
                created_at=_EPOCH,
            )
            added.extend(self.index_constraint_set(
                cset,
                origin=KnowledgeOrigin.HISTORY,
                source_constraint_set_hash=cset_hash,
                trust_level=trust,
                projection_evidence=evidence,
            ))
        return added

    def index_release_package(self, package_dir: str | Path) -> list[KnowledgePattern]:
        """Project an existing verified Step-32 package into advisory knowledge.

        Release/package verification establishes package integrity, not formal
        proof or cross-project correctness. Therefore this source is retained
        as ``VALIDATED`` and never promoted automatically to ``VERIFIED``.
        """
        from ..release import PackageVerificationStatus, verify_release_package

        root = Path(package_dir)
        verification = verify_release_package(root)
        if verification.status != PackageVerificationStatus.VERIFIED:
            self.diagnostics.append("Release package is not verifiably intact; no knowledge was indexed.")
            return []
        try:
            raw = json.loads((root / "ucm_snapshot.json").read_text(encoding="utf-8"))
            if not isinstance(raw, dict):
                raise KnowledgeError("canonical UCM snapshot is not an object")
            cset = ConstraintSet.from_snapshot_dict(raw, unknown_field_policy="error")
        except (OSError, ValueError, SnapshotFormatError, json.JSONDecodeError) as exc:
            self.diagnostics.append(f"Verified release package UCM cannot be restored safely: {type(exc).__name__}: {exc}")
            return []
        package_id = verification.package_id
        evidence = Evidence(
            id="KN-RELEASE-" + stable_hash((package_id, verification.snapshot_identity))[:16],
            kind="release_package",
            description="Verified RCA release package projection; not an external proof or signoff.",
            detail={"package_id": package_id, "release_id": verification.release_id,
                    "snapshot_identity": verification.snapshot_identity},
            confidence=Confidence.MEDIUM,
            rule_id="KNOWLEDGE-RELEASE-PACKAGE",
            created_by="rca.knowledge",
            created_at=_EPOCH,
        )
        return self.index_constraint_set(
            cset, origin=KnowledgeOrigin.RELEASE_PACKAGE,
            source_constraint_set_hash=verification.snapshot_identity,
            trust_level=TrustLevel.VALIDATED, projection_evidence=evidence,
        )

    def search(self, *, text: str | None = None, constraint: Constraint | None = None,
               limit: int | None = None, include_origins: Iterable[KnowledgeOrigin] | None = None) -> KnowledgeSearchResult:
        """Search by normalized UCM semantics and/or deterministic token relevance."""
        query_tokens = _tokens(text or "")
        query_norm: tuple | None = None
        query_match: tuple | None = None
        unsupported: list[str] = []
        if constraint is not None:
            unsupported = has_unsupported_options(constraint)
            if not unsupported:
                query_norm = normalize_constraint(constraint)
                query_match = semantic_match_key(constraint)
        allowed = set(include_origins) if include_origins is not None else None
        candidates: list[tuple[int, str, ApplicabilityStatus, list[str], KnowledgePattern]] = []
        for pattern in self.patterns():
            if allowed is not None and pattern.origin not in allowed:
                continue
            candidate = pattern.template_constraint()
            score, match_kind, rationale = _score_pattern(
                pattern, candidate, query_tokens, constraint, query_norm, query_match, unsupported
            )
            if score <= 0:
                continue
            applicability, applicability_notes = pattern.applicability.evaluate(constraint)
            # A source constraint itself has a complete template, but no query
            # still leaves object-specific applicability unknown.
            if constraint is None and candidate is not None and not pattern.applicability.evidence_required:
                applicability = ApplicabilityStatus.UNKNOWN
                applicability_notes.append("No project/candidate context was supplied.")
            rationale.extend(applicability_notes)
            candidates.append((score, match_kind, applicability, rationale, pattern))
        # relevance is deterministic lookup ordering; tie-break by stable item id.
        candidates.sort(key=lambda item: (-item[0], item[4].id))
        if limit is not None:
            candidates = candidates[:max(0, int(limit))]
        suggestions: list[KnowledgeSuggestion] = []
        for rank, (score, match_kind, applicability, rationale, pattern) in enumerate(candidates, 1):
            candidate = pattern.template_constraint()
            warnings = list(pattern.warnings)
            if unsupported:
                warnings.append("Semantic matching is UNKNOWN because the query has unsupported/unresolved options.")
            if candidate is None:
                warnings.append("This is a canonical advisory pattern, not an acceptable UCM constraint template.")
            suggestion_id = "KS-" + stable_hash({
                "item": pattern.id,
                "query": stable_hash(query_norm) if query_norm is not None else sorted(query_tokens),
                "match": match_kind,
            })[:16]
            suggestions.append(KnowledgeSuggestion(
                id=suggestion_id,
                knowledge_item_id=pattern.id,
                relevance=score,
                rank=rank,
                match_kind=match_kind,
                applicability=applicability,
                evidence=tuple(pattern.provenance.evidence),
                provenance=copy.deepcopy(pattern.provenance),
                assumptions=tuple(sorted(set(pattern.assumptions) | set(pattern.provenance.assumption_ids))),
                warnings=tuple(sorted(set(warnings))),
                trust_level=pattern.trust_level,
                candidate_constraint=candidate.to_canonical_dict() if candidate else None,
                rationale=tuple(rationale),
            ))
        diagnostics = list(self.diagnostics)
        if unsupported:
            diagnostics.append("Query semantic comparison is UNKNOWN: " + "; ".join(sorted(unsupported)))
        return KnowledgeSearchResult(query=text, suggestions=suggestions, diagnostics=diagnostics)

    def suggestions_for_constraint_set(self, cset: ConstraintSet, *, limit_per_constraint: int = 5,
                                       include_project_items: bool = False) -> list[KnowledgeSuggestion]:
        """Produce advisory matches for existing UCM without accepting or changing it."""
        origin_filter = None if include_project_items else {
            KnowledgeOrigin.BUILTIN, KnowledgeOrigin.HISTORY, KnowledgeOrigin.USER_FILE,
        }
        output: list[KnowledgeSuggestion] = []
        for constraint in sorted(cset, key=lambda item: item.id):
            result = self.search(constraint=constraint, limit=limit_per_constraint,
                                 include_origins=origin_filter)
            output.extend(result.suggestions)
        return sorted(output, key=lambda item: (item.knowledge_item_id, item.id, item.rank))

    def inference_references(self, report: Any, cset: ConstraintSet) -> list[dict[str, Any]]:
        """Return strong match references and optionally attach them to a report.

        The function does not alter inference status, generated constraints,
        warnings, conflicts, or safeguards.  Only exact, normalized matches
        from VALIDATED/VERIFIED knowledge are reported as strong references.
        """
        references: list[dict[str, Any]] = []
        for constraint in sorted(cset, key=lambda item: item.id):
            for suggestion in self.search(
                constraint=constraint,
                include_origins={KnowledgeOrigin.BUILTIN, KnowledgeOrigin.HISTORY, KnowledgeOrigin.USER_FILE},
            ).suggestions:
                if (suggestion.match_kind == "semantic_exact"
                        and suggestion.trust_level in {TrustLevel.VALIDATED, TrustLevel.VERIFIED}
                        and suggestion.applicability == ApplicabilityStatus.APPLICABLE):
                    references.append({
                        "constraint_id": constraint.id,
                        "knowledge_item_id": suggestion.knowledge_item_id,
                        "suggestion_id": suggestion.id,
                        "match_kind": suggestion.match_kind,
                        "trust_level": suggestion.trust_level.value,
                    })
        references.sort(key=lambda item: (item["constraint_id"], item["knowledge_item_id"], item["suggestion_id"]))
        if hasattr(report, "knowledge_references"):
            report.knowledge_references = list(references)
        return references


def accept_suggestion(suggestion: KnowledgeSuggestion, cset: ConstraintSet) -> AcceptanceResult:
    """Explicitly materialise a suggestion into the caller's canonical UCM.

    This is deliberately an API action, never part of search.  It keeps fixed
    UCM constraints untouched, rejects semantic duplicates, reports (rather
    than resolves) same-scope conflicts, and leaves full design validation to
    the existing validation/conflict pipeline.
    """
    if suggestion.acceptance_state != "NOT_ACCEPTED":
        return AcceptanceResult("REJECTED", suggestion.knowledge_item_id,
                                warnings=("Suggestion is not in the NOT_ACCEPTED state.",))
    if suggestion.candidate_constraint is None:
        return AcceptanceResult("REJECTED", suggestion.knowledge_item_id,
                                warnings=("Knowledge item has no concrete UCM template to accept.",))
    if suggestion.applicability != ApplicabilityStatus.APPLICABLE:
        return AcceptanceResult("REJECTED", suggestion.knowledge_item_id, warnings=(
            f"Applicability is {suggestion.applicability.value}; explicit evidence is required before acceptance.",
        ))
    try:
        candidate = Constraint.from_canonical_dict(copy.deepcopy(suggestion.candidate_constraint),
                                                   unknown_field_policy="error")
    except (TypeError, ValueError) as exc:
        return AcceptanceResult("REJECTED", suggestion.knowledge_item_id,
                                warnings=(f"Candidate template is invalid: {type(exc).__name__}: {exc}",))
    unsupported = has_unsupported_options(candidate)
    if unsupported:
        return AcceptanceResult("REJECTED", suggestion.knowledge_item_id, warnings=(
            "Candidate semantic status is UNKNOWN: " + "; ".join(sorted(unsupported)),
        ))
    missing_deps = sorted(set(candidate.dependency_ids) - set(cset.constraints))
    missing_assumptions = sorted(
        (set(candidate.assumption_ids) | set(candidate.provenance.assumption_ids)) - set(cset.ledger)
    )
    if missing_deps or missing_assumptions:
        pieces: list[str] = []
        if missing_deps:
            pieces.append("missing dependency IDs: " + ", ".join(missing_deps))
        if missing_assumptions:
            pieces.append("missing assumption IDs: " + ", ".join(missing_assumptions))
        return AcceptanceResult("REJECTED", suggestion.knowledge_item_id,
                                warnings=("Cannot preserve source links safely: " + "; ".join(pieces),))
    candidate_norm = normalize_constraint(candidate)
    candidate_match = semantic_match_key(candidate)
    duplicates = [existing.id for existing in cset if not has_unsupported_options(existing)
                  and normalize_constraint(existing) == candidate_norm]
    if duplicates:
        return AcceptanceResult("DUPLICATE", suggestion.knowledge_item_id,
                                duplicate_of=min(duplicates), warnings=(
                                    "Equivalent UCM intent already exists; no duplicate was added.",
                                ))
    conflicts = [existing.id for existing in cset if not has_unsupported_options(existing)
                 and semantic_match_key(existing) == candidate_match]
    original_id = candidate.id
    candidate.id = ""  # ConstraintSet assigns its ordinary, deterministic next UCM ID.
    # A reused item is not silently promoted to fixed/confirmed intent.  Its
    # original identity/status survives in the append-only provenance evidence.
    original_status = candidate.status.value
    candidate.status = ConstraintStatus.REQUIRES_CONFIRMATION
    candidate.provenance.add_evidence(Evidence(
        id="KN-ACCEPT-" + stable_hash({"knowledge_item_id": suggestion.knowledge_item_id,
                                        "source_constraint_id": original_id,
                                        "semantic": candidate_norm})[:16],
        kind="rule",
        description="Explicit knowledge suggestion acceptance into the canonical UCM.",
        detail={
            "knowledge_item_id": suggestion.knowledge_item_id,
            "suggestion_id": suggestion.id,
            "source_constraint_id": original_id,
            "source_status": original_status,
            "trust_level": suggestion.trust_level.value,
            "match_kind": suggestion.match_kind,
            "matching_rationale": list(suggestion.rationale),
            "knowledge_assumptions": list(suggestion.assumptions),
            "knowledge_warnings": list(suggestion.warnings),
            "acceptance_state": "EXPLICIT_ACCEPTED_REQUIRES_CONFIRMATION",
        },
        confidence=Confidence.LOW,
        rule_id="KNOWLEDGE-REUSE",
        created_by="rca.knowledge",
        created_at=_EPOCH,
    ))
    added = cset.add(candidate)
    # Reuse the established validation/conflict pipeline rather than trying to
    # adjudicate disagreement in knowledge search. With no design/timing graph
    # supplied this is still a read-only UCM conflict/semantic validation pass;
    # it cannot execute EDA or a formal backend.
    validation_result = run_validation(cset=cset)
    validation_issues = tuple(issue.to_dict() for issue in validation_result.report.issues)
    warnings = [
        "Added only by explicit acceptance; preserved UCM provenance/evidence and requires confirmation.",
        "Run the existing validation/conflict pipeline before emission or EDA.",
    ]
    if conflicts:
        warnings.append("Same-scope semantic disagreement retained; no existing constraint was overwritten.")
    return AcceptanceResult(
        "ACCEPTED_WITH_CONFLICTS" if conflicts else "ACCEPTED",
        suggestion.knowledge_item_id,
        constraint_id=added.id,
        conflict_ids=tuple(sorted(conflicts)),
        warnings=tuple(warnings),
        validation_issues=validation_issues,
    )


def pattern_from_constraint(constraint: Constraint, *, origin: KnowledgeOrigin,
                            source_constraint_set_hash: str | None = None,
                            trust_level: TrustLevel | None = None,
                            projection_evidence: Evidence | None = None) -> KnowledgePattern:
    """Build a non-mutating advisory item from a canonical UCM constraint."""
    template = constraint.to_canonical_dict()
    provenance = copy.deepcopy(constraint.provenance) if constraint.provenance else ProvenanceRecord(
        source_kind=constraint.source_kind, created_by="rca.knowledge", created_at=_EPOCH,
        confidence=constraint.confidence,
    )
    if projection_evidence is not None:
        provenance.add_evidence(projection_evidence)
    source_hash = source_constraint_set_hash or ""
    item_id = "K-" + origin.value.replace("_", "-") + "-" + stable_hash({
        "source_hash": source_hash,
        "constraint_id": constraint.id,
        "ucm_semantic": constraint.semantic_key(),
        "normalized_semantic": normalize_constraint(constraint),
    })[:16]
    objects = set(constraint.target_objects) | set(constraint.source_objects) | set(constraint.through_objects)
    if constraint.path_selector:
        objects.update(constraint.path_selector.all_objects())
    clocks = set(constraint.clock_refs)
    for key in ("clock", "master_clock"):
        value = constraint.values.get(key) if constraint.values else None
        if isinstance(value, str):
            clocks.add(value)
    return KnowledgePattern(
        id=item_id,
        title=f"{constraint.type.value} from {origin.value.lower()}",
        description="Read-only projection of a canonical UCM constraint; semantic comparison ignores presentation text.",
        origin=origin,
        trust_level=trust_level or _trust_for_constraint(constraint),
        applicability=ApplicabilityConditions(
            required_objects=tuple(sorted(objects)),
            required_clocks=tuple(sorted(clocks)),
            required_scenarios=tuple(sorted(constraint.scenario_ids)),
            required_constraint_types=(constraint.type.value,),
            rationale="The referenced UCM objects, clocks, scenario scope, and constraint type must match.",
        ),
        provenance=provenance,
        constraint_template=template,
        source_constraint_id=constraint.id,
        source_constraint_set_hash=source_constraint_set_hash,
        assumptions=tuple(sorted(set(constraint.assumption_ids) | set(provenance.assumption_ids))),
        warnings=tuple(sorted(has_unsupported_options(constraint))),
    )


def builtin_patterns() -> list[KnowledgePattern]:
    """Return stable vendor-neutral canonical patterns, never concrete intent."""
    definitions = [
        ("K-BUILTIN-CLOCK-DEFINITION", "Primary clock definition",
         "Define a primary clock only when name, target, and period are explicitly evidenced.",
         ConstraintType.CREATE_CLOCK, ("explicit clock target and period",)),
        ("K-BUILTIN-GENERATED-CLOCK", "Generated clock",
         "Define a generated clock only with an evidenced source and master-clock relationship.",
         ConstraintType.CREATE_GENERATED_CLOCK, ("generated-clock source and master clock",)),
        ("K-BUILTIN-IO-DELAY", "Input/output interface delay",
         "Apply I/O delay only with an explicit interface budget and associated clock.",
         None, ("external interface timing budget and associated clock",)),
        ("K-BUILTIN-CLOCK-RELATIONSHIP", "Clock relationship",
         "Declare asynchronous clock grouping only with explicit CDC or user evidence; never guess it from names.",
         ConstraintType.SET_CLOCK_GROUPS, ("explicit clock-relationship or CDC evidence",)),
        ("K-BUILTIN-CLOCK-UNCERTAINTY", "Clock uncertainty",
         "Apply uncertainty only from an approved timing methodology or characterized source.",
         ConstraintType.SET_CLOCK_UNCERTAINTY, ("approved uncertainty methodology",)),
        ("K-BUILTIN-MULTICYCLE", "Multicycle path exception",
         "Apply a multicycle exception only with functional/architectural proof and complete endpoint scope.",
         ConstraintType.SET_MULTICYCLE_PATH, ("functional multicycle proof and endpoint scope",)),
        ("K-BUILTIN-FALSE-PATH", "False-path exception",
         "Apply a false path only with explicit functional proof and full selector scope.",
         ConstraintType.SET_FALSE_PATH, ("functional false-path proof and selector scope",)),
    ]
    out: list[KnowledgePattern] = []
    for item_id, title, description, constraint_type, evidence_required in definitions:
        provenance = ProvenanceRecord(
            created_by="rca.knowledge", created_at=_EPOCH, source_kind=SourceKind.DERIVED,
            rule_id="KNOWLEDGE-BUILTIN", explanation=description, confidence=Confidence.MEDIUM,
            evidence=[Evidence(
                id="KN-BUILTIN-" + stable_hash(item_id)[:16], kind="rule",
                description="Vendor-neutral built-in knowledge pattern.",
                detail={"knowledge_item_id": item_id}, confidence=Confidence.MEDIUM,
                rule_id="KNOWLEDGE-BUILTIN", created_by="rca.knowledge", created_at=_EPOCH,
            )],
        )
        out.append(KnowledgePattern(
            id=item_id, title=title, description=description, origin=KnowledgeOrigin.BUILTIN,
            trust_level=TrustLevel.VALIDATED,
            applicability=ApplicabilityConditions(
                required_constraint_types=(constraint_type.value,) if constraint_type else (),
                evidence_required=evidence_required,
                rationale="Built-in guidance requires project-specific evidence; it never supplies timing intent.",
            ),
            provenance=provenance,
            warnings=("Built-in guidance is advisory and has no concrete UCM constraint to accept.",),
        ))
    return out


def load_knowledge_file(path: str | Path) -> list[KnowledgePattern]:
    """Load a strictly JSON-only data file without code/import/evaluation support."""
    file_path = Path(path)
    if file_path.is_symlink():
        raise KnowledgeError("Knowledge file must not be a symlink.")
    if file_path.suffix.lower() != ".json":
        raise KnowledgeError("Knowledge files must be JSON data (.json); YAML/Tcl/Python/shell are not accepted.")
    try:
        size = file_path.stat().st_size
    except OSError as exc:
        raise KnowledgeError(f"Cannot stat knowledge file: {exc}") from exc
    if size > MAX_KNOWLEDGE_FILE_BYTES:
        raise KnowledgeError(f"Knowledge file exceeds {MAX_KNOWLEDGE_FILE_BYTES} byte safety limit.")
    try:
        raw = json.loads(file_path.read_text(encoding="utf-8"), object_pairs_hook=_reject_duplicate_pairs,
                         parse_constant=_reject_json_constant)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError, KnowledgeError) as exc:
        raise KnowledgeError(f"Invalid knowledge JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise KnowledgeError("Knowledge document must be a JSON object.")
    _reject_unknown(raw, {"schema_version", "patterns"}, "knowledge document")
    if raw.get("schema_version") != KNOWLEDGE_SCHEMA_VERSION:
        raise KnowledgeError(f"Unsupported knowledge schema_version {raw.get('schema_version')!r}.")
    patterns = raw.get("patterns")
    if not isinstance(patterns, list):
        raise KnowledgeError("knowledge.patterns must be a list.")
    if len(patterns) > 1_000:
        raise KnowledgeError("knowledge.patterns exceeds 1000 item safety limit.")
    output: list[KnowledgePattern] = []
    seen: set[str] = set()
    for ordinal, entry in enumerate(patterns):
        if not isinstance(entry, dict):
            raise KnowledgeError(f"patterns[{ordinal}] must be an object.")
        pattern = _pattern_from_file_entry(entry, file_path)
        if pattern.id in seen:
            raise KnowledgeError(f"Duplicate knowledge item id '{pattern.id}' in file.")
        seen.add(pattern.id)
        output.append(pattern)
    return sorted(output, key=lambda item: item.id)


def _pattern_from_file_entry(entry: dict[str, Any], file_path: Path) -> KnowledgePattern:
    _reject_unknown(entry, {
        "id", "title", "description", "constraint_template", "applicability", "assumptions", "warnings",
    }, "knowledge pattern")
    required = {"id", "title", "description", "constraint_template", "applicability"}
    missing = sorted(required - set(entry))
    if missing:
        raise KnowledgeError("knowledge pattern missing required fields: " + ", ".join(missing))
    item_id = _string(entry["id"], "pattern.id")
    if not item_id or len(item_id) > 128 or not all(ch.isalnum() or ch in "-_." for ch in item_id):
        raise KnowledgeError("pattern.id must be 1..128 characters of A-Z/a-z/0-9/-/_.")
    template = entry["constraint_template"]
    if not isinstance(template, dict):
        raise KnowledgeError("pattern.constraint_template must be a canonical UCM constraint object.")
    _validate_strict_constraint_template(template)
    try:
        constraint = Constraint.from_canonical_dict(copy.deepcopy(template), unknown_field_policy="error")
    except (TypeError, ValueError) as exc:
        raise KnowledgeError(f"pattern.constraint_template is not valid UCM data: {exc}") from exc
    # A compact external template may omit optional provenance. Do not let a
    # parser-time clock make the resulting advisory artifact non-deterministic.
    if template.get("provenance") is None:
        constraint.provenance = ProvenanceRecord(
            source_kind=constraint.source_kind,
            created_by="rca.knowledge.user_file",
            created_at=_EPOCH,
            confidence=constraint.confidence,
        )
    unsupported = has_unsupported_options(constraint)
    if unsupported:
        raise KnowledgeError("pattern.constraint_template has unsupported/unresolved semantic options: " +
                             "; ".join(sorted(unsupported)))
    applicability_data = entry["applicability"]
    if not isinstance(applicability_data, dict):
        raise KnowledgeError("pattern.applicability must be an object.")
    applicability = ApplicabilityConditions.from_dict(applicability_data)
    source_digest = hash_file(file_path)
    evidence = Evidence(
        id="KN-USER-FILE-" + stable_hash({"file": source_digest, "item": item_id})[:16],
        kind="user",
        description="User-provided JSON knowledge item; data was parsed but not executed.",
        detail={"knowledge_item_id": item_id, "file_sha256": source_digest},
        confidence=Confidence.LOW, rule_id="KNOWLEDGE-USER-FILE", created_by="rca.knowledge", created_at=_EPOCH,
    )
    provenance = copy.deepcopy(constraint.provenance) if constraint.provenance else ProvenanceRecord(
        source_kind=SourceKind.USER, created_by="rca.knowledge", created_at=_EPOCH, confidence=Confidence.LOW,
    )
    provenance.add_evidence(evidence)
    return KnowledgePattern(
        id=item_id,
        title=_string(entry["title"], "pattern.title"),
        description=_string(entry["description"], "pattern.description"),
        origin=KnowledgeOrigin.USER_FILE,
        # A user file can never self-promote its trust declaration.
        trust_level=TrustLevel.USER_PROVIDED,
        applicability=applicability,
        provenance=provenance,
        constraint_template=constraint.to_canonical_dict(),
        source_constraint_id=constraint.id,
        source_constraint_set_hash=None,
        assumptions=_string_tuple(entry.get("assumptions", []), "pattern.assumptions"),
        warnings=_string_tuple(entry.get("warnings", []), "pattern.warnings"),
    )


def _score_pattern(pattern: KnowledgePattern, candidate: Constraint | None, query_tokens: set[str],
                   query_constraint: Constraint | None, query_norm: tuple | None,
                   query_match: tuple | None, unsupported: list[str]) -> tuple[int, str, list[str]]:
    if unsupported and query_constraint is not None:
        # Text lookup remains possible but semantic ranking must not pretend a match.
        text_score = _text_score(pattern, query_tokens)
        return (text_score, "semantic_unknown", ["Query includes unsupported/unresolved semantics."])
    if candidate is not None and query_norm is not None:
        candidate_unsupported = has_unsupported_options(candidate)
        if candidate_unsupported:
            return 0, "semantic_unknown", ["Knowledge template has unsupported semantics."]
        if normalize_constraint(candidate) == query_norm:
            return 100, "semantic_exact", ["Exact match after existing UCM/SDC semantic normalization."]
        if semantic_match_key(candidate) == query_match:
            return 70, "same_scope_different_values", [
                "Same normalized scope/identity but values differ; this is a disagreement, not equivalence."
            ]
        if query_constraint is not None and candidate.type == query_constraint.type:
            return 35 + _text_score(pattern, query_tokens), "same_type", [
                "Same constraint type only; no semantic equivalence is claimed."
            ]
    if candidate is None and query_constraint is not None:
        declared_types = set(pattern.applicability.required_constraint_types)
        if not declared_types or query_constraint.type.value in declared_types:
            return 25, "canonical_pattern", [
                "Canonical guidance for this constraint type; it supplies no concrete UCM intent."
            ]
    text_score = _text_score(pattern, query_tokens)
    return text_score, "text", ["Deterministic token match only; no semantic equivalence is claimed."]


def _text_score(pattern: KnowledgePattern, query_tokens: set[str]) -> int:
    if not query_tokens:
        return 0
    haystack = _tokens(" ".join((pattern.id, pattern.title, pattern.description,
                                  pattern.constraint_template.get("type", "") if pattern.constraint_template else "")))
    overlap = len(query_tokens & haystack)
    return min(30, overlap * 10) if overlap else 0


def _trust_for_constraint(constraint: Constraint) -> TrustLevel:
    if constraint.source_kind == SourceKind.USER:
        return TrustLevel.USER_PROVIDED
    if constraint.source_kind in {SourceKind.EXISTING_SDC, SourceKind.RTL, SourceKind.TOOL,
                                  SourceKind.LIBRARY, SourceKind.PHYSICAL_DATA}:
        return TrustLevel.OBSERVED
    if constraint.status in {ConstraintStatus.CONFIRMED, ConstraintStatus.FIXED}:
        return TrustLevel.VALIDATED
    return TrustLevel.UNVERIFIED


def _tokens(value: str) -> set[str]:
    import re
    return {part.lower() for part in re.findall(r"[A-Za-z0-9_]+", value) if part}


def _string(value: Any, label: str) -> str:
    if not isinstance(value, str):
        raise KnowledgeError(f"{label} must be a string.")
    return value


def _string_tuple(value: Any, label: str) -> tuple[str, ...]:
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise KnowledgeError(f"{label} must be a list of strings.")
    return tuple(sorted(set(value)))


def _reject_unknown(value: dict[str, Any], allowed: set[str], label: str) -> None:
    extra = sorted(set(value) - allowed)
    if extra:
        raise KnowledgeError(f"{label} contains unsupported field(s): {', '.join(extra)}")


def _reject_duplicate_pairs(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise KnowledgeError(f"duplicate JSON key '{key}'")
        result[key] = value
    return result


def _reject_json_constant(value: str) -> Any:
    raise KnowledgeError(f"non-finite JSON constant '{value}' is not allowed")


def _validate_strict_constraint_template(template: dict[str, Any]) -> None:
    # The external data format accepts only the published canonical UCM shape.
    _reject_unknown(template, {
        "id", "type", "target_objects", "source_objects", "through_objects", "clock_refs", "target_refs",
        "source_refs", "through_refs", "clock_refs_typed", "values", "source_kind", "provenance",
        "confidence", "status", "opt_status", "generation_confidence", "evidence_ids", "assumption_ids",
        "dependency_ids", "downstream_ids", "affected_paths", "dependent_analyses", "scenario_ids",
        "precedence", "disabled", "comment", "path_selector",
    }, "constraint_template")
    if "id" not in template or "type" not in template or "source_kind" not in template:
        raise KnowledgeError("constraint_template requires id, type, and source_kind.")
    for field_name in ("target_objects", "source_objects", "through_objects", "clock_refs", "evidence_ids",
                       "assumption_ids", "dependency_ids", "downstream_ids", "affected_paths",
                       "dependent_analyses", "scenario_ids"):
        if field_name in template:
            _string_tuple(template[field_name], f"constraint_template.{field_name}")
    if "values" in template and not isinstance(template["values"], dict):
        raise KnowledgeError("constraint_template.values must be an object.")
    for field_name in ("target_refs", "source_refs", "clock_refs_typed"):
        if field_name in template:
            _validate_target_ref_list(template[field_name], f"constraint_template.{field_name}")
    if "through_refs" in template:
        if not isinstance(template["through_refs"], list):
            raise KnowledgeError("constraint_template.through_refs must be a list of lists.")
        for ordinal, stage in enumerate(template["through_refs"]):
            _validate_target_ref_list(stage, f"constraint_template.through_refs[{ordinal}]")
    if template.get("provenance") is not None:
        if not isinstance(template["provenance"], dict):
            raise KnowledgeError("constraint_template.provenance must be an object or null.")
        _validate_strict_provenance(template["provenance"])
    if template.get("path_selector") is not None:
        if not isinstance(template["path_selector"], dict):
            raise KnowledgeError("constraint_template.path_selector must be an object or null.")
        selector = template["path_selector"]
        _reject_unknown(selector, {
            "from_set", "through_set", "to_set", "from_refs", "through_refs", "to_refs", "edge", "min_max",
            "setup_hold", "add_delay", "reset_path", "from_clock", "to_clock", "through_clock", "scenario",
        }, "constraint_template.path_selector")
        for field_name in ("from_set", "to_set", "from_clock", "to_clock", "through_clock"):
            if field_name in selector:
                _string_tuple(selector[field_name], f"constraint_template.path_selector.{field_name}")
        for field_name in ("from_refs", "to_refs"):
            if field_name in selector:
                _validate_target_ref_list(selector[field_name], f"constraint_template.path_selector.{field_name}")
        if "through_refs" in selector:
            if not isinstance(selector["through_refs"], list):
                raise KnowledgeError("constraint_template.path_selector.through_refs must be a list of lists.")
            for ordinal, stage in enumerate(selector["through_refs"]):
                _validate_target_ref_list(stage, f"constraint_template.path_selector.through_refs[{ordinal}]")


def _validate_target_ref_list(value: Any, label: str) -> None:
    if not isinstance(value, list):
        raise KnowledgeError(f"{label} must be a list.")
    for ordinal, reference in enumerate(value):
        if not isinstance(reference, dict):
            raise KnowledgeError(f"{label}[{ordinal}] must be an object.")
        _reject_unknown(reference, {
            "collection_kind", "pattern", "members", "filters", "expression", "resolution_status",
            "unresolved_reason",
        }, f"{label}[{ordinal}]")
        if "collection_kind" not in reference:
            raise KnowledgeError(f"{label}[{ordinal}] is missing collection_kind.")
        if "members" in reference:
            _string_tuple(reference["members"], f"{label}[{ordinal}].members")
        if "filters" in reference and not isinstance(reference["filters"], dict):
            raise KnowledgeError(f"{label}[{ordinal}].filters must be an object.")


def _validate_strict_provenance(provenance: dict[str, Any]) -> None:
    _reject_unknown(provenance, {
        "created_by", "created_at", "source_kind", "rule_id", "evidence", "assumption_ids", "dependency_ids",
        "downstream_ids", "affected_paths", "scenario_ids", "import_meta", "explanation", "confidence",
    }, "constraint_template.provenance")
    evidence = provenance.get("evidence", [])
    if not isinstance(evidence, list):
        raise KnowledgeError("constraint_template.provenance.evidence must be a list.")
    for ordinal, item in enumerate(evidence):
        if not isinstance(item, dict):
            raise KnowledgeError(f"provenance.evidence[{ordinal}] must be an object.")
        _reject_unknown(item, {
            "id", "kind", "description", "detail", "source_objects", "location", "confidence", "rule_id",
            "created_by", "created_at",
        }, f"provenance.evidence[{ordinal}]")
    import_meta = provenance.get("import_meta")
    if import_meta is not None:
        if not isinstance(import_meta, dict):
            raise KnowledgeError("constraint_template.provenance.import_meta must be an object or null.")
        _reject_unknown(import_meta, {
            "source_file", "source_line", "original_command", "source_format", "import_run_id",
            "import_timestamp", "extra",
        }, "constraint_template.provenance.import_meta")


__all__ = [
    "KNOWLEDGE_SCHEMA_VERSION",
    "MAX_KNOWLEDGE_FILE_BYTES",
    "AcceptanceResult",
    "ApplicabilityConditions",
    "ApplicabilityStatus",
    "KnowledgeEngine",
    "KnowledgeError",
    "KnowledgeOrigin",
    "KnowledgePattern",
    "KnowledgeSearchResult",
    "KnowledgeSuggestion",
    "TrustLevel",
    "accept_suggestion",
    "builtin_patterns",
    "load_knowledge_file",
    "pattern_from_constraint",
]
