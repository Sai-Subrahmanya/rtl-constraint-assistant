# RTL Constraint Assistant (RCA)

> RTL-aware timing constraint intelligence, SDC generation, validation, and
> multi-objective optimization for digital VLSI / EDA flows.

RCA parses Verilog/SystemVerilog RTL, builds a normalized design model, infers
clocks/resets/domains, and generates vendor-portable SDC with full provenance.
It validates constraints for correctness, coverage and conflicts, and — when
explicitly provisioned with Yosys/OpenSTA plus project collateral — runs a
closed-loop **multi-objective Pareto optimizer** that explores legitimate
candidate constraints while preserving fixed user intent and enforcing
correctness before any QoR optimization (Manual §2.2, §39). Synopsys and
Cadence support remains SDC dialect rendering only; RCA does not execute or
emulate commercial tools. For reviewed
false-path and multicycle exceptions, users can optionally map explicit
SymbiYosys (`sby`) proof jobs to UCM constraint IDs; RCA records their formal
outcomes and never treats structural analysis as a proof.

> **Design philosophy**: *Never silently invent design intent. Correctness before QoR. The Universal Constraint Model is the source of truth — SDC is a serialization.*

> **Knowledge reuse**: `rca knowledge` is an offline advisory lookup over
> typed patterns, current UCM projections, optional approved JSON data, and
> retained local history. It is not a second constraint source of truth; it
> never parses/executes data files, changes fixed user intent, or accepts a
> suggestion without an explicit UCM API action. See `STEP26_KNOWLEDGE_REUSE.md`.

> **Inference advice**: `rca infer` reports deterministic structural facts,
> candidates, ambiguity, conflicts, and missing information without changing
> UCM or coverage. It never fabricates clock periods, I/O budgets, generated
> clock details, relationships, or timing exceptions.
>
> **Controlled application**: a complete advisory candidate enters canonical
> UCM only through one explicit `ACCEPT`/`CONFIRM` decision, snapshot,
> semantic/conflict, dependency/assumption, MCMM-scope, and isolated existing
> validation checks. `rca apply` resolves one named candidate—never all of
> them—and its dry run never writes UCM, SDC, coverage, or history. See
> [`docs/STEP28_CONSTRAINT_APPLICATION.md`](docs/STEP28_CONSTRAINT_APPLICATION.md).

---

> **Constraint release**: an explicit Step-32 RCA release consumes one
> already-approved Step-31 review and exact canonical UCM evidence. It never
> auto-releases, generates SDC, or claims external EDA/STA/physical/commercial
> signoff. See [`docs/STEP32_CONSTRAINT_RELEASE.md`](docs/STEP32_CONSTRAINT_RELEASE.md).

## Quick start

```bash
pip install -e .

# Run on the included counter example
cd examples/simple_counter

# 1. Analyze the RTL (parses, elaborates, discovers clocks/resets/domains)
rca analyze project.yaml

# 2. See non-mutating evidence-backed inference advice
rca infer project.yaml
rca infer project.yaml --json

# 3. When the advisory JSON contains an evidence-complete ACCEPTABLE candidate,
#    review it and safely preview exactly that one candidate (this never changes UCM).
#    Copy IC-... from the advisory JSON; --candidate and --decision are required.
rca apply project.yaml --candidate IC-... --decision ACCEPT --dry-run --json

# 4. After review, explicitly apply that one validated candidate to a canonical UCM snapshot.
#    This writes only reviewed-ucm.json; it never applies all candidates or emits SDC.
rca apply project.yaml --candidate IC-... --decision ACCEPT --output reviewed-ucm.json --json

# 5. Assess closure readiness from the reviewed canonical UCM snapshot.
#    This is report-only: it neither writes UCM/SDC/coverage/history nor runs EDA.
rca readiness project.yaml --ucm reviewed-ucm.json --json

# 6. Trace canonical constraint lineage or compare two explicit UCM snapshots.
#    This is report-only and never writes history or runs EDA.
rca lineage project.yaml --ucm reviewed-ucm.json --json

# 7. Assess or explicitly approve the exact canonical UCM snapshot.
#    Review approval is governance only, never EDA/STA/physical signoff.
rca review project.yaml --ucm reviewed-ucm.json --json
rca review project.yaml --ucm reviewed-ucm.json --decision APPROVE --reviewer "reviewer" --json > approved-review.json

# 8. Release is a separate explicit governance action over the approved UCM.
#    It consumes an existing Step-31 review and is NOT EDA/STA/physical signoff.
rca release project.yaml --ucm reviewed-ucm.json --review approved-review.json --json
rca release project.yaml --ucm reviewed-ucm.json --review approved-review.json \
  --decision RELEASE --releaser "release-owner" --package-dir artifacts/release-r1 --json
rca release-verify artifacts/release-r1 --json

# 9. Generate SDC (generic / OpenSTA / Synopsys / Cadence backend)
rca generate project.yaml --backend generic
rca generate project.yaml --backend opensta

# 10. Validate generated constraints
#    (also runs mapped SymbiYosys jobs when formal.backend: symbiyosys is configured)
rca validate project.yaml

# 11. Show coverage
rca coverage project.yaml

# 12. Inspect real-EDA prerequisites without executing synthesis or STA
rca doctor project.yaml --json

# 13. Run Yosys + OpenSTA only when doctor reports the real boundary ready
#     and flow.liberty names your readable Liberty collateral.
rca run-sta project.yaml --backend yosys_opensta

# 14. Multi-objective optimization (mock EDA backend works without tools)
rca optimize project.yaml --backend mock

# 15. Query the local historical QoR repository (never executes EDA)
rca history --config project.yaml --best setup_wns

# 16. Search offline vendor-neutral constraint knowledge (advisory only)
rca knowledge search "false path" --json
rca knowledge suggest project.yaml --json

# 17. Full human-readable report
rca report project.yaml

# 18. Launch the web dashboard
rca dashboard project.yaml
```

### Example output (simple_counter)

```
RTL Constraint Assistant
========================
Design: counter
CLOCKS
  clk          detected / posedge / 10.000 ns / FIXED
RESETS
  rst_n        asynchronous / active_low
CLOCK RELATIONSHIPS
  (none — single clock)
CONSTRAINT QUALITY
  Clock source coverage: 100.0%
  Input timing coverage: 100.0%
  Output timing coverage: 100.0%
GENERATED CONSTRAINTS (3)
  [CLK0001] create_clock       -period 10.000 [get_ports clk]
  [INP0002] set_input_delay     2.000 -clock clk [get_ports en]
  [OUT0003] set_output_delay    2.000 -clock clk [get_ports q]
```

---

## Architecture

```
 Verilog / SV ──► Slang/pyslang ──► Design Model ──► Timing Graph
                                                     │
                            User / existing SDC ◄─────┤
                                                     ▼
                                          Universal Constraint Model
                                           (provenance, assumptions)
                                                     │
                 ┌──────────┬───────────┬────────────┼───────────┐
                 ▼          ▼           ▼            ▼           ▼
            Validation   Coverage    Conflicts   Exceptions   Equivalence
                 │          │           │         (formal)    (semantic)
                 └──────────┴───────────┴────────────┼───────────┘
                                                     ▼
                                          SDC backends (generic,
                                            OpenSTA, Synopsys, Cadence)
                                                     │
                                                     ▼
                     explicit Yosys / OpenSTA boundary (commercial execution unsupported)
                                                     │
                                                     ▼
                    QoR artifacts + local SQLite history sidecar
                    (artifacts/provenance and filesystem cache remain authoritative)
                                                     │
                                                     ▼
                                   Pareto multi-objective Optimizer
                                   (feasibility → Pareto → priority
                                    → timing-margin utilization)
```

The architecture enforces ten core invariants (Manual §154):
1. UCM is the source of truth.
2. SDC syntax is a backend concern.
3. Unknowns stay unknown until supplied.
4. Exceptions require stronger evidence.
5. Fixed user intent is immutable by the optimizer.
6. Every candidate is evaluated against **all** active objectives simultaneously.
7. Positive slack utilization is a soft preference — never an isolated final phase.
8. QoR improvement never justifies incorrect semantics.
9. Every important decision carries provenance.
10. Backend/version is recorded for reproducibility.

---

## Project layout

```
rtl-constraint-assistant/
├── pyproject.toml
├── README.md
├── LICENSE
├── Makefile
├── src/rca/
│   ├── cli/             # Typer CLI + FastAPI web dashboard
│   ├── config/          # Pydantic config + JSON schema
│   ├── source/          # Source manifest/resolver
│   ├── parser/          # Slang adapter (pyslang) + diagnostics
│   ├── design_model/    # Module, Port, Net, Instance, Register, Process
│   ├── timing_model/    # Clock, Reset, ClockDomain, TimingPath, TimingGraph
│   ├── constraint_model/# Universal Constraint Model, ConstraintSet, selectors, scenarios
│   ├── provenance/      # Evidence, AssumptionLedger, ProvenanceRecord
│   ├── inference/       # Advisory rules/engine + explicit controlled application
│   ├── validation/      # References, conflicts, coverage, master validator
│   ├── exceptions/      # Exception analysis + conservative/SymbiYosys formal backends
│   ├── equivalence/     # Normalization + semantic comparison
│   ├── sdc/             # SDC importer + generic/OpenSTA/Synopsys/Cadence backends
│   ├── eda/             # ToolBackend interface + Yosys/OpenSTA/Mock adapters
│   ├── reports/         # STA report parser (OpenSTA format)
│   ├── qor/             # Canonical QoR model, Pareto utilities, SQLite history repository
│   ├── optimizer/       # Budget, candidate generation, closed-loop optimizer
│   ├── scenarios/       # MCMM scenario handling
│   ├── explanation/     # Human/machine-readable explanation generator
│   ├── web/             # FastAPI dashboard
│   ├── artifacts/       # Output/run manifest management
│   └── utils/           # Enums, units, hashing, logging
├── tests/               # pytest unit/integration/golden/stress/regression
├── examples/
│   ├── simple_counter/
│   ├── pipeline/
│   ├── multi_clock/
│   └── ...
├── configs/schemas/     # JSON schema for project YAML
├── docs/                # Architecture, ADRs, constraint rules, tool adapters
└── scripts/             # Setup / regression / EDA helpers
```

---

## CLI reference

| Command            | Purpose |
|--------------------|---------|
| `rca init`         | Scaffold a new project directory with an RTL template. |
| `rca analyze`      | Parse/elaborate RTL, report structural findings & missing info. |
| `rca infer`        | Report non-mutating structural facts and advisory candidates (`--ucm SNAPSHOT.json`, `--json` available); no candidate is applied to UCM. |
| `rca apply`        | Resolve exactly one named advisory candidate with required `--candidate` and explicit `--decision`; validates an isolated UCM projection, supports `--dry-run`, optional repeatable `--scenario`, and writes a canonical snapshot only after a mutation. Never applies all candidates or emits SDC/history. |
| `rca readiness CONFIG --ucm SNAPSHOT.json [--scenario ID] [--json]` | Deterministic, report-only closure assessment of a supplied canonical UCM. Aggregates existing validation, coverage, MCMM, provenance, advisory/application, and configuration-aware preflight evidence; never writes UCM/SDC/coverage/history/artifacts or executes EDA/proofs. |
| `rca lineage CONFIG (--ucm SNAPSHOT.json \| --before A.json --after B.json) [--json]` | Deterministic read-only traceability projection for canonical constraints and explicit semantic snapshot comparison. Connects retained provenance, advice/application, validation/formal, readiness, knowledge, and MCMM evidence; never writes UCM/SDC/history/artifacts/SQLite or executes EDA/proofs. |
| `rca review CONFIG --ucm SNAPSHOT.json [--decision APPROVE\|APPROVE_WITH_WARNINGS\|REJECT\|DEFER\|REVOKE] [--json]` | Deterministic governance review of an exact canonical snapshot. Assessment never auto-approves; explicit approval remains separate from external EDA signoff and never writes UCM/SDC/artifacts/history/SQLite or executes EDA/formal. |
| `rca release CONFIG --ucm REVIEWED.json --review APPROVED-REVIEW.json [--decision RELEASE\|REVOKE] [--package-dir DIR] [--json]` | Deterministic release assessment or explicit RCA release over an exact reviewed UCM. It consumes rather than creates Step-31 review, preserves exact MCMM scope, never auto-releases or generates SDC, and remains distinct from external EDA/STA/physical/commercial signoff. A package is written only with explicit `--package-dir`. |
| `rca release-verify PACKAGE [--json]` | Stateless release-package integrity verification of hashes, canonical/semantic UCM identity, review/evidence identity, exact scope, consistency, and revoked/stale state. Never runs EDA/formal, mutates, repairs, or regenerates package contents. |
| `rca generate`     | Emit SDC (generic/opensta/synopsys/cadence backend). |
| `rca validate`     | Validate generated or imported SDC; runs configured SymbiYosys exception proofs if opted in. |
| `rca coverage`     | Per-category coverage report with uncovered objects. |
| `rca compare --a A.sdc --b B.sdc` | Semantic UCM-level diff between two SDC files with scenario and provenance context; unsupported or unresolved intent is reported as `UNKNOWN`, never equivalent. |
| `rca explain -c CID` | Explain why a constraint exists and its evidence. |
| `rca doctor [CONFIG] --json` | Run bounded executable/collateral readiness diagnostics; never executes synthesis, STA, or proofs. |
| `rca run-sta`      | Run the explicitly selected `mock` or `yosys_opensta` flow; real prerequisites fail closed with retained preflight evidence. |
| `rca optimize`     | Closed-loop multi-objective optimization; `optimization.workers` is the only bounded candidate-concurrency control (1–8, default 1), and it atomically writes an authoritative execution ledger before advisory QoR history indexing. |
| `rca history`      | Query/import the local SQLite QoR history sidecar, or inspect the artifact-authoritative optimizer ledger with `--optimization-ledger [--json]`. Never runs EDA, optimization, or cache reuse. |
| `rca inspect`      | Inspect clocks/resets/ports/registers/modules. |
| `rca report`       | Full human-readable design + constraints report. |
| `rca dashboard`    | Launch the FastAPI web UI. |
| `rca version`      | Print version. |

---

## Real EDA preflight and evidence (Step 25)

`mock` and `yosys_opensta` are explicit, separate choices. RCA never changes a
failed or unavailable real request into a mock result. Before a real run, it
performs a bounded, non-executing preflight of the selected backend, safe
Yosys/OpenSTA version probes, RTL and include collateral, Liberty files, the
fresh generated SDC, and the run-output locations. Use the same check directly:

```bash
rca doctor project.yaml
rca doctor project.yaml --json > doctor.json
```

The JSON document includes RCA/Python information, the project SDC backend as
configuration context, Yosys/OpenSTA readiness, optional SymbiYosys readiness,
Liberty/collateral findings, a deterministic non-secret environment fingerprint,
and an explicit classification such as `executable_missing`,
`collateral_missing`, `configuration_invalid`, or `unsupported`. A version probe
is bounded and runs no synthesis, STA, or proof job. `run-sta` writes an
explicit prerequisite diagnostic and exits non-zero when the real boundary is
not ready.

A real `run-sta` needs a compatible Yosys executable, OpenSTA executable, and
at least one readable Liberty file. Set the per-tool timeout explicitly when
needed; it is a run guard/provenance setting rather than a QoR/cache identity:

```yaml
flow:
  liberty: /absolute/or/project-relative/path/to/cells.lib
  tool_timeout_seconds: 600
```

Each real or mock run has exactly one authoritative run-local
`output/runs/<run-id>/run_manifest.json`. It retains backend/tool versions,
argv/cwd/timeout and bounded diagnostic tails, input and output hashes, QoR and
configured power-report provenance, explicit `MOCK`/`REAL` execution mode,
execution/failure status, and the non-secret environment fingerprint. Failed,
timed-out, missing-output, or
malformed-output real invocations remove prior current-run timing/power/QoR
outputs before execution and never produce a successful QoR or cache hit.
`QoRResult` remains canonical QoR; the manifest/hash filesystem evidence
remains cache authority and SQLite remains an advisory history sidecar.

Commercial SDC dialects (`synopsys`, `cadence`) remain serializers only. RCA
reports commercial execution as unsupported/unavailable; it does not invent
PrimeTime, Tempus, or signoff results.

---

## Optional formal exception verification

Step 14 adds an opt-in SymbiYosys adapter for reviewed false-path and
multicycle proof jobs. It uses the existing formal/validation abstractions:
`formal.backend` defaults to `conservative`, so projects do not invoke an
external tool or claim a proof unless they choose `symbiyosys` and map an
explicit user-authored `.sby` file to a UCM exception ID.

```yaml
formal:
  backend: symbiyosys
  proofs:
    - constraint_id: FP0001
      exception_kind: false_path
      sby_file: formal/async_fifo.sby
      task: async_fifo_fp
```

Only `sby` **PASS** with exit code zero is `VERIFIED`; `FAIL` is `INVALID`
and preserves counterexample artifact paths. A missing mapping/tool/file,
timeout, `UNKNOWN`, or indeterminate output stays `UNRESOLVED`. See
`DOCUMENTATION.md` and `STEP14_REPORT.md` for the safety, configuration, and
MCMM provenance details.

---

## Configured OpenROAD/OpenSTA power reports (Step 20)

`rca run-sta` and real `rca optimize` flows can ingest one explicitly
configured OpenROAD/OpenSTA-style `report_power` group-summary text report per
scenario. RCA parses the final `Total` row only when the report has its
Internal, Switching, Leakage, and Total columns and an explicit supported unit;
it normalizes W, mW, uW/µW, nW, and pW to watts.

```yaml
flow:
  power_reports:
    - format: openroad_report_power
      path: reports/func_slow.power.rpt
      scenario_id: FUNC_SLOW    # required for MCMM; optional for one scenario
```

`Total` becomes the existing QoR `power`/`power_total`; dynamic power is
`Internal + Switching` only when both cells are reported, and leakage comes
from the Leakage cell. Missing, ambiguous, malformed, invalid, or unsupported
reports never become zero. The parser records those detailed outcomes as
`PowerParseStatus` in `raw_reports["power"]["parsing_status"]`; canonical
`QoRResult.power_status` remains backward-compatible (`AVAILABLE`,
`UNAVAILABLE`, or existing caller-supplied `ESTIMATED`) and maps every
non-available parse classification to `UNAVAILABLE`. Each accepted report
carries its path, SHA-256, format, unit, scenario/mode/corner, and parser
diagnostics into the existing QoR summary and run manifest; its content is
cache-relevant.

This feature ingests a **configured tool report**. It does not run a power tool,
produce activity data, or claim physical/silicon measurement. Mock flow remains
explicitly mock and power-unavailable. See `STEP20_POWER_REPORT.md` for the
complete supported grammar, status policy, MCMM behavior, and limitations.

---

## Local QoR history repository (Step 21)

RCA writes a local SQLite sidecar at `<flow.output_dir>/qor.sqlite3` after an
existing real-flow manifest/artifact set is complete, and after existing
optimizer artifacts are written. It is a query/index layer only:

- `QoRResult` remains the canonical QoR model.
- `RunManifest` and `ArtifactManager` remain artifact/provenance authorities.
- The filesystem manifest/hash cache remains the only experiment-reuse authority.
- SQLite never copies artifacts and never causes a cache hit or an EDA run.

Use the focused read-only history interface:

```bash
rca history --config project.yaml --run-id RUN_ID
rca history --config project.yaml --candidate C001 --session SESSION_ID
rca history --config project.yaml --scenario FUNC_SLOW
rca history --config project.yaml --constraint-set HASH
rca history --config project.yaml --best power --json
rca history --config project.yaml --import-legacy
```

`--import-legacy` is explicit and idempotently indexes existing manifests and
`qor.json` files without rewriting them. It also indexes the retained root
`candidates.jsonl` optimizer snapshot (or `optimizer_state.json` when JSONL is
absent) as a clearly legacy, session-scoped record. It reports missing or
conflicting historical evidence; summaries cannot recreate fields that were
never written.
Power best queries require canonical `AVAILABLE` report-derived power and omit
mock results by default. Area queries use real mapped area by default; proxy
area requires `--area-source proxy` and is never mixed with real area. Replay
output is retained identity and artifact-integrity evidence, not a promise that
RCA can rerun an EDA experiment. See `STEP21_QOR_DATABASE.md` and
`docs/decisions/ADR-004-qor-history-sqlite.md`.

---

## Enhancements added beyond the manual

With your permission, the following enhancements were incorporated (documented here):

1. **Pydantic data models** for strict validation of configuration and internal objects.
2. **Rich CLI** for beautiful, structured console output.
3. **FastAPI web dashboard** with live constraint/coverage/QoR views (`rca dashboard`).
4. **JSON Schema** for the project configuration (versioned).
5. **Pytest validation taxonomy** with separate unit, golden/reference, integration, regression-runner, stress/concurrency, and opt-in real-EDA categories. The default suite uses deterministic mock/fake fixtures only; `tests/integration/test_optional_real_eda.py` is skipped unless a user explicitly provides tools and collateral. See `STEP24_VALIDATION.md` for current category counts, failure coverage, and commands.
6. **Deterministic hashing** utilities for reproducibility/caching.
7. **Step 25 real-EDA boundary hardening**: typed preflight, bounded argv-only subprocess evidence, stale-output protection, one authoritative run manifest, and controlled fake-tool coverage. See `STEP25_REAL_EDA_HARDENING.md`.

---

## Development

```bash
pip install -e ".[dev]"
pytest -q                                      # all deterministic validation categories
python scripts/regression/run_regression.py    # documented core → golden → integration → stress gates
pytest tests/golden -q                         # reference outputs
pytest tests/integration -q                    # parser-to-artifact workflows (real EDA tests skip by default)
pytest tests/stress -q                         # short deterministic scheduler stress suite
python scripts/setup/verify_environment.py     # no-install prerequisite report
rca doctor project.yaml --json                 # bounded real-EDA/formal readiness evidence
python scripts/eda/diagnose_eda.py --config project.yaml  # legacy version-probe-only EDA diagnostic
ruff check src/ tests/                         # lint
mypy src/rca                                   # type-check
make example                                   # run simple_counter end-to-end
```

The default tests do not require Yosys, OpenSTA, SymbiYosys, a Liberty library,
or commercial software. To opt into a real flow, set `RCA_RUN_REAL_EDA=1` and
provide `RCA_REAL_EDA_LIBERTY`; missing prerequisites produce **SKIPPED**
optional tests, never fabricated results. `AVAILABLE` in an environment
diagnostic means only that a version probe succeeded, not that signoff or a
complete flow is guaranteed. See `STEP24_VALIDATION.md`.

## Docker local setup

The default image is RCA plus its declared Python/runtime dependencies only:

```bash
docker build -t rca .
docker run --rm -v "$PWD:/work" rca doctor /work/project.yaml --json
```

Yosys is an explicit convenience target, not a required Python dependency:

```bash
docker build --target open-source-yosys -t rca:yosys .
```

Neither image bundles OpenSTA, a Liberty/PDK, activity data, or proprietary
software. It does not download tools at container runtime. Provision compatible
open-source executables and project collateral deliberately, then run `rca
doctor`; the container and RCA do not claim signoff.

## References

See `docs/references.md` for the full list of academic and industry sources
cited in the project manual (Synopsys TCM, Cadence Conformal CCD, OpenSTA,
OpenROAD, slang/Surelog, Yosys, and the timing-exception optimization
literature).

## License

MIT — see `LICENSE`.
