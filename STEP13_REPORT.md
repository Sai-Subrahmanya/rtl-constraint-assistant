# Step 13 — Constraint Validation Engine (strengthened)

**Status:** Implemented on the canonical Step-12 baseline (PR #3 open/unmerged).
**Scope:** Strengthen the existing `src/rca/validation/` engine to a robust,
deterministic, provenance-aware validator covering reference integrity,
constraint-type semantics, conflicts, overlap/shadowing, coverage,
completeness, exception safety, scenario awareness, SDC-import
classification, and backend hooks — while extending (not duplicating) the
Step-7 validation data model and preserving the Step-11/Step-12 gates.

> Honest limitation: exception *safety* is structural + verification-state
> based. With no formal backend attached, `verify_exceptions` uses the
> `ConservativeFormalBackend` (always UNRESOLVED), so exceptions are surfaced
> as `EXCEPTION_UNVERIFIED` — never concluded safe merely because they
> improve timing. No real per-scenario formal proof is claimed. The
> Step-7 `coverage.py` still matches I/O ports by exact target name, so
> hierarchical vs local naming in a structural path may yield a
> `COVERAGE_OUTPUT_GAP`/`COVERAGE_INPUT_GAP` warning even when a delay
> exists (pre-existing; out of Step-13 scope).

---

## 1. Design

There is **one** validation data model. `ValidationIssue` /
`ValidationReport` / `ValidationResult` are extended; no parallel classes
were introduced.

- `ValidationIssue` gained optional provenance fields: `source_kind`,
  `origin`, `assumption_ids`, `resolution_status`
  (`RESOLVED / UNKNOWN / UNRESOLVED / REQUIRES_USER_INPUT`).
- `ValidationReport` gained `completeness_summary`.
- `ValidationResult.as_dict()` / `ValidationReport.summary()` expose the
  new summary.
- `issue_id` remains deterministic; it now hashes the added fields too.
- The engine is observational: it never mutates the `ConstraintSet`,
  `Design`, or `TimingGraph`; values are reported, never silently repaired.

Ordered pipeline (engine `run_validation`):

```
references → semantic → conflicts → exceptions+scenarios → completeness
→ backend → coverage → sdc_import(optional) → hydrate_provenance
```

New integration with existing systems (no duplication):

- Reuses `rca.exceptions.verify_exceptions` (Step 8 harness) for exception
  safety state.
- Reuses `rca.sdc.parser.SDCParser` diagnostics for SDC-import
  classification — the parser is never re-implemented.
- Reuses the backend `preflight_constraint` for vendor capability checks.

---

## 2. What was added / strengthened

| Requirement | Where | What |
|---|---|---|
| Result model (1) | `base.py`, `engine.py` | Provenance + resolution fields; deterministic ids kept. |
| Reference/object (2) | `references.py` | Hierarchical+local name resolution; `REF_UNKNOWN` is `RESOLVED` (known miss) when design available, `UNRESOLVED` otherwise; `REF_KIND_INCONSISTENT`; fixed latent `instance_name`→`local_name` bug. |
| Semantic (3) | `semantic.py` | Value/unit/range checks for clock uncertainty/latency/transition, input transition, load, driving cell, design rules, min/max delay; no silent repair. |
| Conflicts (4) | `conflicts.py` | Precedence ranking (fixed/user > config > imported > tool/library > RTL > inference/derived); `CONFLICT_USER_VS_INFERENCE`; `CONFLICT_EXCEPTION` (false_path vs multicycle/max_delay; min>max). |
| Overlap/shadowing (5) | `conflicts.py` (existing) | Broad exceptions, subsumption, duplicates preserved. |
| Coverage (6) | `coverage.py` (existing) | UNKNOWN/NOT_APPLICABLE semantics; numerator/denominator retained. |
| Completeness (7) | `completeness.py` *(new)* | Unresolved clock relationships, generated-clock transforms, missing IO timing, unresolved timing env; classified `REQUIRES_USER_INPUT`/`UNRESOLVED`. |
| Exception safety (8) | `exceptions.py` | `EXCEPTION_UNVERIFIED` + `resolution_status=UNRESOLVED` when no formal proof. |
| Scenario/MCMM (9) | `exceptions.py`, `engine.py` | Empty `scenario_ids` ⇒ all active; non-empty ⇒ listed active; `SCENARIO_UNKNOWN_ID`; scenario identity preserved via `scenario_id` inheritance. |
| SDC import (10) | `sdc_import.py` *(new)* | Classifies `SYNTAX_INVALID / SEMANTIC_INVALID / INCOMPLETE / COMPLETE / UNRESOLVED` from importer diagnostics. |
| Backend hooks (11) | `backend.py` (existing) | Vendor preflight kept behind backend abstraction. |
| Provenance (12) | `base.py`, `engine.py` | `source_kind`/`origin` hydrated from the UCM; scenario inherited. |
| CLI (13) | `cli/main.py` | `rca validate` prints the MCMM scenario matrix, restricts scenario validation to active ids, and accepts an `SDCParser` for import classification. |
| Reporting (14) | `base.py`, `engine.py` | `completeness_summary`, richer `to_dict`. |

---

## 3. New files

```
src/rca/validation/completeness.py    missing-info / completeness layer
src/rca/validation/sdc_import.py      SDC import classification
tests/unit/test_validation_step13.py 40 named Step-13 scenarios
docs/decisions/ADR-002-validation-engine.md
VALIDATION_RULES.md                   validation rules reference
TEST_PLAN.md                          Step-13 test plan
```

## 4. Tests

| Suite | Count | Result |
|---|---|---|
| `tests/unit/test_validation.py` (Step 7) | 74 | passed (no assertion weakened) |
| `tests/unit/test_validation_step13.py` (Step 13) | 40 | passed |
| `tests/unit/test_pareto.py` (Step 11 gate) | 125 | passed |
| `tests/unit/test_mcmm.py` (Step 12 gate) | 70 | passed |
| **Full `python -m pytest -q`** | **800 collected / 800 passed** | passed, 0 failed / 0 skipped / 0 errors |

Step-13 named scenarios: valid refs, nonexistent port, design-unavailable
(UNRESOLVED), ref-kind consistent/inconsistent, empty selector, negative
design rule, non-integer fanout, clock-uncertainty target, driving cell,
nonsensical min_delay, incompatible divide/multiply, conflicting clocks,
conflicting IO delays, contradictory exceptions, user-vs-inference
precedence, broad false_path, duplicate clock (overlap), overlapping false
paths, coverage UNKNOWN, missing clock period, missing IO timing, unresolved
clock relationship, generated clock no transform, fixed constraint never
modified, provenance preserved, nonexistent scenario id, scenario identity,
empty scenario_ids ⇒ all active, MCMM active respected, SDC import complete /
syntax-invalid / empty-incomplete, unknown backend, exception unverified,
deterministic repeat, severity/blocking, report summaries, UNKNOWN/UNRESOLVED
in `to_dict`, stable issue_id.

## 5. Step-11 / Step-12 regression

- `test_pareto.py`: **125 passed** (unchanged).
- `test_mcmm.py`: **70 passed** (unchanged).
- No Step-12 file was modified by Step 13 except adding MCMM-aware
  scenario restriction to the CLI `validate` command — a minimal,
  backward-compatible integration change. PR #3 remains OPEN/UNMERGED.

## 6. Known limitations

- Coverage module matches I/O ports by exact target name; hierarchical vs
  local naming in a path may emit a coverage gap warning even when a delay
  exists (pre-existing Step-7 behaviour).
- Exception "safety" is unverified unless a formal backend returns a proof;
  the default backend returns UNRESOLVED.
- No real per-scenario formal verification or real EDA signoff is claimed.
- `pyslang` is available in this environment, so the real RTL front-end
  tests run; if it were absent those tests would be honestly reported as
  skipped (none are skipped here).

## 7. Final suite result

```
800 collected, 800 passed, 0 failed, 0 skipped, 0 errors
```
