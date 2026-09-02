# Step 11 Report — Multi-Objective QoR Optimizer (Closed)

This is the closing mathematical correction for Step 11. The prior margin
utilization computed only setup degradation against a min(setup,hold)
denominator, which could understate margin consumption when a candidate
degraded hold slack. The metric is now a proper **binding-dimension
utilization**: it accounts for BOTH setup and hold degradation and reports
the most-constrained (highest) consumed fraction.

All other Step 11 policies are preserved: authoritative hard-feasibility
gate, blocked/infeasible/unsafe distinction, real↔proxy area INCOMPARABLE
in both Pareto and priority selection, UNKNOWN metrics never fabricated,
single canonical `stable_hash_cset()`, one-mutation-per-candidate baseline
strategy, fixed-constraint immutability, Step-10 EDA cache interface, and
deterministic tie-breaking.

## 1. Files modified (existing files only)

- `src/rca/qor/objectives.py` — rewrote `compute_margin()` to implement the
  binding-dimension max utilization formula and the zero/unknown-headroom
  policy described below. `margin_utilization` remains a diagnostic, is
  excluded from `OBJECTIVE_SPECS` and from `scalar_score()`, and is used
  only as a residual-slack-preserving LAST tie-break in
  `_priority_compare()` (lower utilization wins).
- `tests/unit/test_pareto.py` — updated the pre-existing
  `test_margin_headroom_above_required` and
  `test_margin_hold_limited_caps_headroom` to reflect the corrected math;
  added `test_margin_missing_baseline_hold_returns_none`; added 13 new
  hold-aware regression tests (14 tests total in this pass → 125 passing
  Pareto/optimizer tests).
- `STEP11_REPORT.md` — this document, updated in place.

No other objective semantics, Pareto logic, identity hashing, search,
feasibility, or cache code was changed.

## 2. Final margin-utilization formula

Computed only for candidates that pass hard feasibility.

Baseline headrooms (ns):

    setup_headroom_b = max(0, baseline_setup_wns - req_s) * 1e9
    hold_headroom_b  = max(0, baseline_hold_wns  - req_h) * 1e9

Consumed (ns), clamped to ≥ 0 (timing improvement counts as 0 consumption):

    setup_consumed = max(0, baseline_setup_wns - cand_setup_wns) * 1e9
    hold_consumed  = max(0, baseline_hold_wns  - cand_hold_wns)  * 1e9

Per-dimension utilization, clamped to [0, 1]:

    setup_util = clamp(setup_consumed / setup_headroom_b, 0, 1)
    hold_util  = clamp(hold_consumed  / hold_headroom_b,  0, 1)

Combined metric (most-constrained fraction consumed):

    margin_utilization = max(setup_util, hold_util)

`margin_headroom_ns` (diagnostic, ns) remains:

    margin_headroom_ns = min(max(0, cand_setup_wns - req_s),
                            max(0, cand_hold_wns  - req_h)) * 1e9

Interpretation: margin_utilization answers "what fraction of the tightest
available baseline timing headroom has been consumed by this candidate?"
A candidate that destroys hold slack is detected even when setup is
generous. This is a diagnostic measure of headroom usage, not a reward.

## 3. Zero / unknown headroom policy

| Situation                                                       | margin_utilization |
|-----------------------------------------------------------------|--------------------|
| Both baseline setup headroom AND baseline hold headroom > 0, all four WNS values known | `max(setup_util, hold_util)` in [0, 1] |
| Baseline setup headroom == 0 (baseline already at setup floor)  | `None` (no positive common margin) |
| Baseline hold headroom  == 0 (baseline already at hold floor)   | `None` (no silent setup-only fallback) |
| Any of baseline_setup / baseline_hold / cand_setup / cand_hold WNS missing | `None` (do not fabricate a value) |
| Candidate improves timing on a dimension (cand WNS > baseline)  | consumption = 0 for that dimension (not negative) |
| Numerical overshoot beyond floor                                | clamped to 1.0 by the per-dimension clamp; hard feasibility already rejects below floor |
| Candidate is INFEASIBLE / BLOCKED / UNSAFE                      | utilization not consulted; feasibility gate wins |

Hard floors (`required_setup_ns` / `required_hold_ns`) are enforced by
`classify_feasibility()` BEFORE utilization is computed; utilization never
overrides feasibility.

## 4. Final-selection tie-break & scalar score

Preserved:
- `_priority_compare()` still prefers LOWER margin_utilization (preserve
  residual slack) as the penultimate tie-break, after user priorities and
  before candidate id.
- `scalar_score()` continues to EXCLUDE margin_utilization entirely.
- margin_utilization is NOT in `OBJECTIVE_SPECS` and is never used for
  Pareto dominance.

## 5. Tests added / modified

`tests/unit/test_pareto.py` — **125 tests** (was 111; +14).

Margin tests (new/updated):
- `test_margin_headroom_above_required` (updated)
- `test_margin_missing_baseline_hold_returns_none` (new)
- `test_margin_hold_limited_caps_headroom` (updated, now asserts exact 0.5)
- `test_margin_setup_limited_setup_consumption_drives`
- `test_margin_hold_limited_hold_consumption_drives`
- `test_margin_both_dimensions_consumed_max_of_two`
- `test_margin_improves_setup_degrades_hold`
- `test_margin_improves_hold_degrades_setup`
- `test_margin_zero_hold_baseline_headroom_none`
- `test_margin_zero_setup_baseline_headroom_none`
- `test_margin_missing_baseline_setup_none`
- `test_margin_missing_candidate_hold_none`
- `test_margin_missing_candidate_setup_none`
- `test_margin_improved_timing_clamps_to_zero`
- `test_margin_feasible_at_boundary_util_is_one`
- `test_margin_utilization_never_exceeds_one`

(Plus existing margin/hard-feasibility/Pareto/priority/identity tests
preserved.)

## 6. Test commands and exact results

Command:
```
python -m pytest tests/unit/test_pareto.py -q
```
Result:
```
collected 125 items
125 passed in 0.53s
```

Command:
```
python -m pytest -q
```
Result:
```
collected 690 items
538 passed, 145 failed, 7 errors in 8.69s
```
- 145 failures: all pre-existing `RuntimeError: pyslang is not installed`
  (Verilog parser unavailable).
- 7 errors: all `pyslang is not installed` import errors.
- No Step-11-introduced failures; pass count rose 524 → 538 because the 14
  new margin tests all pass.
- Dependency-blocked summary: 145 failures + 7 errors are pyslang-blocked.

## 7. Remaining limitations

1. No real Yosys+OpenSTA execution in sandbox (toolchain unavailable); the
   optimizer is wired end-to-end with a pluggable `evaluate_fn` and Step 10
   cache integration; unit tests use stub evaluators.
2. Mutation plans remain clock-uncertainty + IO delay only (per project
   direction).
3. Single-scenario optimization; MCMM deferred.
4. Real area and area_proxy are never numerically converted; they coexist
   on the Pareto front as INCOMPARABLE.
5. Power remains UNKNOWN until a Liberty power model is available; unknown
   power is never treated as zero and never dominates measured power.
6. Margin-tradeoff efficiency (PPA gain per unit margin consumed) is not
   computed as a separate metric; the current tie-break preserves slack
   rather than inventing an efficiency number.
7. Exploratory unsafe mode exists in `classify_feasibility` but is not
   wired to a CLI switch; default safe policy applies.

Step 11 is complete. MCMM is not started.
