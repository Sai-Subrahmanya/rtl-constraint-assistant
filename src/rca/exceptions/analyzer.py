"""
Exception analyzer (Manual §30, Step 8).

For each timing exception (``set_false_path``, ``set_multicycle_path``,
``set_min_delay``, ``set_max_delay``) the analyzer performs conservative
structural analysis against the ``Design`` + ``TimingGraph``:

* resolves ``-from`` / ``-to`` / ``-through`` selectors,
* enumerates affected structural paths,
* computes blast radius (paths / endpoints / clocks / scenarios),
* classifies structural findings (BROAD, NO_EFFECT, ...),
* assigns a risk level (LOW / MEDIUM / HIGH / CRITICAL), and
* records structural evidence without ever pronouncing the exception
  "safe" — formal verification or explicit user confirmation is
  required for that.

The analyzer is OBSERVATIONAL: it never mutates the ``ConstraintSet``,
``Design``, or ``TimingGraph``.
"""

from __future__ import annotations

from dataclasses import dataclass, field, asdict
from typing import Any, Iterable

from ..constraint_model import Constraint, ConstraintSet, PathSelector
from ..design_model import Design
from ..timing_model import TimingGraph
from ..utils.enums import (
    ConstraintStatus,
    ConstraintType,
    EmissionStatus,
    ExceptionApprovalStatus,
    ExceptionFindingKind,
    ExceptionRisk,
    Severity,
    VerificationStatus,
)
from ..utils.hashing import stable_hash
from ..utils.logging import get_logger
from .formal_backend import VerificationResult

log = get_logger("exceptions.analyzer")


@dataclass
class ExceptionBlastRadius:
    path_count: int = 0
    endpoint_count: int = 0
    clock_count: int = 0
    scenario_count: int = 0
    affected_startpoints: list[str] = field(default_factory=list)
    affected_endpoints: list[str] = field(default_factory=list)
    affected_clocks: list[str] = field(default_factory=list)
    affected_scenarios: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {
            "path_count": self.path_count,
            "endpoint_count": self.endpoint_count,
            "clock_count": self.clock_count,
            "scenario_count": self.scenario_count,
            "affected_startpoints": list(self.affected_startpoints),
            "affected_endpoints": list(self.affected_endpoints),
            "affected_clocks": list(self.affected_clocks),
            "affected_scenarios": list(self.affected_scenarios),
        }


@dataclass
class ExceptionAnalysisResult:
    """Structural analysis + verification state for one exception.

    ``lifecycle``            — structural analysis phase outcome.
    ``verification``         — formal-backend VerificationResult (if run).
    ``verification_status``  — roll-up of verification evidence alone
                               (NEVER conflated with user approval).
    ``approval_status``      — user authorization.
    ``emission_status``      — final emission decision combining the
                               three plus risk.
    """

    constraint_id: str
    constraint_type: str
    lifecycle: VerificationStatus = VerificationStatus.PROPOSED
    verification: VerificationResult | None = None
    approval_status: ExceptionApprovalStatus = ExceptionApprovalStatus.NONE
    structural_findings: list[dict[str, Any]] = field(default_factory=list)
    risk: ExceptionRisk = ExceptionRisk.MEDIUM
    blast_radius: ExceptionBlastRadius = field(default_factory=ExceptionBlastRadius)
    evidence: dict[str, Any] = field(default_factory=dict)

    # ---------------- derived status views ----------------

    @property
    def verification_status(self) -> VerificationStatus:
        """Verification evidence alone — never influenced by user approval.

        Priority: backend INVALID > backend VERIFIED > backend ERROR >
        structural INVALID > structural NOT_APPLICABLE > structural ERROR >
        UNRESOLVED.
        """
        # Contradictory verification result overrides everything else.
        if self.verification is not None:
            if self.verification.status == VerificationStatus.INVALID:
                return VerificationStatus.INVALID
            if self.verification.status == VerificationStatus.VERIFIED:
                return VerificationStatus.VERIFIED
            if self.verification.status == VerificationStatus.ERROR:
                return VerificationStatus.ERROR
        # Without contradictory backend evidence, structural contradictions
        # still make the exception INVALID (e.g. bad cycle count).
        if self.lifecycle == VerificationStatus.INVALID:
            return VerificationStatus.INVALID
        if self.lifecycle == VerificationStatus.ERROR:
            return VerificationStatus.ERROR
        if self.lifecycle == VerificationStatus.NOT_APPLICABLE:
            return VerificationStatus.NOT_APPLICABLE
        return VerificationStatus.UNRESOLVED

    @property
    def emission_status(self) -> EmissionStatus:
        """Emission decision combining verification + approval + risk."""
        v = self.verification_status
        a = self.approval_status
        if a == ExceptionApprovalStatus.USER_REJECTED:
            return EmissionStatus.BLOCKED_REJECTED
        if v == VerificationStatus.INVALID:
            return EmissionStatus.BLOCKED_INVALID
        if v == VerificationStatus.ERROR:
            return EmissionStatus.BLOCKED_ERROR
        if v == VerificationStatus.NOT_APPLICABLE:
            return EmissionStatus.BLOCKED_NO_EFFECT
        if v == VerificationStatus.VERIFIED:
            if self.risk == ExceptionRisk.CRITICAL \
                    and a != ExceptionApprovalStatus.USER_CONFIRMED:
                return EmissionStatus.BLOCKED_CRITICAL_RISK
            return EmissionStatus.ALLOWED
        # UNRESOLVED
        if a == ExceptionApprovalStatus.USER_CONFIRMED:
            if self.risk == ExceptionRisk.CRITICAL:
                return EmissionStatus.BLOCKED_CRITICAL_RISK
            return EmissionStatus.ALLOWED_USER_CONFIRMED
        # No user approval: emission allowed only in modes that accept
        # unverified exceptions (balanced/exploratory); the emission_status
        # still reports BLOCKED_UNVERIFIED so reporting surfaces it clearly,
        # and is_emittable() implements the mode policy.
        return EmissionStatus.BLOCKED_UNVERIFIED

    def is_emittable(self, mode: str = "strict") -> bool:
        """Emission policy per safe-mode.

        strict      : ALLOWED only (actual VERIFIED; CRITICAL risk
                      blocked unless also USER_CONFIRMED).
        balanced    : ALLOWED, ALLOWED_USER_CONFIRMED, or BLOCKED_UNVERIFIED
                      with non-CRITICAL risk (i.e., UNRESOLVED but narrow
                      enough to emit with a warning).
        exploratory : ALLOWED, ALLOWED_USER_CONFIRMED, or BLOCKED_UNVERIFIED
                      (non-CRITICAL) — used for bring-up.

        INVALID / ERROR / NOT_APPLICABLE / REJECTED / CRITICAL_RISK are
        NEVER emittable in any mode. User confirmation alone never flips
        verification_status to VERIFIED.
        """
        v = self.verification_status
        a = self.approval_status
        if v in (VerificationStatus.INVALID, VerificationStatus.ERROR,
                 VerificationStatus.NOT_APPLICABLE):
            return False
        if a == ExceptionApprovalStatus.USER_REJECTED:
            return False
        if self.risk == ExceptionRisk.CRITICAL \
                and not (v == VerificationStatus.VERIFIED
                         and a == ExceptionApprovalStatus.USER_CONFIRMED):
            return False
        if mode == "strict":
            return v == VerificationStatus.VERIFIED
        if mode == "balanced":
            if v == VerificationStatus.VERIFIED:
                return True
            if a == ExceptionApprovalStatus.USER_CONFIRMED:
                return True
            # Unresolved + narrow (non-CRITICAL): emit with warning in balanced.
            return v == VerificationStatus.UNRESOLVED \
                and self.risk in (ExceptionRisk.LOW, ExceptionRisk.MEDIUM)
        # exploratory: allow any non-invalid/error/rejected/critical
        return True

    def to_dict(self) -> dict[str, Any]:
        return {
            "constraint_id": self.constraint_id,
            "constraint_type": self.constraint_type,
            "lifecycle": self.lifecycle.value,
            "verification_status": self.verification_status.value,
            "approval_status": self.approval_status.value,
            "emission_status": self.emission_status.value,
            "risk": self.risk.value,
            "blast_radius": self.blast_radius.to_dict(),
            "structural_findings": list(self.structural_findings),
            "verification": self.verification.to_dict() if self.verification else None,
            "evidence": dict(self.evidence),
        }


@dataclass
class ExceptionAnalysisReport:
    results: list[ExceptionAnalysisResult] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return {"results": [r.to_dict() for r in self.results],
                "count": len(self.results)}

    def __iter__(self):
        return iter(self.results)

    def __len__(self) -> int:
        return len(self.results)


# ---------------------------------------------------------------------------
# Selector resolution
# ---------------------------------------------------------------------------


_RESET_NAME_HINTS = ("rst", "reset", "rst_n", "reset_n", "arst", "srst")
_TEST_NAME_HINTS = ("scan_en", "test_en", "scan_mode", "test_mode", "bist_en", "jtag")


def _name_hits(name: str, hints: Iterable[str]) -> bool:
    low = name.lower()
    return any(h in low for h in hints)


def _resolve_selector(ps: PathSelector | None, tg: TimingGraph | None,
                      design: Design | None
                      ) -> tuple[set[str], set[str], list[list[str]]]:
    """Return (startpoints, endpoints, through_stages) as concrete string
    sets.  When no selector is provided returns empty sets → interpreted
    as 'applies to everything' by the caller."""
    if ps is None:
        return set(), set(), []
    starts: set[str] = set(ps.from_set)
    ends: set[str] = set(ps.to_set)
    throughs: list[list[str]] = [list(t) for t in ps.through_set]
    return starts, ends, throughs


def _expand_clock_sets(names: set[str], tg: TimingGraph | None) -> set[str]:
    """If a name matches a known clock, return the registers driven by
    that clock plus the clock name itself. Non-clock names are returned
    unchanged. This is a conservative expansion that lets selectors
    specified as -from $clk match regs launched by that clock."""
    if tg is None:
        return set(names)
    out: set[str] = set()
    for n in names:
        if n in tg.clocks:
            out.add(n)
            out.update(tg.clocks[n].registers_driven or [])
        else:
            out.add(n)
    return out


def _paths_matching(tg: TimingGraph | None, starts: set[str], ends: set[str],
                    throughs: list[list[str]]
                    ) -> list[Any]:
    """Return paths that satisfy (-from, -to, -through) selectors.

    ``throughs`` is a list of stages, where each stage is itself a list
    of objects representing one ``-through`` argument. Per SDC/UCM
    semantics, an ordered sequence of stages must be satisfied IN
    ORDER along the path; within a single stage (one ``-through``
    argument with multiple objects) the path must contain at least one
    of the listed objects (OR semantics per -through argument, AND
    across ordered stages).
    """
    if tg is None:
        return []
    exp_starts = _expand_clock_sets(starts, tg)
    exp_ends = _expand_clock_sets(ends, tg)
    matched: list[Any] = []
    for p in tg.paths:
        # if no selectors at all, match all
        if not starts and not ends and not throughs:
            matched.append(p)
            continue
        if starts and p.startpoint not in exp_starts \
                and (p.launch_clock not in starts):
            continue
        if ends and p.endpoint not in exp_ends \
                and (p.capture_clock not in ends):
            continue
        if throughs:
            # Build ordered haystack preserving signal flow:
            #   startpoint → [combinational elements in traversal order] → endpoint
            hay = [p.startpoint] + list(p.combinational_elements or []) + [p.endpoint]
            if not _through_matches_ordered(hay, throughs):
                continue
        matched.append(p)
    return matched


def _through_matches_ordered(hay: list[str], throughs: list[list[str]]) -> bool:
    """Ordered through-stage matching.

    Each throughs[i] is a stage (list of names). Walk the haystack in
    order; after consuming stage i, search the remainder for stage i+1.
    A stage matches if ANY of its names appears in the current search
    window (OR within a -through; AND across ordered stages).
    """
    pos = 0
    for stage in throughs:
        if not stage:
            # Empty stage → trivially satisfied but still advances pos by 0.
            continue
        found = -1
        for j in range(pos, len(hay)):
            if hay[j] in stage:
                found = j
                break
        if found < 0:
            return False
        pos = found + 1
    return True


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------


def analyze_exceptions(design: Design | None,
                       cset: ConstraintSet,
                       tg: TimingGraph | None = None,
                       user_confirmed_ids: set[str] | None = None,
                       user_rejected_ids: set[str] | None = None,
                       ) -> ExceptionAnalysisReport:
    """Structural analysis of every exception in ``cset``.

    This function is deterministic: ordering of results and findings is
    based on constraint id, and no mutation of the inputs occurs.
    """
    user_confirmed_ids = user_confirmed_ids or set()
    user_rejected_ids = user_rejected_ids or set()
    report = ExceptionAnalysisReport()

    # Build clock-group map for overlap analysis
    clock_group_pairs: dict[tuple[str, str], str] = {}
    for c in cset.by_type(ConstraintType.SET_CLOCK_GROUPS):
        groups = c.values.get("groups", [])
        rel = c.values.get("relationship", "asynchronous")
        for gi, g in enumerate(groups):
            for og in groups[gi + 1:]:
                for a in g:
                    for b in og:
                        if a != b:
                            clock_group_pairs[tuple(sorted((a, b)))] = rel

    for c in sorted(cset.exceptions(), key=lambda x: x.id):
        r = ExceptionAnalysisResult(
            constraint_id=c.id,
            constraint_type=c.type.value,
            lifecycle=VerificationStatus.STRUCTURALLY_ANALYZED,
        )
        # Approval status is independent of verification.
        if c.id in user_rejected_ids:
            r.approval_status = ExceptionApprovalStatus.USER_REJECTED
        elif c.id in user_confirmed_ids or _is_user_fixed(c):
            r.approval_status = ExceptionApprovalStatus.USER_CONFIRMED
        else:
            r.approval_status = ExceptionApprovalStatus.NONE
        try:
            _analyze_one(c, r, design, tg, clock_group_pairs)
        except Exception as exc:  # defensive: analysis never breaks pipeline
            log.exception("exception analysis failed for %s: %s", c.id, exc)
            r.lifecycle = VerificationStatus.ERROR
            r.evidence["error"] = str(exc)
        report.results.append(r)

    return report


def _is_user_fixed(c: Constraint) -> bool:
    # Constraints explicitly marked FIXED status are treated as
    # user-confirmed for the approval-status (NOT as formally verified).
    try:
        return c.status == ConstraintStatus.FIXED
    except Exception:
        return False


def _analyze_one(c: Constraint, r: ExceptionAnalysisResult,
                 design: Design | None, tg: TimingGraph | None,
                 clock_group_pairs: dict[tuple[str, str], str]) -> None:
    ps = c.path_selector
    starts, ends, throughs = _resolve_selector(ps, tg, design)
    matched = _paths_matching(tg, starts, ends, throughs)

    r.evidence["selector"] = {
        "from": sorted(starts), "to": sorted(ends),
        "through": [list(t) for t in throughs],
    }

    # Blast radius
    br = ExceptionBlastRadius()
    br.path_count = len(matched)
    sps = sorted({p.startpoint for p in matched})
    eps = sorted({p.endpoint for p in matched})
    clks = sorted({p.launch_clock for p in matched if p.launch_clock}
                  | {p.capture_clock for p in matched if p.capture_clock})
    scens = sorted(set(c.scenario_ids or []))
    br.affected_startpoints = sps
    br.affected_endpoints = eps
    br.affected_clocks = clks
    br.affected_scenarios = scens
    br.endpoint_count = len(eps)
    br.clock_count = len(clks)
    br.scenario_count = len(scens)
    r.blast_radius = br

    r.evidence["path_count"] = br.path_count
    r.evidence["endpoint_count"] = br.endpoint_count
    r.evidence["clock_count"] = br.clock_count

    # ---------------- type-specific structural checks ----------------
    if c.type == ConstraintType.SET_FALSE_PATH:
        _false_path_findings(c, r, starts, ends, throughs, matched, tg, clock_group_pairs)
    elif c.type == ConstraintType.SET_MULTICYCLE_PATH:
        _multicycle_findings(c, r, starts, ends, throughs, matched, tg)
    else:  # set_min_delay / set_max_delay
        _generic_delay_exception_findings(c, r, starts, ends, matched)

    # Risk classification (blast-radius based)
    r.risk = _classify_risk(c, r, starts, ends, matched)

    # Lifecycle synthesis
    if any(f["kind"] == ExceptionFindingKind.NO_EFFECT.value for f in r.structural_findings):
        r.lifecycle = VerificationStatus.NOT_APPLICABLE
    elif any(f["severity"] == Severity.ERROR.value
             for f in r.structural_findings):
        # Hard contradictions (bad cycles, etc.) are INVALID. BROAD / CRITICAL
        # risk by itself is not a contradiction — it's a review signal and is
        # handled via the emission gate + risk classification.
        r.lifecycle = VerificationStatus.INVALID
    else:
        r.lifecycle = VerificationStatus.STRUCTURALLY_ANALYZED


def _add_finding(r: ExceptionAnalysisResult,
                 kind: ExceptionFindingKind,
                 severity: Severity,
                 message: str, **kw: Any) -> None:
    f = {
        "kind": kind.value,
        "severity": severity.value,
        "message": message,
        "finding_id": "F" + stable_hash((
            r.constraint_id, kind.value, message[:160]
        ))[:8].upper(),
    }
    f.update(kw)
    r.structural_findings.append(f)


def _false_path_findings(c: Constraint, r: ExceptionAnalysisResult,
                         starts: set[str], ends: set[str],
                         throughs: list[list[str]],
                         matched: list, tg: TimingGraph | None,
                         clock_group_pairs: dict[tuple[str, str], str]) -> None:
    ps = c.path_selector

    # BROAD selector: no -from/-to/-through
    if ps is None or ps.is_empty():
        _add_finding(r, ExceptionFindingKind.BROAD, Severity.CRITICAL,
                     "set_false_path with no -from/-to/-through disables all timing checks.",
                     suggestion="Add -from/-to/-through to restrict the exception.")

    # NO_EFFECT: selector resolves to zero paths (graph available).
    if tg is not None and not matched and not (ps is None or ps.is_empty()):
        _add_finding(r, ExceptionFindingKind.NO_EFFECT, Severity.WARNING,
                     "set_false_path selector matches zero structural paths.",
                     suggestion="The exception may be redundant or misspelled; verify -from/-to/-through names.")

    # CLOCK_DOMAIN_CROSSING: exception spans two different clocks
    cross_clocks = [p for p in matched
                    if p.launch_clock and p.capture_clock
                    and p.launch_clock != p.capture_clock]
    if cross_clocks:
        pair_set: set[tuple[str, str]] = set()
        for p in cross_clocks:
            pair_set.add(tuple(sorted((p.launch_clock, p.capture_clock))))
        _add_finding(r, ExceptionFindingKind.CLOCK_DOMAIN_CROSSING, Severity.HIGH,
                     f"set_false_path covers {len(cross_clocks)} cross-clock path(s) "
                     f"across {len(pair_set)} clock pair(s); requires review or proof.",
                     pairs=[list(p) for p in sorted(pair_set)])
        # Interact with clock groups: note overlap but never call redundant.
        for pair in sorted(pair_set):
            if pair in clock_group_pairs:
                _add_finding(r, ExceptionFindingKind.CLOCK_GROUP_OVERLAP,
                             Severity.INFO,
                             f"set_false_path overlaps with set_clock_groups -"
                             f"{clock_group_pairs[pair]} between {pair[0]} and {pair[1]}.")
        r.evidence["cross_clock_pair_count"] = len(pair_set)

    # RESET / TEST hints (naming only — never auto-classify as safe)
    names = starts | ends | {t for st in throughs for t in st}
    if any(_name_hits(n, _RESET_NAME_HINTS) for n in names):
        _add_finding(r, ExceptionFindingKind.RESET_RELATED, Severity.MEDIUM,
                     "set_false_path selector references reset-named objects; "
                     "verify reset timing policy before relying on this exception.",
                     suggestion="Reset paths often need specific recovery/removal checks; naming alone is not sufficient.")
    if any(_name_hits(n, _TEST_NAME_HINTS) for n in names):
        _add_finding(r, ExceptionFindingKind.TEST_MODE, Severity.INFO,
                     "set_false_path selector references test/scan-named objects; "
                     "ensure the exception is guarded by a test-mode scenario.")

    # Broad scope => REQUIRES_FORMAL_VERIFICATION
    if r.blast_radius.path_count > 100 or (ps is None or ps.is_empty()):
        _add_finding(r, ExceptionFindingKind.REQUIRES_FORMAL_VERIFICATION,
                     Severity.HIGH,
                     "Exception has broad blast radius; formal verification or explicit user approval required before strict-mode emission.")


def _multicycle_findings(c: Constraint, r: ExceptionAnalysisResult,
                         starts: set[str], ends: set[str],
                         throughs: list[list[str]],
                         matched: list, tg: TimingGraph | None) -> None:
    cycles = c.values.get("cycles")
    # cycle count
    try:
        c_i = int(cycles)
        if c_i < 0:
            raise ValueError
    except Exception:
        _add_finding(r, ExceptionFindingKind.CYCLE_COUNT_INVALID, Severity.ERROR,
                     f"set_multicycle_path has invalid -cycles value {cycles!r}.",
                     suggestion="-cycles must be a non-negative integer.")
        return
    if c_i == 0:
        _add_finding(r, ExceptionFindingKind.CYCLE_COUNT_INVALID, Severity.ERROR,
                     "set_multicycle_path -cycles 0 is not meaningful.",
                     suggestion="Use -cycles 1 (default) or greater.")

    # setup/hold incoherence
    if bool(c.values.get("start")) and bool(c.values.get("end")):
        _add_finding(r, ExceptionFindingKind.SETUP_HOLD_INCOHERENT, Severity.WARNING,
                     "set_multicycle_path specifies both -start and -end (likely unintended).")

    # no effect
    if tg is not None and not matched:
        _add_finding(r, ExceptionFindingKind.NO_EFFECT, Severity.WARNING,
                     "set_multicycle_path selector matches zero structural paths.",
                     suggestion="Verify -from/-to/-through names against the elaborated design.")

    # cross-clock without explicit evidence
    cross = [p for p in matched
             if p.launch_clock and p.capture_clock
             and p.launch_clock != p.capture_clock]
    if cross:
        _add_finding(r, ExceptionFindingKind.CLOCK_DOMAIN_CROSSING, Severity.HIGH,
                     f"set_multicycle_path covers {len(cross)} cross-clock path(s).",
                     suggestion="Cross-clock multicycles require explicit design evidence (e.g. handshaking, known divider ratio).")

    # evidence for multicycle relationship
    if r.blast_radius.path_count > 0:
        r.evidence["mean_slack_ns"] = _mean_slack_ns(matched)
        r.evidence["cycle_count"] = c_i
    else:
        _add_finding(r, ExceptionFindingKind.MULTICYCLE_NO_EVIDENCE, Severity.MEDIUM,
                     "set_multicycle_path has no structural path evidence.")

    if r.blast_radius.path_count > 200:
        _add_finding(r, ExceptionFindingKind.REQUIRES_FORMAL_VERIFICATION,
                     Severity.HIGH,
                     "Multicycle has very broad blast radius; requires formal verification or explicit approval.")


def _generic_delay_exception_findings(c: Constraint, r: ExceptionAnalysisResult,
                                      starts: set[str], ends: set[str],
                                      matched: list) -> None:
    if not matched and starts and ends:
        _add_finding(r, ExceptionFindingKind.NO_EFFECT, Severity.WARNING,
                     f"{c.type.value} selector matches zero structural paths.")
    if r.blast_radius.path_count > 200:
        _add_finding(r, ExceptionFindingKind.BROAD, Severity.HIGH,
                     f"{c.type.value} has broad blast radius ({r.blast_radius.path_count} paths).")


def _classify_risk(c: Constraint, r: ExceptionAnalysisResult,
                   starts: set[str], ends: set[str],
                   matched: list) -> ExceptionRisk:
    if any(f["severity"] == Severity.CRITICAL.value for f in r.structural_findings):
        return ExceptionRisk.CRITICAL
    if r.blast_radius.path_count > 200:
        return ExceptionRisk.CRITICAL
    if any(f["severity"] == Severity.HIGH.value for f in r.structural_findings):
        return ExceptionRisk.HIGH
    if r.blast_radius.path_count == 0:
        return ExceptionRisk.LOW
    if r.blast_radius.path_count > 20:
        return ExceptionRisk.HIGH
    if (starts and ends) or r.blast_radius.clock_count >= 2:
        return ExceptionRisk.MEDIUM
    return ExceptionRisk.LOW


def _mean_slack_ns(paths: list) -> float | None:
    vals = [p.slack for p in paths if getattr(p, "slack", None) is not None]
    if not vals:
        return None
    return round((sum(vals) / len(vals)) * 1e9, 3)
