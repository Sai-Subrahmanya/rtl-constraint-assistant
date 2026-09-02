# Test Plan — RCA Validation, Formal, Semantic Comparison, Power, and QoR History (Steps 11–15, 20–21)

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
| Step-20 power-report ingestion | `tests/unit/test_power_reports.py` | 38 | One representative OpenROAD/OpenSTA `report_power` fixture; parsing, units, parser classification/canonical QoR compatibility, provenance, artifact/cache, MCMM, Pareto, mock, and CLI/report regressions. |
| Step-21 QoR history repository | `tests/unit/test_qor_repository.py` | 37 | SQLite initialization/versioning, transactional historical graph persistence, canonical QoR/power/provenance/artifact/MCMM indexing, deterministic parameterized queries, explicit legacy import, flow failure safety, cache separation, CLI history, and WAL reader behavior. |

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
variants are generated in test text or pytest temporary directories. Detailed
outcomes are parser `PowerParseStatus` classifications; canonical QoR keeps
only backward-compatible `PowerStatus` availability (`AVAILABLE`,
`UNAVAILABLE`, `ESTIMATED`) and maps all parser failures to `UNAVAILABLE`.

1. Recognized `Total`, Internal + Switching dynamic, and Leakage map to the existing QoR fields.
2. Explicit `W` normalization.
3. Explicit `Watts` normalization.
4. Explicit `mW` normalization.
5. Explicit `uW` normalization.
6. Explicit `µW` normalization.
7. Explicit `nW` normalization.
8. Explicit `pW` normalization.
9. Valid literal zero is parser `AVAILABLE`.
10. Missing report is parser `UNAVAILABLE`, never zero.
11. `PowerParseStatus` is separate from the unchanged three-value canonical `PowerStatus`.
12. Missing Total is parser `UNKNOWN`.
13. Missing Internal/Switching component leaves dynamic unknown but keeps total.
14. Missing Leakage leaves leakage unknown but keeps total.
15. Malformed numeric cell is parser `MALFORMED`.
16. A `38.6%` Internal cell cannot be parsed as power.
17. A `100.0%` Total cell cannot be parsed as power.
18. Multiple candidate report tables are parser `UNKNOWN`.
19. Unrelated text is parser `UNSUPPORTED`.
20. Unsupported explicit unit is parser `UNSUPPORTED`.
21. Absent unit is parser `UNKNOWN`; no implicit watts default exists.
22. Negative values are parser `INVALID`.
23. Inconsistent complete component sum is parser `INVALID`.
24. Repeated parse is deterministic.
25. Summary/rehydration retain power components and provenance.
26. Single-scenario YAML allows omitted scenario ID and resolves its path.
27. MCMM rejects a global report mapping.
28. MCMM rejects an unknown report scenario ID.
29. MCMM rejects an inactive report scenario ID.
30. MCMM rejects duplicate scenario report mappings.
31. Real-flow plumbing accepts valid report-derived evidence, tracks the manifest artifact/hash, and preserves a same-input cache hit while report mutation rekeys it.
32. A missing configured report remains canonical unavailable in a real flow.
33. A malformed report preserves parser `MALFORMED` provenance while canonical QoR remains unavailable with no numeric components.
34. Mock flow ignores a configured report and stays mock/unavailable.
35. MCMM selects only each scenario's own report, retains per-scenario provenance, and keeps global power unknown when an active scenario is missing power.
36. Available report-derived lower power participates in existing Pareto comparison.
37. Rebinding a report to another MCMM scenario, and missing-versus-present evidence, change the existing cache identity.
38. CLI and human report present tool-reported wording/provenance and show a detailed parser classification beside canonical unavailable power.

## Step-21 QoR history repository scenarios

`tests/unit/test_qor_repository.py` uses temporary SQLite sidecars and no live
EDA tool. Its 37 named cases cover:

1. database creation, current schema version, and application identifier;
2. transactional schema initialization/migration and migration rollback;
3. unsupported-newer and ledger/user-version mismatch rejection;
4. flow-record insertion, idempotency, and same-run changed-evidence conflict;
5. full canonical QoR column projection, including detailed area/cell/timing data;
6. nullable unknown metric storage and valid numeric zero;
7. canonical `PowerStatus`, separate `PowerParseStatus`, provenance, report SHA-256, and components;
8. artifact references/hashes and replay-time integrity validation;
9. replay identity inputs, commands, and explicit non-executable replay limitation;
10. session-scoped candidate identity, mutations, constraint-set identity, and lineage;
11. physical evaluation linkage to a later optimizer candidate;
12. distinct MCMM scenario evidence and derived aggregate/objective persistence;
13. conservative MCMM unknown/incomparable aggregate fields;
14. candidate/scenario/status/constraint-set query filtering;
15. real/proxy area separation and power availability rules in best-QoR queries;
16. deterministic list, artifact, best, candidate, MCMM, and CLI JSON ordering;
17. transaction rollback for evaluation and MCMM graphs;
18. explicit legacy import, missing-field NULL handling, idempotency, current-record recognition, and conflicts;
19. legacy power parser provenance retention;
20. WAL two-connection reader behavior;
21. real-flow blocked-manifest indexing after existing artifact output;
22. explicit database-failure warning while QoR/artifacts stay intact;
23. no sidecar for ordinary mock flow unless a repository is explicitly supplied;
24. passive cache-key indexing and proof that repository queries do not invoke filesystem cache lookup;
25. CLI `history` query, JSON, candidate/session, selector validation, and explicit legacy-import behavior.

## Gate criteria

- `tests/unit/test_validation.py` : **74 passed** (no weakened assertions).
- `tests/unit/test_validation_step13.py` : **40 passed**.
- `tests/unit/test_symbiyosys.py` : **11 passed** (Step-14 gate; no real formal tool required).
- `tests/unit/test_pareto.py` : **125 passed** (Step-11 regression).
- `tests/unit/test_mcmm.py` : **70 passed** (Step-12 regression).
- `tests/unit/test_equivalence.py` : **67 passed** (Step-15 audit/hardening gate).
- `tests/unit/test_power_reports.py` : **38 passed** (Step-20 parser/flow/cache/MCMM/CLI gate; no live power tool).
- Full `python -m pytest -q` : **854 collected, 854 passed, 0 failed, 0 skipped, 0 errors** (in the project virtual environment).

## Environment notes

The `pyslang` Verilog/SystemVerilog front-end is optional; tests that
exercise the real RTL parser require it and are skipped/reported honestly
in environments without it. This suite does not depend on it.
