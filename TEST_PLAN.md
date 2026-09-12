# Test Plan — RCA Validation, Formal, Semantic Comparison, Power, QoR History, Real-EDA, and Knowledge Boundaries (Steps 11–15, 20–26)

This test plan describes validation-engine, concrete formal-adapter,
semantic-comparison, conservative power-report-ingestion, real-EDA
execution-boundary, and offline knowledge/reuse coverage. Tests are kept in `tests/unit/test_validation.py` (Step 7),
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
| Step-25 real-EDA hardening | `tests/unit/test_eda_preflight.py`, `tests/unit/test_eda_subprocess_hardening.py`, `tests/integration/test_real_eda_boundary.py`, plus established EDA boundary cases | 30 new focused cases | Typed preflight/doctor JSON, safe argv and process-group timeout cleanup, explicit mock separation, controlled fake Yosys/OpenSTA success/failure/timeout/missing/malformed/stale behavior, provenance/manifest/fingerprint/no-secret contract, power evidence, and cache reuse/invalidation. |
| Step-26 knowledge/reuse | `tests/unit/test_knowledge.py`, `tests/golden/test_knowledge_golden.py`, `tests/golden/knowledge/builtin_patterns.json`, `tests/integration/test_knowledge_cli.py` | 11 focused cases | Typed data/trust/applicability, Step-9 normalized equivalence and stable ranking, explicit UCM acceptance/duplicate/conflict preservation, history snapshot projection without trust promotion, strict JSON/no-execution security, built-in golden data, and advisory CLI/artifact boundaries. |

## Step-26 knowledge/reuse scenarios

1. Built-in pattern data is stable, typed, vendor-neutral, and never directly
   acceptable as UCM.
2. Existing Step-9 normalisation identifies unit-equivalent UCM constraints;
   same scope with different values remains a reported disagreement.
3. Ranking and suggestion IDs/order are repeatable and are not probability or
   correctness claims.
4. Explicit acceptance uses `ConstraintSet.add`, preserves provenance/evidence,
   leaves fixed intent untouched, rejects duplicates, and reports conflicts for
   the established validation pipeline.
5. User JSON data is strict, bounded, non-symlink data only; unknown fields,
   duplicate keys, YAML/Tcl/Python payloads, and non-executed strings are
   rejected safely.
6. SQLite history is read only, requires a retained canonical snapshot, keeps
   validation/evidence observations, and never promotes historical existence to
   `VERIFIED`.
7. Built-in data is a checked-in golden fixture and the CLI reports advisory
   `list`, `search`, `show`, and `suggest` output without accepting a result.

See `STEP26_KNOWLEDGE_REUSE.md` for the data/API/CLI contract and exact
non-authority boundary.

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

## Step-25 real-EDA boundary scenarios

Step 25 default coverage uses only controlled executable fixtures and temporary
Liberty/RTL/report collateral. It never needs a local commercial tool, a PDK,
or a real Yosys/OpenSTA binary.

1. Typed preflight distinguishes versioned versus merely found/missing Yosys,
   OpenSTA, and SymbiYosys executables; missing RTL/Liberty/include/SDC
   collateral; unsupported backend; output location; and selected formal
   collateral.
2. Preflight fingerprint is repeatable and excludes injected environment-secret
   values. `RunManifest` extension round-trips without disrupting prior fields.
3. `rca doctor --json` is valid machine-readable output for valid and invalid
   configuration and explicitly states mock is opt-in.
4. Controlled fake real flow records non-mock QoR, fresh report/output hashes,
   command argv/cwd/timeout tails, tool/version provenance, preflight, and one
   authoritative `run_manifest.json` (no divergent second manifest).
5. Nonzero Yosys/OpenSTA exits, timeouts, absent netlist/report files, and
   malformed timing reports have typed failure classification and no successful
   QoR/power/cache outcome.
6. POSIX timeout testing verifies process-group descendant termination;
   argv-injection text is retained as one argument and no command environment is
   serialized.
7. Reused run directories remove stale synthesis/timing/stat/QoR/power outputs
   before a failed real attempt; only current artifacts can be manifest evidence.
8. Configured valid power report evidence is parsed and hashed only after valid
   STA; malformed report evidence remains canonical power-unavailable.
9. Identical successful fake-real inputs reuse a cache entry; source or tool
   version changes re-execute; a failed manifest cannot be a cache hit.

The opt-in actual-real suite remains separate:

```bash
RCA_RUN_REAL_EDA=1 RCA_REAL_EDA_LIBERTY=/path/to/cells.lib \
  pytest tests/integration/test_optional_real_eda.py -m optional_real_eda -q
```

It skips only when opt-in/tool/Liberty prerequisites are unavailable. Once they
are available, unexpected real-flow failure is a test failure. The controlled
fakes above are execution-boundary tests, not actual Yosys/OpenSTA measurements.

## Gate criteria

The named subsystem suites remain mandatory regression gates: validation,
formal-adapter behavior (without a real formal tool), optimizer/Pareto, MCMM,
semantic comparison, and power-report parsing/flow/cache behavior. Exact test
counts are intentionally not recorded here because the active Step 24 taxonomy
adds focused coverage over time. Run the maintained gates instead:

```bash
python scripts/regression/run_regression.py
pytest tests/unit -q
pytest tests/golden -q
pytest tests/integration -q
pytest tests/stress -q
```

A passing default suite may contain explicitly skipped optional real-EDA cases;
those are not a replacement for a provisioned real-tool run. See
`STEP24_VALIDATION.md` for the current optional-real-EDA policy and failure
matrix.


## Step-32 constraint-release baseline and package scenarios

Step 32 adds `tests/unit/test_constraint_release.py` (41 focused cases),
`tests/integration/test_cli_constraint_release.py` (CLI assessment, explicit
release/package, and invalid verification paths), and
`tests/golden/test_constraint_release_golden.py` with 32 deterministic named
release/package projections. These cases verify:

1. frozen typed release/policy/snapshot/package records and stable JSON/IDs;
2. explicit-only candidate, release, revoke, and supersede lifecycle behavior;
3. approved, warning-approved, absent, pending, stale, revoked, and mismatched
   Step-31 review handling without creating or changing review;
4. fail-closed readiness, validation, coverage, lineage, formal-class, SDC,
   supplied-artifact, unsupported semantic, and unresolved-evidence handling;
5. exact global, selected, all-active, unknown, and conflicting MCMM scope;
6. no automatic UCM application, SDC generation, EDA/formal execution, or
   external-signoff claim;
7. reproducible descriptor/UCM/evidence/artifact package contents, SHA-256
   integrity, safe relative-path checks, corruption detection, and stateless
   verification; and
8. deterministic `rca release` / `rca release-verify` JSON, explicit action
   rejection, and package E2E behavior.

The release package test is not an external-tool test. A `VERIFIED` package
means only that the retained RCA package is internally consistent and
hash-valid; it does not imply STA, physical, commercial, or ASIC signoff.

## Environment notes

The `pyslang` Verilog/SystemVerilog front-end is optional; tests that
exercise the real RTL parser require it and are skipped/reported honestly
in environments without it. This suite does not depend on it.

---

# Step 24 — Validation Taxonomy, Regression, and Stress Plan

The active test layout is intentionally divided by purpose rather than by a
raw-count target. See `STEP24_VALIDATION.md` for the maintained execution
commands and full failure matrix.

| Category | Command | Success criteria |
|---|---|---|
| Unit | `pytest tests/unit -q` | Existing subsystem contracts pass. |
| Golden | `pytest tests/golden -q` | Semantic references match; no volatile fields are asserted. |
| Integration | `pytest tests/integration -q` | Full deterministic CLI/parser/flow/artifact/history paths pass. |
| Stress | `pytest tests/stress -q` | Fixed workloads preserve worker bounds and deterministic semantics. |
| Regression | `python scripts/regression/run_regression.py` | Every gate reports and exits zero. |
| Optional real EDA | `RCA_RUN_REAL_EDA=1 RCA_REAL_EDA_LIBERTY=/path/lib pytest -m optional_real_eda -q` | Provisioned real tools pass; absence is explicitly skipped. |

## Reference coverage map

- Existing `golden/sdc` references cover single/multiple/generated clocks,
  input/output timing, clock relationships/groups, false paths, multicycle
  paths, delays and DRC commands.
- Existing timing corpus covers structural clock/reset/CDC behavior.
- Step 24 reference checks cover semantic equivalent/different/UNKNOWN SDC,
  validation/conflict/coverage classification, OpenROAD-format report power,
  MCMM aggregation, optimizer mutation, and execution-ledger ordering.

## Determinism rule

Only invocation-specific ledger identifiers, task paths, logical worker labels,
and artifact locators may be normalized. Constraints, lineage, QoR values,
scenario identity, cache observation, status, Pareto/final selection, and hashes
remain meaningful and must not be normalized away.

## Failure and integrity gates

The regression gates collectively cover malformed/missing RTL and config,
unsupported/unresolved intent, missing optional tools/Liberty, flow failures,
formal unavailable/unresolved, SQLite advisory failures, cache hash corruption,
incomplete MCMM data, budget/deadline behavior, invalid workers, executor and
worker failures. Run manifests/artifacts remain the evidence authority and
SQLite remains an advisory query/index sidecar.

## Steps 33–46 lifecycle, safety and UX coverage

| Area | Tests | Core assertion |
|---|---|---|
| Controlled handoff | `tests/unit/test_constraint_handoff.py`, `tests/integration/test_cli_constraint_handoff.py` | Package/release integrity and target support are checked; `--execute` remains an explicit unavailable boundary. |
| Workflow/configuration | `tests/unit/test_workflow_configuration.py`, `tests/integration/test_complete_governed_workflow.py`, `tests/integration/test_cli_complete_governed_workflow.py` | Composition is deterministic and read-only; defaults stop at required review/EDA unavailable; workflow config is strict and portable. |
| Advanced inference/knowledge | `tests/unit/test_inference_candidates.py`, `tests/unit/test_knowledge.py` | Hypotheses stay unconfirmed/rejected; verified package reuse is advisory `VALIDATED`, never proof. |
| Formal/replay evidence | `tests/unit/test_symbiyosys.py`, `tests/unit/test_replay_evidence.py` | Formal result binds to current canonical semantics; stale proof/artifact evidence fails closed; replay never executes automatically. |
| Offline E2E | `tests/integration/test_governed_workflow_demo.py` | The copied canonical demo runs without real tools, labels mock/unavailable/signoff boundaries, and writes only its local output. |

The dashboard projection integration test also checks opt-in report-root
containment and escaped browser rendering. Optional real EDA tests remain
opt-in and must skip/unavailable when installed executable/collateral evidence
is absent.
