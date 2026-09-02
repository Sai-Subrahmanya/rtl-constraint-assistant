"""
Semantic comparison engine for two constraint sets (Step 9 — WP-L).

Pipeline::

    SDC A  → importer → UCM A ┐
                              ├→ normalize → pair by semantic key
    SDC B  → importer → UCM B ┘           → field-level diff
                                           → multi-set / duplicate detection
                                           → scenario-aware grouping
                                           → UNKNOWN when unsafe

Key properties:

* Comparison runs over the UCM, never over raw SDC text.
* Deterministic: outputs (statuses, ordering, IDs) are stable across
  runs and independent of dict/set iteration order. We do NOT use
  Python's built-in ``hash()``.
* Provenance equality is tracked separately from semantic equality —
  USER vs IMPORT constraints with identical timing intent compare as
  SEMANTIC_EQUIVALENT.
* Duplicates are detected per-side and classified as
  DUPLICATE (identical semantics), REDUNDANT (harmless overlap),
  or CONFLICTING (same identity, conflicting values).
* Unsupported / unresolved options force UNKNOWN for the affected pair
  rather than falsely claiming equivalence.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..constraint_model import Constraint, ConstraintSet
from ..provenance import ProvenanceRecord
from ..utils.enums import (
    ComparisonLevel,
    ConstraintPairStatus,
    ConstraintType,
    DiagnosticSeverity,
    DiffAction,
    EquivalenceResult,
)
from ..utils.hashing import stable_hash
from .normalize import (
    field_level_diff,
    has_unsupported_options,
    normalize_constraint,
    semantic_match_key,
)


# ---------------------------------------------------------------------------
# Dataclasses
# ---------------------------------------------------------------------------


@dataclass
class FieldDifference:
    field: str
    value_a: Any
    value_b: Any
    explanation: str

    def to_dict(self) -> dict[str, Any]:
        return {
            "field": self.field,
            "value_a": self.value_a,
            "value_b": self.value_b,
            "explanation": self.explanation,
        }


@dataclass
class PairResult:
    """Result of comparing one semantic pair of constraints."""
    status: ConstraintPairStatus = ConstraintPairStatus.UNKNOWN
    level: ComparisonLevel = ComparisonLevel.UNKNOWN
    a_id: str | None = None
    b_id: str | None = None
    constraint_type: str = ""
    a_source_kind: str = ""
    b_source_kind: str = ""
    semantic_key_digest: str = ""
    fields: list[FieldDifference] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)
    scenarios: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.status.value,
            "level": self.level.value,
            "a_id": self.a_id,
            "b_id": self.b_id,
            "constraint_type": self.constraint_type,
            "a_source_kind": self.a_source_kind,
            "b_source_kind": self.b_source_kind,
            "provenance_equal": self.a_source_kind == self.b_source_kind
                                if (self.a_source_kind and self.b_source_kind)
                                else None,
            "semantic_key_digest": self.semantic_key_digest,
            "fields": [f.to_dict() for f in self.fields],
            "notes": list(self.notes),
            "scenarios": list(self.scenarios),
        }


@dataclass
class DuplicateRecord:
    """A duplicate (identical or conflicting) within one side."""
    side: str               # "left" | "right"
    classification: ConstraintPairStatus
    ids: list[str]
    constraint_type: str
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "side": self.side,
            "classification": self.classification.value,
            "ids": list(self.ids),
            "constraint_type": self.constraint_type,
            "note": self.note,
        }


@dataclass
class ComparisonResult:
    """Structured comparison report (Step 9 §16)."""
    overall_status: EquivalenceResult = EquivalenceResult.UNKNOWN
    comparison_level: ComparisonLevel = ComparisonLevel.UNKNOWN

    equivalent_constraints: list[PairResult] = field(default_factory=list)
    different_constraints: list[PairResult] = field(default_factory=list)
    unknown_constraints: list[PairResult] = field(default_factory=list)
    only_in_left: list[PairResult] = field(default_factory=list)
    only_in_right: list[PairResult] = field(default_factory=list)
    duplicates_left: list[DuplicateRecord] = field(default_factory=list)
    duplicates_right: list[DuplicateRecord] = field(default_factory=list)
    normalization_notes: list[str] = field(default_factory=list)
    diagnostics: list[str] = field(default_factory=list)

    # --- summary accessors ---

    def counts(self) -> dict[str, int]:
        return {
            "equivalent": len(self.equivalent_constraints),
            "different": len(self.different_constraints),
            "unknown": len(self.unknown_constraints),
            "only_in_left": len(self.only_in_left),
            "only_in_right": len(self.only_in_right),
            "duplicates_left": len(self.duplicates_left),
            "duplicates_right": len(self.duplicates_right),
        }

    def to_dict(self) -> dict[str, Any]:
        return {
            "overall_status": self.overall_status.value,
            "comparison_level": self.comparison_level.value,
            "counts": self.counts(),
            "equivalent_constraints": [p.to_dict() for p in self.equivalent_constraints],
            "different_constraints": [p.to_dict() for p in self.different_constraints],
            "unknown_constraints": [p.to_dict() for p in self.unknown_constraints],
            "only_in_left": [p.to_dict() for p in self.only_in_left],
            "only_in_right": [p.to_dict() for p in self.only_in_right],
            "duplicates_left": [d.to_dict() for d in self.duplicates_left],
            "duplicates_right": [d.to_dict() for d in self.duplicates_right],
            "normalization_notes": list(self.normalization_notes),
            "diagnostics": list(self.diagnostics),
        }


# ---------------------------------------------------------------------------
# DiffEntry (legacy-compatible shim for code that still imports it)
# ---------------------------------------------------------------------------


@dataclass
class DiffEntry:
    action: DiffAction
    signature: tuple = ()
    a_id: str | None = None
    b_id: str | None = None
    note: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "action": self.action.value,
            "a_id": self.a_id,
            "b_id": self.b_id,
            "note": self.note,
        }


# ---------------------------------------------------------------------------
# Core comparison
# ---------------------------------------------------------------------------


def _provenance_equal(a: Constraint, b: Constraint) -> bool:
    """Provenance is considered equal when source_kind AND the primary
    provenance source file/rule match. Semantic comparisons do NOT use
    this for equivalence — it is informational only (Step 9 §19)."""
    try:
        if a.source_kind != b.source_kind:
            return False
        pa = getattr(a, "provenance", None)
        pb = getattr(b, "provenance", None)
        if pa is None and pb is None:
            return True
        if pa is None or pb is None:
            return False
        return (getattr(pa, "source_file", None) == getattr(pb, "source_file", None)
                and getattr(pa, "rule_id", None) == getattr(pb, "rule_id", None))
    except Exception:
        return False


def _group_by_semantic_key(cs: ConstraintSet) -> dict[tuple, list[Constraint]]:
    out: dict[tuple, list[Constraint]] = {}
    for c in cs:
        if getattr(c, "disabled", False):
            continue
        k = semantic_match_key(c)
        out.setdefault(k, []).append(c)
    # Deterministic within-group ordering by constraint id.
    for v in out.values():
        v.sort(key=lambda x: x.id)
    return out


def _detect_side_duplicates(side: str,
                            groups: dict[tuple, list[Constraint]]
                            ) -> list[DuplicateRecord]:
    """Within one side, classify multiple constraints sharing a semantic
    match key as DUPLICATE (identical sig), REDUNDANT (overlap e.g. two
    broad clock defs with same params), or CONFLICTING (same identity
    but different values)."""
    dups: list[DuplicateRecord] = []
    for k, items in groups.items():
        if len(items) < 2:
            continue
        sigs = {}
        for c in items:
            sig = normalize_constraint(c)
            sigs.setdefault(sig, []).append(c)
        type_str = items[0].type.value
        if len(sigs) == 1:
            dups.append(DuplicateRecord(
                side=side,
                classification=ConstraintPairStatus.DUPLICATE,
                ids=[c.id for c in items],
                constraint_type=type_str,
                note="identical semantic duplicate",
            ))
        else:
            # All items share match key but signatures differ — check
            # whether they are REDUNDANT (overlapping selectors, e.g.
            # broad+specific) or CONFLICTING (same scalar identity).
            scalar_conflict = False
            ident_fields = {"name", "source", "master_clock", "clock"}
            rep = items[0]
            vrep = dict(rep.values or {})
            for c in items[1:]:
                vc = dict(c.values or {})
                for fld in ident_fields:
                    if vrep.get(fld) is not None and vc.get(fld) is not None \
                            and vrep.get(fld) == vc.get(fld):
                        # Same scalar identity but different sigs = conflict
                        scalar_conflict = True
            if scalar_conflict:
                dups.append(DuplicateRecord(
                    side=side,
                    classification=ConstraintPairStatus.CONFLICTING,
                    ids=[c.id for c in items],
                    constraint_type=type_str,
                    note="same identity but conflicting values",
                ))
            else:
                dups.append(DuplicateRecord(
                    side=side,
                    classification=ConstraintPairStatus.REDUNDANT,
                    ids=[c.id for c in items],
                    constraint_type=type_str,
                    note="overlapping but not identical (e.g. broad + specific)",
                ))
    # Deterministic ordering.
    dups.sort(key=lambda d: (d.constraint_type, d.ids[0] if d.ids else ""))
    return dups


def _pair(a: Constraint, b: Constraint) -> PairResult:
    """Compare two constraints assumed to share a semantic match key."""
    # UNKNOWN reasons from either side.
    reasons_a = has_unsupported_options(a)
    reasons_b = has_unsupported_options(b)
    reasons = reasons_a + reasons_b

    pr = PairResult(
        a_id=a.id,
        b_id=b.id,
        constraint_type=a.type.value,
        a_source_kind=a.source_kind.value if a.source_kind else "",
        b_source_kind=b.source_kind.value if b.source_kind else "",
        semantic_key_digest=stable_hash(semantic_match_key(a)),
        scenarios=sorted(set(a.scenario_ids) | set(b.scenario_ids)),
    )

    sig_a = normalize_constraint(a)
    sig_b = normalize_constraint(b)

    if reasons:
        pr.status = ConstraintPairStatus.UNKNOWN
        pr.level = ComparisonLevel.UNKNOWN
        pr.notes.extend(reasons)
        return pr

    if sig_a == sig_b:
        pr.status = ConstraintPairStatus.EQUIVALENT
        pr.level = ComparisonLevel.SEMANTIC_EQUIVALENT
        if _provenance_equal(a, b):
            pr.notes.append("provenance-equal")
        else:
            pr.notes.append("provenance differs but semantics equal")
        return pr

    # Signatures differ — produce field-level diffs.
    fd = field_level_diff(a, b)
    pr.status = ConstraintPairStatus.DIFFERENT
    pr.level = ComparisonLevel.SEMANTIC_DIFFERENT
    pr.fields = [FieldDifference(**d) for d in fd]
    return pr


def compare(base: ConstraintSet, other: ConstraintSet) -> ComparisonResult:
    """Compare two UCM ConstraintSets and return a structured ComparisonResult.

    Pairing policy (Step 9 corrections):
      1. Group both sides by deterministic semantic match key.
      2. Detect per-side duplicates / conflicts.
      3. For each key, perform multiset matching in this order:
         a. exact normalized signature (one-to-one greedy)
         b. unambiguous scalar identity match (e.g. same clock name +
            targets) within the same match-key group
         c. if exactly one unmatched candidate remains on each side,
            pair them for field-level diff
         d. otherwise, treat extras as ONLY_IN_LEFT / ONLY_IN_RIGHT
            — do NOT invent arbitrary pairs just to produce a diff.
    """
    result = ComparisonResult()
    result.normalization_notes.append(
        "Timing quantities normalized to SI seconds; unordered collections sorted; "
        "ordered -through stages preserved; provenance ignored for semantic identity."
    )
    result.normalization_notes.append(
        "Conservative UNKNOWN returned when options are unsupported, targets "
        "unresolved, or import marked PARTIAL/UNRESOLVED."
    )
    result.normalization_notes.append(
        "Pairing: exact signature first, unambiguous identity second; "
        "ambiguous remainders are reported as ONLY_IN_LEFT / ONLY_IN_RIGHT, "
        "not force-paired."
    )

    groups_a = _group_by_semantic_key(base)
    groups_b = _group_by_semantic_key(other)

    result.duplicates_left = _detect_side_duplicates("left", groups_a)
    result.duplicates_right = _detect_side_duplicates("right", groups_b)

    all_keys = sorted(set(groups_a.keys()) | set(groups_b.keys()),
                      key=lambda k: stable_hash(k))

    for k in all_keys:
        list_a = list(groups_a.get(k, []))
        list_b = list(groups_b.get(k, []))

        # Pairwise matching with decreasing strength.
        pairs, rem_a, rem_b = _pair_multiset(list_a, list_b)

        for ca, cb, strength in pairs:
            pr = _pair(ca, cb)
            if strength == "signature":
                # best match; nothing extra to annotate
                pass
            else:
                pr.notes.append(f"paired by {strength}")
            if pr.status == ConstraintPairStatus.EQUIVALENT:
                result.equivalent_constraints.append(pr)
            elif pr.status == ConstraintPairStatus.UNKNOWN:
                result.unknown_constraints.append(pr)
            else:
                result.different_constraints.append(pr)

        for ca in rem_a:
            result.only_in_left.append(PairResult(
                status=ConstraintPairStatus.ONLY_IN_LEFT,
                level=ComparisonLevel.SEMANTIC_DIFFERENT,
                a_id=ca.id,
                constraint_type=ca.type.value,
                a_source_kind=ca.source_kind.value if ca.source_kind else "",
                semantic_key_digest=stable_hash(k),
                scenarios=sorted(ca.scenario_ids),
                notes=["present in A but not in B (no unambiguous B counterpart)"],
            ))
        for cb in rem_b:
            result.only_in_right.append(PairResult(
                status=ConstraintPairStatus.ONLY_IN_RIGHT,
                level=ComparisonLevel.SEMANTIC_DIFFERENT,
                b_id=cb.id,
                constraint_type=cb.type.value,
                b_source_kind=cb.source_kind.value if cb.source_kind else "",
                semantic_key_digest=stable_hash(k),
                scenarios=sorted(cb.scenario_ids),
                notes=["present in B but not in A (no unambiguous A counterpart)"],
            ))

    # Deterministic ordering of all lists.
    def _sig(p: PairResult) -> str:
        return stable_hash((p.constraint_type, p.a_id or "", p.b_id or ""))
    for lst in (result.equivalent_constraints, result.different_constraints,
                result.unknown_constraints, result.only_in_left,
                result.only_in_right):
        lst.sort(key=_sig)

    # Overall status rollup
    c = result.counts()
    if c["different"] or c["only_in_left"] or c["only_in_right"]:
        result.overall_status = EquivalenceResult.DIFFERENT
        result.comparison_level = ComparisonLevel.SEMANTIC_DIFFERENT
    elif c["unknown"]:
        result.overall_status = EquivalenceResult.UNKNOWN
        result.comparison_level = ComparisonLevel.UNKNOWN
    else:
        any_normalized = any(
            any("provenance differs" in n for n in p.notes)
            for p in result.equivalent_constraints
        ) or any(
            any(n.startswith("paired by") for n in p.notes)
            for p in result.equivalent_constraints
        )
        if any_normalized:
            result.overall_status = EquivalenceResult.EQUIVALENT_AFTER_NORMALIZATION
            result.comparison_level = ComparisonLevel.SEMANTIC_EQUIVALENT
        else:
            result.overall_status = EquivalenceResult.EQUIVALENT
            result.comparison_level = ComparisonLevel.SEMANTIC_EQUIVALENT

    # Back-compat: legacy "diff" list of DiffEntry
    result._legacy_diff = _legacy_diff(result)
    return result


# ---------------------------------------------------------------------------
# Multiset pairing
# ---------------------------------------------------------------------------

def _identity_sig(c: Constraint) -> tuple:
    """Scalar identity signature — used to find unambiguous pairs
    within a match-key group when exact signature differs."""
    v = dict(c.values or {})
    t = c.type
    parts = [t.value]
    # Scalar identity fields by type
    if t in (ConstraintType.CREATE_CLOCK,
             ConstraintType.CREATE_GENERATED_CLOCK):
        parts.append(v.get("name"))
        if t == ConstraintType.CREATE_GENERATED_CLOCK:
            parts.append(v.get("source"))
            parts.append(v.get("master_clock"))
    if t in (ConstraintType.SET_INPUT_DELAY, ConstraintType.SET_OUTPUT_DELAY,
             ConstraintType.SET_CLOCK_UNCERTAINTY, ConstraintType.SET_CLOCK_LATENCY,
             ConstraintType.SET_CLOCK_TRANSITION, ConstraintType.SET_PROPAGATED_CLOCK):
        parts.append(tuple(sorted(c.clock_refs)))
        parts.append(tuple(sorted(c.target_objects)))
        parts.append(v.get("min_max", "max"))
        parts.append(v.get("edge", "both"))
    if t == ConstraintType.SET_CLOCK_GROUPS:
        # partition is identity
        parts.append(tuple(sorted(tuple(sorted(g)) for g in v.get("groups", []))))
    if t in (ConstraintType.SET_FALSE_PATH, ConstraintType.SET_MULTICYCLE_PATH,
             ConstraintType.SET_MIN_DELAY, ConstraintType.SET_MAX_DELAY):
        if c.path_selector is not None:
            parts.append(c.path_selector.semantic_key())
        else:
            parts.append(())
        if t == ConstraintType.SET_MULTICYCLE_PATH:
            parts.append(v.get("setup_hold", "setup"))
    return tuple(parts)


def _pair_multiset(list_a: list[Constraint], list_b: list[Constraint]
                   ) -> tuple[list[tuple[Constraint, Constraint, str]],
                              list[Constraint], list[Constraint]]:
    """Return (pairs, remaining_a, remaining_b) using the hierarchy:
       1. exact normalized signature (multiset)
       2. unambiguous identity-sig match
       3. single-remainder fallback
    Pairs are reported with a tag describing how they were matched:
       "signature" | "identity" | "single_remainder"
    """
    pairs: list[tuple[Constraint, Constraint, str]] = []
    remaining_a = list(list_a)
    remaining_b = list(list_b)

    # 1) exact normalized signature
    def _sig(c: Constraint) -> tuple:
        return normalize_constraint(c)

    while True:
        matched = False
        for ia, ca in enumerate(remaining_a):
            sa = _sig(ca)
            for ib, cb in enumerate(remaining_b):
                if _sig(cb) == sa:
                    pairs.append((ca, cb, "signature"))
                    remaining_a.pop(ia); remaining_b.pop(ib)
                    matched = True
                    break
            if matched:
                break
        if not matched:
            break

    if not remaining_a or not remaining_b:
        return pairs, remaining_a, remaining_b

    # 2) unambiguous identity signature within group
    while True:
        matched = False
        for ia, ca in enumerate(remaining_a):
            ia_sig = _identity_sig(ca)
            hits = [ib for ib, cb in enumerate(remaining_b)
                    if _identity_sig(cb) == ia_sig]
            if len(hits) == 1:
                ib = hits[0]
                pairs.append((ca, remaining_b[ib], "identity"))
                remaining_a.pop(ia); remaining_b.pop(ib)
                matched = True
                break
        if not matched:
            break

    if not remaining_a or not remaining_b:
        return pairs, remaining_a, remaining_b

    # 3) If exactly one remains on each side, pair them so the user sees
    #    a field-level diff; otherwise leave them as only_in_* (ambiguous).
    if len(remaining_a) == 1 and len(remaining_b) == 1:
        pairs.append((remaining_a[0], remaining_b[0], "single_remainder"))
        remaining_a, remaining_b = [], []

    return pairs, remaining_a, remaining_b


def _legacy_diff(r: ComparisonResult) -> list[DiffEntry]:
    out: list[DiffEntry] = []
    for p in r.different_constraints:
        note = p.fields[0].explanation if p.fields else "semantic difference"
        out.append(DiffEntry(
            action=DiffAction.MODIFIED,
            a_id=p.a_id, b_id=p.b_id,
            note=f"{p.constraint_type}: {note}",
        ))
    for p in r.only_in_left:
        out.append(DiffEntry(action=DiffAction.REMOVED, a_id=p.a_id, note="in A not B"))
    for p in r.only_in_right:
        out.append(DiffEntry(action=DiffAction.ADDED, b_id=p.b_id, note="in B not A"))
    return out


# ---------------------------------------------------------------------------
# SDC text entry point
# ---------------------------------------------------------------------------


def compare_sdc_text(a_text: str, b_text: str,
                     *,
                     importer: Any = None,
                     design: Any = None,
                     tg: Any = None,
                     source_a: str = "<a>",
                     source_b: str = "<b>",
                     ) -> ComparisonResult:
    """High-level helper: import two SDC text strings via the existing
    Step-5 :class:`SdcImporter` and compare the resulting UCMs.

    Security: this function does NOT execute Tcl, shell commands,
    ``eval``, or external processes. The Step-5 importer tokenizes
    SDC into an AST without invoking Tcl.

    Parameters
    ----------
    a_text, b_text : str
        Raw SDC text for sides A and B.
    importer : SdcImporter, optional
        Pre-built importer to reuse (design context, resolver config).
        If not provided one is constructed using ``design``/``tg``.
    design, tg : optional
        Design/TimingGraph context passed to the importer if an
        importer is not supplied.
    source_a, source_b : str
        Synthetic source-file names for diagnostics.

    Import failures produce :class:`ComparisonResult` with
    ``overall_status == ERROR`` and structured diagnostics; raw
    importer exceptions are caught and converted to diagnostic
    entries.
    """
    result = ComparisonResult()
    try:
        # Local import so the equivalence package does not force a
        # hard import-time dependency on the importer (it may be
        # unavailable in minimal unit-test environments where the user
        # only needs UCM-level compare()).
        from ..sdc_importer import SdcImporter
    except Exception as exc:
        result.overall_status = EquivalenceResult.ERROR
        result.diagnostics.append(f"cannot load SDC importer: {exc}")
        return result

    def _do_import(text: str, source: str, side: str
                   ) -> tuple[ConstraintSet | None, list[str]]:
        diags: list[str] = []
        try:
            imp = importer if importer is not None else SdcImporter(
                design=design, tg=tg, source_file=source)
            res = imp.from_text(text, source_file=source)
        except Exception as exc:
            diags.append(f"{side} import raised {type(exc).__name__}: {exc}")
            return None, diags
        # Convert importer diagnostics to strings
        for d in getattr(res, "diagnostics", []) or []:
            try:
                sev = getattr(d, "severity", None)
                msg = getattr(d, "message", str(d))
                code = getattr(d, "code", "")
                diags.append(f"{side}: [{getattr(sev,'value',sev)}] {code} {msg}")
            except Exception:
                diags.append(f"{side}: {d!r}")
        # Count ERROR-level import diagnostics
        err_count = 0
        for ic in getattr(res, "imports", []) or []:
            for d in getattr(ic, "diagnostics", []) or []:
                sev = getattr(d, "severity", None)
                if sev == DiagnosticSeverity.ERROR or (
                        hasattr(sev, "value") and sev.value == "ERROR"):
                    err_count += 1
        err_sev = sum(1 for d in (getattr(res, "diagnostics", []) or [])
                      if (getattr(d, "severity", None) == DiagnosticSeverity.ERROR
                          or (hasattr(getattr(d, "severity", None), "value")
                              and d.severity.value == "ERROR")))
        if err_count + err_sev > 0:
            diags.append(f"{side}: SDC import reported {err_count + err_sev} error(s)")
            return None, diags
        return res.constraint_set, diags

    cset_a, da = _do_import(a_text, source_a, "A")
    cset_b, db = _do_import(b_text, source_b, "B")
    result.diagnostics.extend(da)
    result.diagnostics.extend(db)

    if cset_a is None or cset_b is None:
        result.overall_status = EquivalenceResult.ERROR
        result.comparison_level = ComparisonLevel.UNKNOWN
        return result

    inner = compare(cset_a, cset_b)
    # Transfer fields over
    result.overall_status = inner.overall_status
    result.comparison_level = inner.comparison_level
    result.equivalent_constraints = inner.equivalent_constraints
    result.different_constraints = inner.different_constraints
    result.unknown_constraints = inner.unknown_constraints
    result.only_in_left = inner.only_in_left
    result.only_in_right = inner.only_in_right
    result.duplicates_left = inner.duplicates_left
    result.duplicates_right = inner.duplicates_right
    result.normalization_notes = list(inner.normalization_notes)
    result.diagnostics.extend(inner.diagnostics)
    result._legacy_diff = getattr(inner, "_legacy_diff", [])
    return result
