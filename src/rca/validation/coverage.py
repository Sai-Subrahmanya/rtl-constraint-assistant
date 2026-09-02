"""Coverage engine (Manual §28, Step 7 §12–§13, §13 revisions).

Reports coverage per *structural* timing-path category when a
:class:`TimingGraph` is available.  Where exact coverage cannot be
established the value is reported as ``UNKNOWN`` rather than
fabricating a percentage.

Sub-metrics (explicitly separated so they are never conflated):

* **Clock source coverage** — how many clock sources in the graph have
  an accompanying ``create_clock`` / ``create_generated_clock``.
* **Input timing path coverage** — structurally-known INPUT_TO_REGISTER
  and INPUT_TO_OUTPUT paths where the input has a usable
  ``set_input_delay`` tied to a defined launch clock.
* **Output timing path coverage** — REGISTER_TO_OUTPUT and
  INPUT_TO_OUTPUT paths with a usable ``set_output_delay``.
* **Register-to-register coverage** — REGISTER_TO_REGISTER paths whose
  launch/capture clocks are both defined and (if they differ) whose
  CDC relationship is explicitly handled.
* **CDC *path* coverage** — structurally-identified CDC paths whose
  timing is explicitly handled (false_path, clock_groups with an
  appropriate relationship, or max/min exception explicitly covering
  the crossing).  This is *not* the same thing as a clock pair being
  known.
* **Clock relationship coverage** — unordered clock-domain pairs with
  an explicit ``set_clock_groups`` declaration (or same-domain, which
  is implicitly handled).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from ..constraint_model import Constraint, ConstraintSet
from ..design_model import Design
from ..timing_model import TimingGraph
from ..utils.enums import (
    ClockDomainRelationship,
    ConstraintType,
    ErrorCode,
    Severity,
    TimingPathClass,
    UncoveredClassification,
    ValidationCategory,
)
from ..utils.logging import get_logger
from .base import ValidationIssue, ValidationReport, _issue

log = get_logger("validation.coverage")

UNKNOWN = "UNKNOWN"


# ---------------------------------------------------------------------------
# Data structure
# ---------------------------------------------------------------------------


@dataclass
class CoverageReport:
    # --- Clock source coverage ---
    clock_sources_total: int = 0
    clock_sources_covered: int = 0

    # --- Structural path-based coverage ---
    input_paths_total: int = 0
    input_paths_covered: int = 0
    output_paths_total: int = 0
    output_paths_covered: int = 0
    reg_to_reg_total: int = 0
    reg_to_reg_covered: int = 0

    # --- CDC path coverage (actual structural CDC paths) ---
    cdc_paths_total: int = 0
    cdc_paths_covered: int = 0

    # --- Clock-domain relationship coverage (domain pairs, NOT paths) ---
    clock_relationship_pairs_total: int = 0
    clock_relationship_pairs_handled: int = 0

    # Graph availability flag
    graph_available: bool = False

    # Human-readable uncovered entries (paths / objects)
    uncovered: list[dict[str, Any]] = field(default_factory=list)

    # ---------------- helpers ----------------

    def _pct(self, covered: int, total: int) -> Any:
        if not self.graph_available:
            return UNKNOWN
        if total == 0:
            # Structural graph present but zero applicable objects/paths in
            # this category — semantically NOT_APPLICABLE rather than 100%.
            return "NOT_APPLICABLE"
        return round(100.0 * covered / total, 1)

    def as_dict(self) -> dict[str, Any]:
        return {
            "clock_source_coverage_pct":
                self._pct(self.clock_sources_covered, self.clock_sources_total),
            "input_timing_path_coverage_pct":
                self._pct(self.input_paths_covered, self.input_paths_total),
            "output_timing_path_coverage_pct":
                self._pct(self.output_paths_covered, self.output_paths_total),
            "reg_to_reg_coverage_pct":
                self._pct(self.reg_to_reg_covered, self.reg_to_reg_total),
            "cdc_path_coverage_pct":
                self._pct(self.cdc_paths_covered, self.cdc_paths_total),
            "clock_relationship_coverage_pct":
                self._pct(self.clock_relationship_pairs_handled,
                          self.clock_relationship_pairs_total),
            "totals": {
                "clock_sources": self.clock_sources_total,
                "input_paths": self.input_paths_total,
                "output_paths": self.output_paths_total,
                "reg_reg_paths": self.reg_to_reg_total,
                "cdc_paths": self.cdc_paths_total,
                "clock_pairs": self.clock_relationship_pairs_total,
            },
            "graph_available": self.graph_available,
            "uncovered": list(self.uncovered),
        }


# ---------------------------------------------------------------------------
# Public entry point
# ---------------------------------------------------------------------------


def compute_coverage(design: Design | None, tg: TimingGraph | None,
                     cset: ConstraintSet,
                     val_report: ValidationReport) -> CoverageReport:
    rep = CoverageReport()

    if design is None or tg is None:
        rep.graph_available = False
        _issue(val_report, Severity.INFO, ValidationCategory.COVERAGE,
               ErrorCode.COVERAGE_UNKNOWN,
               "Coverage cannot be computed: design or timing graph unavailable.",
               blocking=False,
               suggestion="Provide an elaborated design and timing graph for path-based coverage.")
        return rep

    rep.graph_available = True

    # Pre-index constraint set
    clocks_by_name, gclocks_by_name = _index_clocks(cset)
    constrained_clocks: set[str] = set(clocks_by_name) | set(gclocks_by_name)

    input_delays = _index_io_delays(cset, ConstraintType.SET_INPUT_DELAY)
    output_delays = _index_io_delays(cset, ConstraintType.SET_OUTPUT_DELAY)

    clock_group_pairs, handled_cdc_pairs = _index_clock_groups(cset)
    explicit_cdc_path_exceptions = _index_cdc_path_exceptions(cset)

    # ----- 1. Clock source coverage -----
    graph_clock_names = set(tg.clocks.keys())
    rep.clock_sources_total = len(graph_clock_names)
    rep.clock_sources_covered = len(graph_clock_names & constrained_clocks)
    for n in sorted(graph_clock_names - constrained_clocks):
        rep.uncovered.append(_entry(
            category="clock_source",
            classification=UncoveredClassification.REQUIRES_USER_DECISION,
            object=n,
            message=f"Clock '{n}' has no create_clock/create_generated_clock constraint.",
            startpoint=n, endpoint=None, launch=None, capture=None,
            reason="No create_clock or create_generated_clock references this clock.",
            suggestion="Add a create_clock or create_generated_clock for this clock."))
        _issue(val_report, Severity.ERROR, ValidationCategory.COVERAGE,
               ErrorCode.COVERAGE_CLOCK_GAP,
               f"Uncovered clock source: '{n}'.",
               object_names=[n], blocking=True,
               suggestion="Add a create_clock or create_generated_clock for this clock.")

    # ----- 2. Structural path classification -----
    # Partition all structural timing paths
    reg_paths: list = []
    cdc_paths: list = []
    input_paths: list = []
    output_paths: list = []
    for p in tg.paths:
        pt = p.path_type
        if pt == TimingPathClass.REG_TO_REG:
            reg_paths.append(p)
        elif pt == TimingPathClass.CDC:
            cdc_paths.append(p)
        elif pt in (TimingPathClass.INPUT_TO_REG, TimingPathClass.INPUT_TO_OUTPUT):
            input_paths.append(p)
            if pt == TimingPathClass.INPUT_TO_OUTPUT:
                output_paths.append(p)
        elif pt in (TimingPathClass.REG_TO_OUTPUT,):
            output_paths.append(p)
        # Other path types (clock_to_control, reset_to_register, test_scan, etc.)
        # are tracked only if they carry clock info.

    # ----- 3. Register-to-register coverage -----
    _assess_reg_reg(rep, val_report, reg_paths, constrained_clocks,
                    clock_group_pairs, explicit_cdc_path_exceptions)

    # ----- 4. CDC path coverage -----
    _assess_cdc_paths(rep, val_report, cdc_paths, constrained_clocks,
                      clock_group_pairs, handled_cdc_pairs,
                      explicit_cdc_path_exceptions)

    # ----- 5. Clock relationship coverage (distinct from CDC paths) -----
    _assess_clock_relationships(rep, val_report, tg, clock_group_pairs)

    # ----- 6. Input timing coverage (graph-aware) -----
    _assess_input_paths(rep, val_report, input_paths, constrained_clocks,
                        input_delays)

    # ----- 7. Output timing coverage (graph-aware) -----
    _assess_output_paths(rep, val_report, output_paths, constrained_clocks,
                         output_delays)

    return rep


# ---------------------------------------------------------------------------
# Path assessors
# ---------------------------------------------------------------------------


def _assess_reg_reg(rep: CoverageReport, report: ValidationReport,
                    paths: list, constrained_clocks: set[str],
                    clock_group_pairs: set[tuple[str, str]],
                    cdc_exceptions: set[tuple[str, str]]) -> None:
    rep.reg_to_reg_total = len(paths)
    for p in sorted(paths, key=lambda x: (x.startpoint, x.endpoint)):
        lc, cc = p.launch_clock, p.capture_clock
        lc_ok = lc in constrained_clocks
        cc_ok = cc in constrained_clocks

        if not lc or not cc:
            rep.uncovered.append(_entry(
                category="reg_to_reg",
                classification=UncoveredClassification.UNKNOWN,
                object=f"{p.startpoint}->{p.endpoint}",
                message=f"Reg-to-reg path {p.startpoint} -> {p.endpoint} has unknown launch/capture clock.",
                startpoint=p.startpoint, endpoint=p.endpoint,
                launch=lc, capture=cc,
                reason="Launch or capture clock unresolved in timing graph."))
            _issue(report, Severity.WARNING, ValidationCategory.COVERAGE,
                   ErrorCode.COVERAGE_REG2REG_GAP,
                   f"Reg-to-reg path {p.startpoint} -> {p.endpoint} has an unknown clock.",
                   object_names=[p.startpoint, p.endpoint], blocking=False)
            continue
        if not lc_ok or not cc_ok:
            # One of the clocks has no create_clock constraint.
            missing = [c for c in (lc, cc) if c not in constrained_clocks]
            rep.uncovered.append(_entry(
                category="reg_to_reg",
                classification=UncoveredClassification.UNCONSTRAINED,
                object=f"{p.startpoint}->{p.endpoint}",
                message=f"Reg-to-reg path {p.startpoint} -> {p.endpoint} references unconstrained clock(s): {missing}.",
                startpoint=p.startpoint, endpoint=p.endpoint,
                launch=lc, capture=cc,
                reason=f"Clock(s) {missing} have no create_clock constraint."))
            _issue(report, Severity.ERROR, ValidationCategory.COVERAGE,
                   ErrorCode.COVERAGE_CLOCK_GAP,
                   f"Clock(s) {missing} on reg-to-reg path {p.startpoint} -> {p.endpoint} lack create_clock.",
                   object_names=missing, blocking=True)
            continue
        if lc == cc:
            rep.reg_to_reg_covered += 1
            continue
        # Cross-clock reg2reg: requires explicit relationship/exception.
        pair = _pair(lc, cc)
        if pair in clock_group_pairs or pair in cdc_exceptions:
            rep.reg_to_reg_covered += 1
        else:
            rep.uncovered.append(_entry(
                category="reg_to_reg",
                classification=UncoveredClassification.REQUIRES_USER_DECISION,
                object=f"{p.startpoint}->{p.endpoint}",
                message=f"Cross-clock reg-to-reg path {p.startpoint} ({lc}) -> {p.endpoint} ({cc}) lacks explicit handling.",
                startpoint=p.startpoint, endpoint=p.endpoint,
                launch=lc, capture=cc,
                reason=f"No explicit clock relationship or timing exception between '{lc}' and '{cc}'."))
            _issue(report, Severity.WARNING, ValidationCategory.COVERAGE,
                   ErrorCode.COVERAGE_REG2REG_GAP,
                   f"Cross-clock reg-to-reg path {p.startpoint} ({lc}) -> {p.endpoint} ({cc}) lacks explicit handling.",
                   object_names=[p.startpoint, p.endpoint, lc, cc], blocking=False)
    # Aggregate informational issue (one per run)
    missing_ct = rep.reg_to_reg_total - rep.reg_to_reg_covered
    if missing_ct:
        # individual issues were already emitted for non-covered cases;
        # avoid double-counting as an extra ERROR-level issue.
        pass


def _assess_cdc_paths(rep: CoverageReport, report: ValidationReport,
                      cdc_paths: list, constrained_clocks: set[str],
                      clock_group_pairs: set[tuple[str, str]],
                      handled_cdc_pairs: set[tuple[str, str]],
                      cdc_exceptions: set[tuple[str, str]]) -> None:
    rep.cdc_paths_total = len(cdc_paths)
    for p in sorted(cdc_paths, key=lambda x: (x.startpoint, x.endpoint)):
        lc, cc = p.launch_clock, p.capture_clock
        if not lc or not cc or lc not in constrained_clocks or cc not in constrained_clocks:
            rep.uncovered.append(_entry(
                category="cdc_path",
                classification=UncoveredClassification.UNKNOWN,
                object=f"{p.startpoint}->{p.endpoint}",
                message=f"CDC path {p.startpoint} ({lc}) -> {p.endpoint} ({cc}) has unresolved clock(s).",
                startpoint=p.startpoint, endpoint=p.endpoint,
                launch=lc, capture=cc,
                reason="One or both endpoint clocks are undefined."))
            _issue(report, Severity.ERROR, ValidationCategory.COVERAGE,
                   ErrorCode.COVERAGE_CDC_GAP,
                   f"CDC path {p.startpoint} -> {p.endpoint} has unresolved clock(s).",
                   object_names=[p.startpoint, p.endpoint], blocking=True)
            continue
        pair = _pair(lc, cc)
        # Conservative policy for CDC: an "asynchronous" or "physically_exclusive"
        # clock-groups declaration is NECESSARY but NOT sufficient on its own — we
        # also require either a synchronizer note (not yet modeled) OR a false_path
        # / max_delay exception that explicitly covers the crossing, EXCEPT when
        # the user declared asynchronous groups AND we are in balanced mode we
        # mark it as handled-by-relationship (clock_relationship coverage improves,
        # but cdc_path coverage is only counted when there is an explicit exception).
        if pair in cdc_exceptions:
            rep.cdc_paths_covered += 1
            continue
        # Async group known but no explicit path-level exception → not covered
        # for CDC timing handling (clock_relationship metric captures the group
        # knowledge separately).
        rel_kind = handled_cdc_pairs.get(pair)
        reason = (f"Clocks '{lc}' and '{cc}' have group relationship '{rel_kind}' "
                  "but no path-level timing exception (set_false_path / set_max_delay) "
                  "covers this CDC crossing.")
        rep.uncovered.append(_entry(
            category="cdc_path",
            classification=UncoveredClassification.REQUIRES_USER_DECISION,
            object=f"{p.startpoint}->{p.endpoint}",
            message=f"CDC path {p.startpoint} ({lc}) -> {p.endpoint} ({cc}) lacks explicit path-level timing handling.",
            startpoint=p.startpoint, endpoint=p.endpoint,
            launch=lc, capture=cc, reason=reason,
            suggestion="Declare the clock relationship (e.g. set_clock_groups -asynchronous) where appropriate, AND add explicit path-level handling (set_false_path / set_max_delay) or record verified synchronizer/CDC handling for the crossing. An asynchronous group declaration alone does not prove the data path is safely constrained."))
        _issue(report, Severity.ERROR, ValidationCategory.COVERAGE,
               ErrorCode.COVERAGE_CDC_GAP,
               f"CDC path {p.startpoint} ({lc}) -> {p.endpoint} ({cc}) lacks explicit path-level timing handling.",
               object_names=[p.startpoint, p.endpoint, lc, cc], blocking=True,
               suggestion="Declare the clock relationship separately where appropriate, and add explicit path-level handling (set_false_path / set_max_delay) or verified CDC metadata for this crossing. Async groups alone do not prove the data path is safe.")


def _assess_clock_relationships(rep: CoverageReport, report: ValidationReport,
                                tg: TimingGraph,
                                clock_group_pairs: set[tuple[str, str]]) -> None:
    pairs: set[tuple[str, str]] = set()
    # Collect unordered clock pairs from domain_edges AND from any paths.
    for e in tg.domain_edges:
        if e.clock_a and e.clock_b and e.clock_a != e.clock_b:
            pairs.add(_pair(e.clock_a, e.clock_b))
    for p in tg.paths:
        if p.launch_clock and p.capture_clock and p.launch_clock != p.capture_clock:
            pairs.add(_pair(p.launch_clock, p.capture_clock))
    rep.clock_relationship_pairs_total = len(pairs)
    handled = pairs & clock_group_pairs
    rep.clock_relationship_pairs_handled = len(handled)
    for a, b in sorted(pairs - clock_group_pairs):
        # Same-domain is implicitly handled — but these are all different-domain pairs.
        rep.uncovered.append(_entry(
            category="clock_relationship",
            classification=UncoveredClassification.REQUIRES_USER_DECISION,
            object=f"{a}<->{b}",
            message=f"Clock relationship between '{a}' and '{b}' is unknown.",
            startpoint=a, endpoint=b, launch=a, capture=b,
            reason="No set_clock_groups declaration for this clock pair.",
            suggestion="Add set_clock_groups -asynchronous or a logical/physical exclusion, or confirm synchronous crossing."))
        _issue(report, Severity.ERROR, ValidationCategory.COVERAGE,
               ErrorCode.COVERAGE_CDC_GAP,
               f"Clock relationship unknown: '{a}' <-> '{b}'.",
               object_names=[a, b], blocking=True,
               suggestion="Add set_clock_groups -asynchronous, -logically_exclusive, or -physically_exclusive as appropriate.")


def _assess_input_paths(rep: CoverageReport, report: ValidationReport,
                        input_paths: list, constrained_clocks: set[str],
                        input_delays: dict[tuple, list[Constraint]]) -> None:
    rep.input_paths_total = len(input_paths)
    for p in sorted(input_paths, key=lambda x: (x.startpoint, x.endpoint)):
        in_port = p.startpoint
        cap_clk = p.capture_clock
        if not cap_clk:
            rep.uncovered.append(_entry(
                category="input_timing_path",
                classification=UncoveredClassification.UNKNOWN,
                object=f"{in_port}->{p.endpoint}",
                message=f"Input path {in_port} -> {p.endpoint} has unknown capture clock.",
                startpoint=in_port, endpoint=p.endpoint,
                launch=None, capture=cap_clk,
                reason="Capture clock unresolved in timing graph."))
            _issue(report, Severity.WARNING, ValidationCategory.COVERAGE,
                   ErrorCode.COVERAGE_INPUT_GAP,
                   f"Input path {in_port} -> {p.endpoint} has unresolved capture clock.",
                   object_names=[in_port, p.endpoint], blocking=False)
            continue
        if cap_clk not in constrained_clocks:
            rep.uncovered.append(_entry(
                category="input_timing_path",
                classification=UncoveredClassification.UNCONSTRAINED,
                object=f"{in_port}->{p.endpoint}",
                message=f"Input path {in_port} -> {p.endpoint} references capture clock '{cap_clk}' which has no create_clock constraint.",
                startpoint=in_port, endpoint=p.endpoint,
                launch=None, capture=cap_clk,
                reason=f"Capture clock '{cap_clk}' has no create_clock/create_generated_clock."))
            _issue(report, Severity.ERROR, ValidationCategory.COVERAGE,
                   ErrorCode.COVERAGE_INPUT_GAP,
                   f"Input path {in_port} -> {p.endpoint}: capture clock '{cap_clk}' is unconstrained.",
                   object_names=[in_port, p.endpoint, cap_clk], blocking=True)
            continue
        # Collect set_input_delay constraints targeting this port.
        port_delays = [(_clk, cs) for (_port, _clk, _mm, _edge, _add), cs
                       in input_delays.items() if _port == in_port]
        # Clock-exact match (required for path-level coverage credit).
        matching = [c for _clk, cs in port_delays for c in cs if _clk == cap_clk]
        # Other-clock delays on the same port — these exist but don't cover
        # THIS path because the clock doesn't match the structural capture clock.
        wrong_clock = [c for _clk, cs in port_delays for c in cs if _clk != cap_clk]
        if matching:
            rep.input_paths_covered += 1
            continue
        if wrong_clock:
            wrong_names = sorted({c.values.get("clock") for c in wrong_clock
                                  if c.values.get("clock")})
            rep.uncovered.append(_entry(
                category="input_timing_path",
                classification=UncoveredClassification.REQUIRES_USER_DECISION,
                object=f"{in_port}->{p.endpoint}",
                message=(f"Input path {in_port} -> {p.endpoint} has set_input_delay "
                         f"constraint(s) but they reference clock(s) {wrong_names} "
                         f"while the structural capture clock is '{cap_clk}'."),
                startpoint=in_port, endpoint=p.endpoint,
                launch=None, capture=cap_clk,
                reason=(f"set_input_delay on '{in_port}' is associated with "
                        f"clock(s) {wrong_names}, not the path's capture clock "
                        f"'{cap_clk}'."),
                suggestion=(f"Add or correct set_input_delay on '{in_port}' to "
                            f"reference the structural capture clock '{cap_clk}', "
                            f"or confirm the clock relationship if the delay is intentional.")))
            _issue(report, Severity.WARNING, ValidationCategory.COVERAGE,
                   ErrorCode.COVERAGE_INPUT_GAP,
                   f"Input timing clock mismatch: {in_port} -> {p.endpoint} "
                   f"captured by '{cap_clk}' but set_input_delay uses {wrong_names}.",
                   object_names=[in_port, p.endpoint, cap_clk] + wrong_names,
                   blocking=False,
                   suggestion=(f"Add a set_input_delay on '{in_port}' that references "
                               f"the capture clock '{cap_clk}'."))
            continue
        # No set_input_delay at all for this port.
        rep.uncovered.append(_entry(
            category="input_timing_path",
            classification=UncoveredClassification.UNCONSTRAINED,
            object=f"{in_port}->{p.endpoint}",
            message=f"Input path {in_port} -> {p.endpoint} has no applicable set_input_delay.",
            startpoint=in_port, endpoint=p.endpoint,
            launch=None, capture=cap_clk,
            reason=f"No set_input_delay on '{in_port}' references a defined clock."))
        _issue(report, Severity.WARNING, ValidationCategory.COVERAGE,
               ErrorCode.COVERAGE_INPUT_GAP,
               f"Input timing gap: {in_port} -> {p.endpoint} has no applicable set_input_delay.",
               object_names=[in_port, p.endpoint], blocking=False,
               suggestion=f"Add set_input_delay on {in_port} referencing the capture clock '{cap_clk}'.")


def _assess_output_paths(rep: CoverageReport, report: ValidationReport,
                         output_paths: list, constrained_clocks: set[str],
                         output_delays: dict[tuple, list[Constraint]]) -> None:
    rep.output_paths_total = len(output_paths)
    for p in sorted(output_paths, key=lambda x: (x.startpoint, x.endpoint)):
        out_port = p.endpoint
        launch_clk = p.launch_clock
        if not launch_clk:
            rep.uncovered.append(_entry(
                category="output_timing_path",
                classification=UncoveredClassification.UNKNOWN,
                object=f"{p.startpoint}->{out_port}",
                message=f"Output path {p.startpoint} -> {out_port} has unknown launch clock.",
                startpoint=p.startpoint, endpoint=out_port,
                launch=launch_clk, capture=None,
                reason="Launch clock unresolved in timing graph."))
            _issue(report, Severity.WARNING, ValidationCategory.COVERAGE,
                   ErrorCode.COVERAGE_OUTPUT_GAP,
                   f"Output path {p.startpoint} -> {out_port} has unresolved launch clock.",
                   object_names=[p.startpoint, out_port], blocking=False)
            continue
        if launch_clk not in constrained_clocks:
            rep.uncovered.append(_entry(
                category="output_timing_path",
                classification=UncoveredClassification.UNCONSTRAINED,
                object=f"{p.startpoint}->{out_port}",
                message=f"Output path {p.startpoint} -> {out_port} references launch clock '{launch_clk}' which has no create_clock constraint.",
                startpoint=p.startpoint, endpoint=out_port,
                launch=launch_clk, capture=None,
                reason=f"Launch clock '{launch_clk}' has no create_clock/create_generated_clock."))
            _issue(report, Severity.ERROR, ValidationCategory.COVERAGE,
                   ErrorCode.COVERAGE_OUTPUT_GAP,
                   f"Output path {p.startpoint} -> {out_port}: launch clock '{launch_clk}' is unconstrained.",
                   object_names=[p.startpoint, out_port, launch_clk], blocking=True)
            continue
        port_delays = [(_clk, cs) for (_port, _clk, _mm, _edge, _add), cs
                       in output_delays.items() if _port == out_port]
        matching = [c for _clk, cs in port_delays for c in cs if _clk == launch_clk]
        wrong_clock = [c for _clk, cs in port_delays for c in cs if _clk != launch_clk]
        if matching:
            rep.output_paths_covered += 1
            continue
        if wrong_clock:
            wrong_names = sorted({c.values.get("clock") for c in wrong_clock
                                  if c.values.get("clock")})
            rep.uncovered.append(_entry(
                category="output_timing_path",
                classification=UncoveredClassification.REQUIRES_USER_DECISION,
                object=f"{p.startpoint}->{out_port}",
                message=(f"Output path {p.startpoint} -> {out_port} has set_output_delay "
                         f"constraint(s) but they reference clock(s) {wrong_names} "
                         f"while the structural launch clock is '{launch_clk}'."),
                startpoint=p.startpoint, endpoint=out_port,
                launch=launch_clk, capture=None,
                reason=(f"set_output_delay on '{out_port}' is associated with "
                        f"clock(s) {wrong_names}, not the path's launch clock "
                        f"'{launch_clk}'."),
                suggestion=(f"Add or correct set_output_delay on '{out_port}' to "
                            f"reference the structural launch clock '{launch_clk}'.")))
            _issue(report, Severity.WARNING, ValidationCategory.COVERAGE,
                   ErrorCode.COVERAGE_OUTPUT_GAP,
                   f"Output timing clock mismatch: {p.startpoint} -> {out_port} "
                   f"launched by '{launch_clk}' but set_output_delay uses {wrong_names}.",
                   object_names=[p.startpoint, out_port, launch_clk] + wrong_names,
                   blocking=False,
                   suggestion=(f"Add a set_output_delay on '{out_port}' that references "
                               f"the launch clock '{launch_clk}'."))
            continue
        rep.uncovered.append(_entry(
            category="output_timing_path",
            classification=UncoveredClassification.UNCONSTRAINED,
            object=f"{p.startpoint}->{out_port}",
            message=f"Output path {p.startpoint} -> {out_port} has no applicable set_output_delay.",
            startpoint=p.startpoint, endpoint=out_port,
            launch=launch_clk, capture=None,
            reason=f"No set_output_delay on '{out_port}' references a defined clock."))
        _issue(report, Severity.WARNING, ValidationCategory.COVERAGE,
               ErrorCode.COVERAGE_OUTPUT_GAP,
               f"Output timing gap: {p.startpoint} -> {out_port} has no applicable set_output_delay.",
               object_names=[p.startpoint, out_port], blocking=False,
               suggestion=f"Add set_output_delay on {out_port} referencing the launch clock '{launch_clk}'.")


# ---------------------------------------------------------------------------
# Constraint indexers
# ---------------------------------------------------------------------------


def _index_clocks(cset: ConstraintSet):
    by_name: dict[str, Constraint] = {}
    for c in cset.clocks():
        n = c.values.get("name")
        if n:
            by_name[n] = c
    g_by_name: dict[str, Constraint] = {}
    for c in cset.generated_clocks():
        n = c.values.get("name")
        if n:
            g_by_name[n] = c
    return by_name, g_by_name


def _index_io_delays(cset: ConstraintSet, ct: ConstraintType) -> dict[tuple, list[Constraint]]:
    out: dict[tuple, list[Constraint]] = {}
    for c in cset.by_type(ct):
        clk = c.values.get("clock") or (c.clock_refs[0] if c.clock_refs else None)
        if not clk:
            continue
        mm = c.values.get("min_max", "both")
        edge = c.values.get("edge", "both")
        add = bool(c.values.get("add_delay", False))
        for tgt in c.target_objects or []:
            key = (tgt, clk, mm, edge, add)
            out.setdefault(key, []).append(c)
    return out


def _index_clock_groups(cset: ConstraintSet) -> tuple[set[tuple[str, str]], dict[tuple[str, str], str]]:
    """Return (set of handled pairs, mapping pair -> relationship kind).

    The set includes any pair of clocks declared in the same
    set_clock_groups but DIFFERENT groups.
    """
    pairs: set[tuple[str, str]] = set()
    rel_map: dict[tuple[str, str], str] = {}
    for c in cset.by_type(ConstraintType.SET_CLOCK_GROUPS):
        groups = c.values.get("groups", [])
        rel = c.values.get("relationship", "asynchronous")
        for gi, g in enumerate(groups):
            for og in groups[gi + 1:]:
                for a in g:
                    for b in og:
                        if a == b:
                            continue
                        p = _pair(a, b)
                        pairs.add(p)
                        # last writer wins; conflicts are handled by semantic layer
                        rel_map[p] = rel
    return pairs, rel_map


def _index_cdc_path_exceptions(cset: ConstraintSet) -> set[tuple[str, str]]:
    """Return set of (a,b) clock pairs explicitly handled by a
    set_false_path or set_max_delay/set_min_delay with both from and to
    populated."""
    out: set[tuple[str, str]] = set()
    for ct in (ConstraintType.SET_FALSE_PATH, ConstraintType.SET_MAX_DELAY,
               ConstraintType.SET_MIN_DELAY, ConstraintType.SET_MULTICYCLE_PATH):
        for c in cset.by_type(ct):
            ps = c.path_selector
            if not ps:
                continue
            # We only count it as explicit CDC handling when BOTH from and to are
            # populated; a bare set_false_path with no selectors is flagged as
            # BROAD elsewhere and should not give blanket credit here.
            if not ps.from_set or not ps.to_set:
                continue
            for a in ps.from_set:
                for b in ps.to_set:
                    if a != b:
                        out.add(_pair(a, b))
    return out


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _pair(a: str, b: str) -> tuple[str, str]:
    return tuple(sorted((a, b)))  # type: ignore[return-value]


def _entry(*, category: str, classification: UncoveredClassification,
           object: str, message: str,
           startpoint: str | None, endpoint: str | None,
           launch: str | None, capture: str | None,
           reason: str, suggestion: str | None = None) -> dict[str, Any]:
    d: dict[str, Any] = {
        "category": category,
        "classification": classification.value,
        "object": object,
        "message": message,
        "startpoint": startpoint,
        "endpoint": endpoint,
        "launch_clock": launch,
        "capture_clock": capture,
        "reason": reason,
    }
    if suggestion:
        d["suggestion"] = suggestion
    return d
