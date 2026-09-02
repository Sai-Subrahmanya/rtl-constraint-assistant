# RTL Constraint Assistant (RCA)

> RTL-aware timing constraint intelligence, SDC generation, validation, and
> multi-objective optimization for digital VLSI / EDA flows.

RCA parses Verilog/SystemVerilog RTL, builds a normalized design model, infers
clocks/resets/domains, and generates vendor-portable SDC with full provenance.
It validates constraints for correctness, coverage and conflicts, and — when
integrated with Yosys/OpenSTA (or commercial Synopsys/Cadence tools) — runs a
closed-loop **multi-objective Pareto optimizer** that explores legitimate
candidate constraints while preserving fixed user intent and enforcing
correctness before any QoR optimization (Manual §2.2, §39). For reviewed
false-path and multicycle exceptions, users can optionally map explicit
SymbiYosys (`sby`) proof jobs to UCM constraint IDs; RCA records their formal
outcomes and never treats structural analysis as a proof.

> **Design philosophy**: *Never silently invent design intent. Correctness before QoR. The Universal Constraint Model is the source of truth — SDC is a serialization.*

---

## Quick start

```bash
pip install -e .

# Run on the included counter example
cd examples/simple_counter

# 1. Analyze the RTL (parses, elaborates, discovers clocks/resets/domains)
rca analyze project.yaml

# 2. See inference engine proposals
rca infer project.yaml

# 3. Generate SDC (generic / OpenSTA / Synopsys / Cadence backend)
rca generate project.yaml --backend generic
rca generate project.yaml --backend opensta

# 4. Validate generated constraints
#    (also runs mapped SymbiYosys jobs when formal.backend: symbiyosys is configured)
rca validate project.yaml

# 5. Show coverage
rca coverage project.yaml

# 6. Run synthesis (Yosys) + STA when a Liberty library is available
rca run-sta project.yaml --backend yosys_opensta

# 7. Multi-objective optimization (mock EDA backend works without tools)
rca optimize project.yaml --backend mock

# 8. Full human-readable report
rca report project.yaml

# 9. Launch the web dashboard
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
                                       Yosys / OpenSTA / commercial STA
                                                     │
                                                     ▼
                                                 QoR DB
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
│   ├── inference/       # Rule registry + clock/reset/IO/gclk rules + engine
│   ├── validation/      # References, conflicts, coverage, master validator
│   ├── exceptions/      # Exception analysis + conservative/SymbiYosys formal backends
│   ├── equivalence/     # Normalization + semantic comparison
│   ├── sdc/             # SDC importer + generic/OpenSTA/Synopsys/Cadence backends
│   ├── eda/             # ToolBackend interface + Yosys/OpenSTA/Mock adapters
│   ├── reports/         # STA report parser (OpenSTA format)
│   ├── qor/             # QoR model, Pareto filter, candidate scoring
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
| `rca infer`        | Run inference engine and print proposed constraints. |
| `rca generate`     | Emit SDC (generic/opensta/synopsys/cadence backend). |
| `rca validate`     | Validate generated or imported SDC; runs configured SymbiYosys exception proofs if opted in. |
| `rca coverage`     | Per-category coverage report with uncovered objects. |
| `rca compare --a A.sdc --b B.sdc` | Semantic UCM-level diff between two SDC files with scenario and provenance context; unsupported or unresolved intent is reported as `UNKNOWN`, never equivalent. |
| `rca explain -c CID` | Explain why a constraint exists and its evidence. |
| `rca run-sta`      | Run synthesis + STA and collect QoR. |
| `rca optimize`     | Closed-loop multi-objective optimization. |
| `rca inspect`      | Inspect clocks/resets/ports/registers/modules. |
| `rca report`       | Full human-readable design + constraints report. |
| `rca dashboard`    | Launch the FastAPI web UI. |
| `rca version`      | Print version. |

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
reports never become zero. Each accepted report carries its path, SHA-256,
format, unit, scenario/mode/corner, and parser diagnostics into the existing
QoR summary and run manifest; its content is cache-relevant.

This feature ingests a **configured tool report**. It does not run a power tool,
produce activity data, or claim physical/silicon measurement. Mock flow remains
explicitly mock and power-unavailable. See `STEP20_POWER_REPORT.md` for the
complete supported grammar, status policy, MCMM behavior, and limitations.

---

## Enhancements added beyond the manual

With your permission, the following enhancements were incorporated (documented here):

1. **Pydantic data models** for strict validation of configuration and internal objects.
2. **Rich CLI** for beautiful, structured console output.
3. **FastAPI web dashboard** with live constraint/coverage/QoR views (`rca dashboard`).
4. **JSON Schema** for the project configuration (versioned).
5. **Pytest** test suite with dedicated Step-11 Pareto/optimizer coverage (125 tests), Step-12 MCMM coverage (70 tests), Step-13 validation scenarios (40 tests), Step-14 SymbiYosys formal-adapter coverage (11 tests), Step-15 semantic-comparison audit coverage (67 tests), and Step-20 power-report ingestion coverage (34 tests) — full suite **850 collected / 850 passed**. Coverage spans parser, constraints, SDC parsing/generation, connectivity, timing model, inference, equivalence, validation (reference/semantic/conflicts/overlap/coverage/completeness/exception-safety/scenario/SDC-import/backend), formal proof verdicts/provenance, configured power reports, EDA flow, determinism, units and expression semantics; exercising the Verilog/SystemVerilog front-end additionally requires the `pyslang` package.
6. **Deterministic hashing** utilities for reproducibility/caching.

---

## Development

```bash
pip install -e ".[dev]"
pytest                    # run unit tests
ruff check src/ tests/    # lint
mypy src/rca              # type-check
make example              # run simple_counter end-to-end
```

## References

See `docs/references.md` for the full list of academic and industry sources
cited in the project manual (Synopsys TCM, Cadence Conformal CCD, OpenSTA,
OpenROAD, slang/Surelog, Yosys, and the timing-exception optimization
literature).

## License

MIT — see `LICENSE`.
