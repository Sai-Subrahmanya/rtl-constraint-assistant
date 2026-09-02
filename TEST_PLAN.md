# Test Plan — RCA Validation, Formal Exception Verification, Semantic Comparison, and Power Reports (Steps 13–15, 20)

This test plan describes validation-engine, concrete formal-adapter,
semantic-comparison, and conservative power-report-ingestion coverage. Tests are kept in `tests/unit/test_validation.py` (Step 7),
`tests/unit/test_validation_step13.py` (Step 13, 40 named scenarios),
`tests/unit/test_symbiyosys.py` (Step 14, 11 named scenarios), and
`tests/unit/test_equivalence.py` (Step 9 capability, Step-15 audit-hardened;
67 named semantic-comparison scenarios), and `tests/unit/test_power_reports.py`
(Step 20; one representative fixture plus temporary variants).

## Suites

| Suite | Location | Count | Notes |
|---|---|---|---|
| Step-7 validation | `tests/unit/test_validation.py` | 74 | Deterministic IDs, clocks, gclks, IO, groups, conflicts, coverage, exceptions, scenarios, backend, immutability. |
| Step-13 validation | `tests/unit/test_validation_step13.py` | 40 | Strengthened reference/semantic/conflict/completeness/exception-safety/scenario/SDC-import/backend/provenance/determinism. |
| Step-14 SymbiYosys formal adapter | `tests/unit/test_symbiyosys.py` | 11 | PASS/FAIL/UNKNOWN/error/timeout verdicts, counterexamples, configuration, scenario provenance, validation integration, and immutability. |
| Step-15 semantic-comparison audit | `tests/unit/test_equivalence.py` | 67 | Existing UCM/SDC normalization and deterministic semantic-diff coverage, plus hardened CLI UNKNOWN handling and active-MCMM scope/context comparisons. |
| Step-12 MCMM | `tests/unit/test_mcmm.py` | 70 | MCMM aggregation, per-scenario identity, cache, evaluator. |
| Step-11 Pareto | `tests/unit/test_pareto.py` | 125 | Multi-objective Pareto/scalar/final selection. |
| Step-20 power-report ingestion | `tests/unit/test_power_reports.py` | 34 | One representative OpenROAD/OpenSTA `report_power` fixture; parsing, units, statuses, provenance, artifact/cache, MCMM, Pareto, mock, and CLI/report regressions. |

## Step-13 named scenarios

1. `test_13_01` — valid references produce no false invalid findings.
2. `test_13_02` — nonexistent port flagged when design available (RESOLVED miss).
3. `test_13_03` — design unavailable ⇒ reference is UNRESOLVED, not invalid.
4. `test_13_04` — ref-kind inconsistent (input_delay on a pin).
5. `test_13_05` — ref-kind consistent (input_delay on a port).
6. `test_13_06` — empty selector disallowed for I/O.
7. `test_13_07` — negative design-rule value rejected.
8. `test_13_08` — non-integer fanout rejected.
9. `test_13_09` — clock uncertainty requires a target clock.
10. `test_13_10` — driving cell requires target + cell.
11. `test_13_11` — nonsensical min_delay rejected.
12. `test_13_12` — incompatible divide_by+multiply_by rejected.
13. `test_13_13` — conflicting clock periods flagged.
14. `test_13_14` — conflicting IO delays flagged.
15. `test_13_15` — contradictory exceptions (false_path + multicycle).
16. `test_13_16` — user-vs-inference conflict precedence.
17. `test_13_17` — broad false_path flagged.
18. `test_13_18` — duplicate clock is overlap, not conflict.
19. `test_13_19` — overlapping false paths reported.
20. `test_13_20` — coverage unknown without graph.
21. `test_13_21` — completeness: missing clock period.
22. `test_13_22` — completeness: missing IO timing.
23. `test_13_23` — completeness: unresolved clock relationship.
24. `test_13_24` — completeness: generated clock without transform.
25. `test_13_25` — fixed constraint never modified.
26. `test_13_26` — provenance preserved in issue dict.
27. `test_13_27` — nonexistent scenario id flagged.
28. `test_13_28` — scenario-specific issues preserve identity.
29. `test_13_29` — empty scenario_ids ⇒ all active.
30. `test_13_30` — MCMM active scenarios respected.
31. `test_13_31` — SDC import complete (no incomplete finding).
32. `test_13_32` — SDC import syntax-invalid classified + SYNTAX_ERROR.
33. `test_13_33` — SDC import empty ⇒ incomplete.
34. `test_13_34` — unknown backend flagged without crashing.
35. `test_13_35` — exception unverified when no formal backend.
36. `test_13_36` — deterministic repeated validation.
37. `test_13_37` — severity/blocking ordering.
38. `test_13_38` — report exposes new completeness summary.
39. `test_13_39` — UNKNOWN/UNRESOLVED in `to_dict`.
40. `test_13_40` — issue_id stable with provenance fields.

## Step-14 named scenarios

1. `test_14_01` — only SBY PASS plus zero exit is formally VERIFIED.
2. `test_14_02` — SBY FAIL is INVALID and preserves counterexample artifacts.
3. `test_14_03` — SBY UNKNOWN or missing marker stays UNRESOLVED.
4. `test_14_04` — missing mapping, proof file, or executable stays UNRESOLVED.
5. `test_14_05` — timeout and PASS/non-zero discrepancy are never VERIFIED.
6. `test_14_06` — false-path/multicycle proof-kind mismatch is explicit ERROR.
7. `test_14_07` — multicycle proof provenance preserves the cycle count.
8. `test_14_08` — stable run ID, scenario provenance, and no UCM mutation.
9. `test_14_09` — formal counterexample becomes a blocking existing-validation finding.
10. `test_14_10` — YAML formal config resolves proof paths and constructs the adapter.
11. `test_14_11` — duplicate UCM proof mappings are deterministically rejected.

The test fixture is a local fake `sby` executable. It exercises the adapter's
actual argument-list subprocess, status-marker, timeout, artifact, and
provenance handling without substituting a fake result for a real formal proof.

## Step-20 power-report scenarios

The one tracked fixture is `tests/golden/reports/openroad_report_power_representative.rpt`.
It is labelled representative test data, not a live power-tool result. All bad
variants are generated in test text or pytest temporary directories.

1. Recognized `Total`, Internal + Switching dynamic, and Leakage map to the existing QoR fields.
2. Explicit `W` normalization.
3. Explicit `Watts` normalization.
4. Explicit `mW` normalization.
5. Explicit `uW` normalization.
6. Explicit `µW` normalization.
7. Explicit `nW` normalization.
8. Explicit `pW` normalization.
9. Valid literal zero remains `AVAILABLE`.
10. Missing report is `UNAVAILABLE`, never zero.
11. Missing Total is `UNKNOWN`.
12. Missing Internal/Switching component leaves dynamic unknown but keeps total.
13. Missing Leakage leaves leakage unknown but keeps total.
14. Malformed numeric cell is `MALFORMED`.
15. Multiple candidate report tables are `UNKNOWN`.
16. Unrelated text is `UNSUPPORTED`.
17. Unsupported explicit unit is `UNSUPPORTED`.
18. Absent unit is `UNKNOWN`; no implicit watts default exists.
19. Negative values are `INVALID`.
20. Inconsistent complete component sum is `INVALID`.
21. Repeated parse is deterministic.
22. Summary/rehydration retain power components and provenance.
23. Single-scenario YAML allows omitted scenario ID and resolves its path.
24. MCMM rejects global, unknown, inactive, and duplicate report mappings.
25. Real-flow plumbing accepts valid report-derived evidence.
26. Existing run manifest records a run-relative report artifact and SHA-256.
27. Report mutation changes the existing cache key and prevents a cache hit.
28. A missing configured report remains unavailable in a real flow.
29. Mock flow ignores a configured report and stays mock/unavailable.
30. MCMM selects only each scenario's own report and retains per-scenario provenance.
31. Missing required active-scenario power leaves global MCMM power unknown; no average is made.
32. Available report-derived lower power participates in existing Pareto comparison.
33. Effective scenario association changes the existing cache identity.
34. Existing CLI and human report present tool-reported wording and report provenance.

## Gate criteria

- `tests/unit/test_validation.py` : **74 passed** (no weakened assertions).
- `tests/unit/test_validation_step13.py` : **40 passed**.
- `tests/unit/test_symbiyosys.py` : **11 passed** (Step-14 gate; no real formal tool required).
- `tests/unit/test_pareto.py` : **125 passed** (Step-11 regression).
- `tests/unit/test_mcmm.py` : **70 passed** (Step-12 regression).
- `tests/unit/test_equivalence.py` : **67 passed** (Step-15 audit/hardening gate).
- `tests/unit/test_power_reports.py` : **34 passed** (Step-20 parser/flow/cache/MCMM/CLI gate; no live power tool).
- Full `python -m pytest -q` : **850 collected, 850 passed, 0 failed, 0 skipped, 0 errors** (in the project virtual environment).

## Environment notes

The `pyslang` Verilog/SystemVerilog front-end is optional; tests that
exercise the real RTL parser require it and are skipped/reported honestly
in environments without it. This suite does not depend on it.
