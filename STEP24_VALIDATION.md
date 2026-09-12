# Step 24 — Integration, Golden, Regression & Stress Validation

**Status:** implemented

## Validation taxonomy

| Category | Location / command | Purpose | External-tool policy |
|---|---|---|---|
| Unit | `tests/unit`, `pytest tests/unit -q` | Isolated model, parser, validation, SDC, flow-boundary, MCMM, QoR/history, formal-adapter, optimizer, concurrency, and ledger rules. | Fake executables or mock inputs only. |
| Golden/reference | `tests/golden`, `pytest tests/golden -q` | Stable semantic reference outputs: timing corpus, canonical SDC constructs, semantic comparison, validation/conflict/coverage, report power, MCMM aggregation, and execution ledger. | No live EDA. |
| Integration | `tests/integration`, `pytest tests/integration -q` | Complete parse → infer → UCM → validate → SDC → flow → QoR → optimize → artifacts/ledger → history → CLI workflows. | Default tests use explicit mock or deterministic fake command fixtures. |
| Regression | `scripts/regression/run_regression.py` | Ordered pytest gates across the important existing subsystems. It preserves pytest output and fails non-zero on any failing suite. | Same default policy as its selected suites. |
| Stress/concurrency | `tests/stress`, `pytest tests/stress -q` | Short fixed-workload repeated runs, worker bounds, cache observations, failures, deadlines/budgets, executor errors, MCMM, and repeatability. | Controlled in-process evaluators only. |
| Optional real EDA | `tests/integration/test_optional_real_eda.py`, `pytest -m optional_real_eda -q` | Opt-in Yosys/OpenSTA flow and SymbiYosys version-probe checks. | Requires `RCA_RUN_REAL_EDA=1`; missing tools/collateral are **SKIPPED**, never faked. |

The default `pytest -q` executes every deterministic category and reports
optional real-EDA tests as skipped unless explicitly enabled. No category
creates a second UCM, QoR, cache, artifact store, history database, optimizer,
or scheduler.

## Golden philosophy and determinism

Golden checks compare semantic structures, not volatile process facts. Existing
SDC references cover single and generated clocks, reset-adjacent clocking, I/O
delays, clock groups, false paths, and multicycle paths. The reference suite
adds semantic equivalence/difference/UNKNOWN, validation conflict and coverage,
report-derived power, MCMM result, candidate mutation, and execution-ledger
coverage.

Allowed normalization is narrowly limited to fresh execution-locator fields:
ledger invocation IDs, task IDs, task working paths, logical worker labels, and
artifact locators. Timestamps, temporary paths, and runtime IDs are never used
as semantic equality facts. Constraint type/targets/values, scenario identity,
provenance, confidence, validation class, QoR semantics, candidate lineage,
Pareto membership, cache observation, task ordinal/status, and artifact hashes
remain asserted.

The stress workload has no random generator; evaluator delays derive only from
the deterministic candidate ID. `MockEDA` remains explicitly marked mock.

## Default regression runner

```bash
python scripts/regression/run_regression.py
# or a narrow gate:
python scripts/regression/run_regression.py --suite golden
# only when deliberately provisioned for real EDA:
RCA_RUN_REAL_EDA=1 RCA_REAL_EDA_LIBERTY=/path/cells.lib \
  python scripts/regression/run_regression.py --real-eda
```

The runner invokes ordinary pytest, in this order: the complete unit/core suite,
then golden, integration, and stress. Its final table shows
collected/passed/failed/skipped/errors/elapsed values per suite. It never
retries or masks a failure.

## Environment and EDA diagnostics

```bash
python scripts/setup/verify_environment.py
python scripts/setup/verify_environment.py --json
python scripts/eda/diagnose_eda.py --config project.yaml
python scripts/eda/diagnose_eda.py --json
```

Setup verification checks the declared Python/runtime packages and Python
version, without installing anything. It lists Yosys, OpenSTA, and SymbiYosys as
**OPTIONAL** and distinguishes AVAILABLE from MISSING; mock-only RCA is still
supported. The EDA diagnostic performs version probes only, reports configured
Liberty-file presence and backend, and labels Yosys/OpenSTA prerequisites as
expected runnable only when both tools and at least one configured readable
Liberty are present. An executable/version probe is not a claim of a functional
flow or signoff.

## Optional real-EDA boundary

The optional real-flow test must have all of the following before it runs:

```bash
export RCA_RUN_REAL_EDA=1
export RCA_REAL_EDA_LIBERTY=/absolute/path/to/cells.lib
# RCA_YOSYS, RCA_OPENSTA, and RCA_SYMBIYOSYS may name non-default binaries.
pytest tests/integration/test_optional_real_eda.py -m optional_real_eda -q
```

No Liberty, unavailable binary, or disabled opt-in yields an explicit pytest
**SKIPPED** result. Once prerequisites are present, a real flow failure fails
the test. The deterministic fake toolchain used elsewhere is clearly a test
fixture and is not reported as a real EDA result.

## Failure matrix

| Failure / boundary | Expected classification and audit evidence | Primary coverage |
|---|---|---|
| Malformed or missing RTL | Parser/CLI error; no fabricated design/SDC | `test_pipeline_boundaries.py` and parser unit tests |
| Malformed config / invalid workers | Config validation error before execution | integration boundary, config/concurrency unit tests |
| Unresolved constraint / unsupported SDC | validation blocking or semantic `UNKNOWN`, never equivalent | validation/equivalence golden and unit suites |
| Missing Yosys/OpenSTA/Liberty | real flow `BLOCKED`; diagnostic says MISSING; optional test skipped | EDA unit tests, diagnostic script, optional test |
| EDA execution/timeout | structured failed/blocked flow result and retained artifacts | EDA unit suite |
| Worker / executor creation / submission failure | typed failed, blocked, or cancelled ledger entry; no serial fallback | ledger and stress suites |
| SQLite failure | advisory warning only; artifacts, QoR/cache, and ledger remain authoritative | QoR repository and ledger unit tests |
| Cache corruption / meaningful input change | integrity miss / new cache identity, never a valid hit | EDA unit and fake-toolchain integration test |
| Incomplete MCMM evidence | conservative blocked/unknown aggregate with per-scenario identity | MCMM unit and golden suite |
| Formal unavailable/unresolved | unresolved, never VERIFIED | formal unit and integration boundary |
| EDA budget / elapsed deadline | skipped/unsubmitted or recorded in-flight result with typed stop | ledger and stress suites |

## Artifact-integrity policy

`RunManifest` remains the evidence authority. Tests verify artifact hashes,
run-relative references, cache integrity misses for corrupted outputs,
ledger-manifest hashes, valid atomic ledger JSON, and explicit legacy-history
reconciliation. SQLite is only queried/indexed after authoritative files exist;
ledger inspection reads the artifact directly and does not need SQLite.

## Limits

This framework does not claim production signoff, guaranteed real-tool
reproducibility, or performance speedup. It does not install or emulate
commercial tools. The optional real tests prove only their executed, provisioned
case; all real-EDA interpretation remains subject to retained tool/library/input
evidence and the existing conservative RCA boundaries.
