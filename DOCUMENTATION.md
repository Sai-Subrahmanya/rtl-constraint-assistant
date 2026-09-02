# RTL Constraint Assistant (RCA) — Complete Reference Manual

*Repository:* `rtl-constraint-assistant/` at `/home/user/rtl-constraint-assistant/`
*Version:* 0.1.0-alpha
*License:* MIT
*Python:* ≥ 3.10 (developed and tested on 3.13)

---

## Table of Contents

1. [What RCA Is](#1-what-rca-is)
2. [Architecture at a Glance](#2-architecture-at-a-glance)
3. [Repository Layout (every file and folder)](#3-repository-layout)
4. [Root-level Project Files](#4-root-level-project-files)
5. [Source Tree (`src/rca/`) — Subsystem by Subsystem](#5-source-tree)
6. [Config, Config Schemas, and Scripts](#6-config-and-scripts)
7. [Examples](#7-examples)
8. [Tests](#8-tests)
9. [Docs](#9-docs)
10. [Universal Constraint Model (UCM) — deep dive](#10-universal-constraint-model)
11. [End-to-End Data Flow](#11-end-to-end-data-flow)
12. [CLI Reference](#12-cli-reference)
13. [Writing a Project YAML](#13-writing-a-project-yaml)
14. [Safe Modes, Confidence, Provenance, and Invariants](#14-safety-invariants)
15. [MCMM Scenarios](#15-mcmm-scenarios)
16. [Optimization & Pareto Loop](#16-optimization-and-pareto)
17. [Web Dashboard](#17-web-dashboard)
18. [Docker](#18-docker)
19. [Known Gaps and Roadmap](#19-known-gaps)
20. [Troubleshooting / FAQ](#20-faq)

---

## 1. What RCA Is

RCA is an EDA (Electronic Design Automation) tool that takes synthesizable
Verilog / SystemVerilog RTL plus an optional project configuration, and:

1. **Parses and elaborates** the RTL via **pyslang** (the Python binding to
   the slang SystemVerilog compiler).
2. **Builds a normalized Design Model** of modules, ports, nets, registers,
   processes, instances.
3. **Infers timing intent** — clocks, resets, clock domains, cross-domain
   paths, generated-clock candidates, and missing information — using
   pluggable inference rules.
4. **Imports any existing SDC** into a vendor-neutral **Universal Constraint
   Model (UCM)** so there is one source of truth.
5. **Generates SDC** for multiple backends (generic, OpenSTA, Synopsys
   PrimeTime-style, Cadence Tempus-style).
6. **Validates** constraints (reference integrity, internal conflicts, I/O
   coverage) and produces structured diagnostics.
7. **Optionally runs Yosys synthesis + OpenSTA static timing analysis** (or
   a built-in mock EDA flow) and parses the resulting timing reports.
8. **Performs closed-loop multi-objective Pareto optimization** over
   setup/hold/area/power to tune constraints within user-provided bounds
   while respecting confidence and provenance.
9. **Explains** each constraint in natural language: where it came from,
   what evidence supports it, what assumptions it depends on.
10. Provides a **FastAPI web dashboard**, **rich CLI** output, Pydantic
    validation, JSON Schema for configs, pytest with coverage, and a
    Dockerfile.

The UCM is the source of truth; SDC text is *always* a derived artifact.
RCA never silently invents design intent: anything it cannot prove is
reported as missing information rather than guessed.

---

## 2. Architecture at a Glance

```
                  RTL files (.sv/.v)     project.yaml     existing SDC
                          │                    │                 │
                          ▼                    ▼                 ▼
            ┌─────────────────────────┐  ┌──────────┐  ┌─────────────────┐
            │  Source resolution      │  │ Pydantic │  │  SDC importer   │
            │  (rca.source.manifest)  │  │ ProjectC │  │  (rca.sdc.parse)│
            └────────────┬────────────┘  └────┬─────┘  └────────┬────────┘
                         │                   │                 │
                         ▼                   ▼                 │
            ┌──────────────────────────────────────────┐       │
            │ Parser adapter (pyslang) → Design Model  │◄──────┘
            │ rca.parser.{base,slang_adapter,diagnos.} │
            └──────────────────┬───────────────────────┘
                               ▼
            ┌──────────────────────────────────────────┐
            │ Timing Graph  (rca.timing_model)         │
            │ clocks, resets, domains, paths, CDC      │
            └──────────────────┬───────────────────────┘
                               ▼
            ┌──────────────────────────────────────────┐
            │ Inference Engine + Rules  (rca.inference)│
            │ clocks, resets, IO, generated clocks     │
            └──────────────────┬───────────────────────┘
                               ▼
            ┌──────────────────────────────────────────┐
            │  Universal Constraint Model  (rca.cons… )│ ◄──── Assumption
            │  ConstraintSet + provenance + scenarios   │       Ledger
            └───┬──────────────┬──────────────┬────────┘
                │              │              │
                ▼              ▼              ▼
        ┌──────────┐   ┌─────────────┐  ┌───────────────┐
        │ SDC Gen  │   │ Validator   │  │ EDA backends  │
        │ generic/ │   │ refs/conf/  │  │ Yosys/OpenSTA/│
        │ opensta/ │   │ coverage    │  │ Mock          │
        │ synopsys/│   └──────┬──────┘  └───────┬───────┘
        │ cadence/ │          │                 │
        └─────┬────┘          │                 ▼
              │               │        ┌────────────────┐
              ▼               ▼        │ QoR + Pareto   │
        ┌──────────┐    ┌──────────┐   │ multi-obj opt  │
        │ Equiv/   │    │ Explain/ │   └────────┬───────┘
        │ Compare  │    │ Reports  │            │
        └──────────┘    └──────────┘            ▼
                                          ┌──────────┐
                                          │Dashboard │
                                          │ FastAPI  │
                                          └──────────┘
```

---

## 3. Repository Layout

Full tree (non-generated files, 128 total):

```
rtl-constraint-assistant/
├── .gitignore
├── Dockerfile
├── LICENSE
├── Makefile
├── README.md
├── DOCUMENTATION.md                ← this file
├── pyproject.toml
├── configs/
│   ├── examples/.gitkeep
│   └── schemas/
│       └── project.schema.json
├── docs/
│   ├── references.md
│   └── decisions/
│       └── ADR-001-universal-constraint-model.md
├── examples/
│   ├── simple_counter/
│   │   ├── project.yaml
│   │   └── rtl/
│   │       └── counter.sv
│   ├── pipeline/
│   │   ├── project.yaml
│   │   └── rtl/
│   │       └── pipeline.sv
│   └── multi_clock/
│       ├── project.yaml
│       └── rtl/
│           └── two_clocks.sv
├── results/.gitkeep
├── scripts/
│   ├── eda/.gitkeep
│   ├── regression/.gitkeep
│   └── setup/.gitkeep
├── src/
│   └── rca/
│       ├── __init__.py
│       ├── artifacts/
│       ├── cli/
│       ├── config/
│       ├── constraint_model/
│       ├── design_model/
│       ├── eda/
│       ├── elaboration/
│       ├── equivalence/
│       ├── exceptions/
│       ├── explanation/
│       ├── inference/
│       ├── optimizer/
│       ├── parser/
│       ├── provenance/
│       ├── qor/
│       ├── reports/
│       ├── scenarios/
│       ├── sdc/
│       ├── search/
│       ├── source/
│       ├── timing_model/
│       ├── utils/
│       ├── validation/
│       └── web/
├── tests/
│   ├── __init__.py
│   ├── conftest.py
│   ├── golden/__init__.py
│   ├── integration/__init__.py
│   ├── regression/__init__.py
│   ├── stress/__init__.py
│   └── unit/
│       ├── __init__.py
│       ├── test_constraints.py
│       ├── test_pareto.py
│       ├── test_parser.py
│       ├── test_sdc_parser.py
│       └── test_units.py
└── src/rtl_constraint_assistant.egg-info/   (generated by pip install -e .)
```

> Generated artifacts (build/, dist/, __pycache__/, *.egg-info, pytest
> cache, .mypy_cache, .ruff_cache, examples/*/output/) are excluded via
> `.gitignore` and recreated on demand; they are *not* source of truth.

---

## 4. Root-level Project Files

| File | Lines | Purpose |
|---|---|---|
| `pyproject.toml` | 68 | PEP-621 project metadata, build system, dependencies, entry points, pytest/ruff/mypy config. |
| `Makefile` | 44 | Convenience targets: `install`, `dev-install`, `test`, `test-cov`, `lint`, `typecheck`, `clean`, `example`, `example-pipeline`, `example-multiclock`, `dashboard`. |
| `Dockerfile` | 34 | Reproducible container with Debian trixie, Python 3, Yosys, build tools, RCA installed via pip, optional OpenSTA build (commented out). Entrypoint `rca`, port 8765 for dashboard. |
| `LICENSE` | 21 | MIT license. |
| `README.md` | 221 | Quick-start overview, architecture diagram, CLI list, installation. |
| `.gitignore` | 22 | Excludes bytecode, virtualenvs, caches, build artifacts, example output dirs, IDE files. |

### `pyproject.toml` contents in detail

- **Build system**: `setuptools>=68` + `wheel`, `setuptools.build_meta`.
- **Project metadata**: name `rtl-constraint-assistant`, version `0.1.0`,
  MIT license, requires `python>=3.10`, classifiers for Python 3.10–3.13
  and "Scientific/Engineering :: EDA".
- **Runtime dependencies** (all installed via pip):
  - `pydantic>=2.0` — data model validation (Enhancement).
  - `typer>=0.9` — CLI framework (Enhancement: rich CLI).
  - `rich>=13.0` — terminal formatting/tables.
  - `pyyaml>=6.0` — YAML config parsing.
  - `jsonschema>=4.0` — config schema validation (Enhancement).
  - `pyslang>=11.0` — SystemVerilog parser/elaborator (WP-B).
  - `networkx>=3.0` — timing graph structure.
  - `numpy>=1.24` — numeric work for optimization.
  - `jinja2>=3.1` — SDC/HTML report templating.
  - `fastapi>=0.100` + `uvicorn>=0.23` — web dashboard (Enhancement).
- **`[project.optional-dependencies].dev`**: pytest, pytest-cov,
  pytest-mock, ruff, mypy.
- **`[project.scripts]`**: entry point `rca = rca.cli.main:app`.
- **`[tool.pytest.ini_options]`**: `testpaths=tests`, `pythonpath=[src]`,
  default options `-v --tb=short`.
- **`[tool.ruff]`**: line length 100, target py310.
- **`[tool.mypy]`**: py310, `warn_return_any`.

### `Makefile` targets

| Target | Effect |
|---|---|
| `make install` | `pip3 install -e .` (editable install). |
| `make dev-install` | install with `[dev]` extras (pytest, ruff, mypy). |
| `make test` | `pytest tests/ -v`. |
| `make test-cov` | pytest with `--cov=rca --cov-report=term-missing`. |
| `make lint` | `ruff check src/ tests/`. |
| `make typecheck` | `mypy src/rca --ignore-missing-imports`. |
| `make clean` | remove build/, dist/, eggs, example outputs, pycaches. |
| `make example` | runs `rca report` on `examples/simple_counter/`. |
| `make example-pipeline` | same for the pipeline example. |
| `make example-multiclock` | same for the multi-clock example. |
| `make dashboard` | launches the FastAPI dashboard on simple_counter. |

---

## 5. Source Tree

Every Python module under `src/rca/` is 7,614 lines total. Subsystems
below, each with each file's purpose.

### 5.1 `src/rca/__init__.py` (10 lines)

Package docstring describing RCA. Re-exports the public `__version__`.

### 5.2 `src/rca/utils/` — foundational utilities

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | — | Package marker. |
| `enums.py` | 298 | **Central type vocabulary.** Defines every enum used across the system: `ConstraintType` (CREATE_CLOCK, SET_INPUT_DELAY, SET_FALSE_PATH, …), `ConstraintStatus` (FIXED, CONFIRMED, PROPOSED, REQUIRES_CONFIRMATION, INFEASIBLE), `OptimizationStatus` (FIXED, TUNABLE, REJECTED), `Confidence`, `GenerationConfidence`, `SourceKind` (USER, INFERENCE, IMPORT, FORMAL, EDA_BACKEND, SCENARIO_DEFAULT), `SignalSense` (POSEDGE, NEGEDGE, BOTH), `ResetKind` (SYNC, ASYNC), `ResetPolarity` (ACTIVE_HIGH, ACTIVE_LOW), `Severity` (CRITICAL, ERROR, WARNING, HIGH, MEDIUM, LOW, INFO), `EquivalenceResult` (EQUIVALENT, DIFFERENT, OVERLAPPING, CONFLICTING), `SafeMode` (AGGRESSIVE, BALANCED, STRICT — see §14), `OptimizationObjective`, `StopReason`, `ClockRelationship` (SYNCHRONOUS, ASYNCHRONOUS, PLESIOSYNCHRONOUS, UNRELATED), `MissingInfoKind`, `ErrorCode` (per-module code space). |
| `units.py` | 140 | Time/frequency parsing and conversion. Internal unit is SI **seconds** (float). Helpers: `ns()`, `ps()`, `fs()` → seconds; `to_ns(s)`, `to_ps(s)`, `to_fs(s)`; `parse_time("2.5ns")`, `parse_freq("500MHz")` → period in seconds; `period_to_freq_hz()` / `freq_to_period()`. |
| `hashing.py` | 48 | Deterministic hashing for reproducibility. `stable_hash(obj)` (SHA-256 of a canonical JSON serialization), `hash_file(path)`, `hash_files(paths)` for source sets, `hash_constraint_set()`. Used by the artifact manager and run manifests (Manual §66, §67). |
| `logging.py` | 77 | Structured logging via `rich.logging.RichHandler` for the console. Configurable level, optional JSON-formatted machine logs. `setup_logging(level, json=False)`. |

### 5.3 `src/rca/parser/` — RTL front-end (WP-B)

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | — | Re-exports `SlangAdapter`, `DiagnosticCollection`, `ParserDiagnostic` etc. |
| `base.py` | — | Abstract `ParserAdapter` ABC defining the contract any parser backend (pyslang, Surelog/UHDM, future) must implement: `parse(files, include_dirs, defines, top, params) → Design`. |
| `diagnostics.py` | ~90 | `Diagnostic` dataclass and `DiagnosticCollection`: a structured issue bag with `severity`, `code: ErrorCode`, `message`, `file`, `line`, `hint`. Provides `.errors()`, `.warnings()`, `worst_severity()`. Severity levels use INFO/LOW/MEDIUM/HIGH/WARNING/ERROR/CRITICAL. |
| `slang_adapter.py` | ~490 | **Primary parser/elaborator (WP-B).** Wraps `pyslang.Compilation` to (1) create a compilation with proper options (include dirs, defines, param overrides), (2) parse all files, (3) elaborate the top, (4) walk the elaborated tree to construct the normalized Design Model (`design_model.*`): modules, ports (direction, width, clock/reset signal detection via naming heuristics + sensitivity-list inspection), nets (wires/logic), processes (always/always_ff/always_comb), registers (extracted from non-blocking assignments in clocked always_ff blocks, with clock edge, reset kind/polarity, async/sync detection), instances (for hierarchical designs). It hooks `Compilation` diagnostics (errors/warnings) into the RCA `DiagnosticCollection`. Heuristics: signals named `clk*`, `clock`, `*_clk` → clock candidates; `rst*`, `reset*`, `*_rst_n`, `rst_n` → reset candidates; sensitivity list with `posedge clk or negedge rst_n` → async active-low reset; `posedge clk` only → sync reset. |

### 5.4 `src/rca/design_model/` — normalized netlist model (WP-C)

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | — | Re-exports the public model classes. |
| `port.py` | ~50 | `Port` dataclass: `name`, `hierarchical_name`, `direction` (INPUT/OUTPUT/INOUT), `width` (bits), `port_type` (CLOCK/RESET/DATA/SCAN/OTHER), `bus_range`, `is_clock`, `is_reset`, `attributes`. |
| `net.py` | ~40 | `Net` dataclass: `name`, `hierarchical_name`, `width`, `drivers`, `loads`, `type` (WIRE/REG/LOGIC/OTHER). |
| `register.py` | ~60 | `Register` dataclass: `name`, `hierarchical_name`, `width`, `clock_signal`, `clock_edge` (POSEDGE/NEGEDGE), `reset_signal` (optional), `reset_polarity`, `reset_kind` (SYNC/ASYNC), `has_enable`. |
| `process.py` | ~50 | `Process` dataclass: `name`, `kind` (ALWAYS_FF / ALWAYS_COMB / ALWAYS_LATCH / INITIAL / OTHER), `sensitivity_list`, `blocking_assigns`, `nonblocking_assigns`. |
| `instance.py` | ~40 | `Instance` dataclass: `name`, `module_name`, `hierarchical_name`, `port_connections: dict[str,str]`, `parameters`. |
| `module.py` | ~80 | `Module` dataclass: `name`, `ports`, `nets`, `registers`, `processes`, `instances`, `parameters`, `source_file`. Methods: `clocks()`, `resets()`, `inputs()`, `outputs()`, `summary()`. |
| `design.py` | ~90 | `Design` dataclass: top-level container holding `modules: dict[str,Module]`, `top_module_name`, `source_files`, `elaboration_options`. Methods: `top_module()`, `summary()`, `all_ports()`, `all_registers()`, `all_instances()`. |

### 5.5 `src/rca/timing_model/` — clocks, domains, paths (WP-D)

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | — | Re-exports. |
| `clock.py` | ~80 | `Clock` and `ClockCandidate`: `name`, `source`, `period_seconds`, `waveform` (edge list), `is_generated`, `master_clock`, `divide_by`, `multiply_by`, `edge` (POSEDGE/NEGEDGE), `confidence`. |
| `reset.py` | ~40 | `Reset`: `name`, `polarity`, `kind` (sync/async), `clock_name`, `confidence`. |
| `clock_domain.py` | ~70 | `ClockDomain`: `name`, `clock`, `reset`, `registers: list[str]`, `is_async_to: set[str]`. |
| `timing_path.py` | ~80 | `TimingPath`: `startpoint`, `endpoint`, `start_clock`, `end_clock`, `path_type` (REG2REG/IN2REG/REG2OUT/IN2OUT/CDC), `is_cross_domain`, `logic_depth`. |
| `timing_graph.py` | ~180 | `TimingGraph` — the central timing view built from a `Design`. Discovers clock/reset candidates, constructs `ClockDomain`s, classifies registers by their clocking process, enumerates timing paths between registers and across I/O, marks CDC edges. Methods: `build(design, user_clocks)`, `clocks()`, `domains()`, `paths()`, `cross_domain_paths()`, `missing_information()`, `summary()`. |

### 5.6 `src/rca/provenance/` — evidence, assumptions, audit trail (WP-E)

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | — | Re-exports. |
| `evidence.py` | ~60 | `Evidence` dataclass: `source_kind` (USER/INFERENCE/IMPORT/…), `description`, `source_file`, `line`, `details`. Supports a human-readable `explain()`. |
| `assumption.py` | ~60 | `Assumption` dataclass: `id`, `kind` (DEFAULT_CLOCK_PERIOD, DEFAULT_IO_DELAY, …), `description`, `severity` (LOW/MEDIUM/HIGH), `default_value`, `justification`, `resolution_hint`. |
| `provenance.py` | ~80 | `ProvenanceRecord`: bundles `evidences: list[Evidence]`, `assumptions: list[Assumption]`, `created_by` (rule/user/backend name), `created_at` (ISO timestamp), `confidence: Confidence`. Also `AssumptionLedger`, a collector used during inference to record every default/fallback assumption made (output in reports and the "missing info" section). |

### 5.7 `src/rca/constraint_model/` — the Universal Constraint Model (WP-E)

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | — | Re-exports `Constraint`, `ConstraintSet`, `PathSelector`, `Scenario`. |
| `selectors.py` | ~40 | `PathSelector` (Manual §36): structured SDC path filter — `from_set`, `to_set`, `through_set` (list of lists for multiple -through), plus helpers `from_sdc(...)` and `to_sdc_args()`. |
| `constraint.py` | ~180 | `Constraint` — single UCM object. Fields: `id`, `type: ConstraintType`, `target_objects: list[str]`, `path_selector: PathSelector | None`, `values: dict`, `status: ConstraintStatus`, `opt_status: OptimizationStatus`, `confidence: Confidence`, `generation_confidence: GenerationConfidence`, `source_kind: SourceKind`, `scenario`, `comment`, `provenance: ProvenanceRecord | None`, `disabled: bool`. Methods: `summary()`, `is_safe_to_emit(mode)` (see §14), `explain()` (natural language), `clone()`. Uses `ConfigDict(arbitrary_types_allowed=True)` per Pydantic V2 (warning fixed). |
| `scenarios.py` | ~50 | `Scenario` dataclass for MCMM: `name`, `mode` (func/scan/atpg/test/sleep/…), `operating_condition`, `process_corner`, `voltage`, `temperature`, `derate_setup`, `derate_hold`, `clock_scenarios`, `case_analysis`, `disabled_paths`. `summary()`. |
| `constraint_set.py` | 330 | **Core UCM container** (see §10). Pydantic model holding `constraints: dict[id, Constraint]`, `scenarios`, `assumptions`, `metadata`. Factory helpers for every constraint type: `create_clock(...)`, `create_generated_clock(...)`, `create_input_delay(...)`, `create_output_delay(...)`, `create_false_path(...)`, `create_multicycle(...)`, `create_clock_groups(...)`, `create_clock_uncertainty(...)`, `create_design_rule(...)`, plus the generic `add_constraint_by_type(...)` used by the SDC importer. Queries: `by_type(t)`, `clocks()`, `generated_clocks()`, `exceptions()`, `io_constraints()`, `get(id)`. `emittable(mode)` returns the canonical emission order (Manual §25): CREATE_CLOCK → CREATE_GENERATED_CLOCK → CLOCK_UNCERTAINTY/LATENCY/PROPAGATED → IO delays → driving cell/load/transition → design rules → CLOCK_GROUPS → FALSE_PATH/MULTICYCLE/MIN_DELAY/MAX_DELAY. `snapshot()` returns a JSON-serializable dump.

### 5.8 `src/rca/inference/` — rule-based constraint inference (WP-F, WP-G)

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | — | Re-exports `InferenceEngine`, `RuleRegistry`, all rule modules. |
| `rules.py` | ~60 | Abstract `InferenceRule` ABC (`run(design, timing_graph, config, cset, ledger) → list[Constraint]`) and `RuleRegistry` which maps names to rule classes and supports enable/disable. |
| `clock_rules.py` | ~120 | Detects clocks from design-model clock candidates; creates `create_clock` entries with a **user-specified period if given**, otherwise emits a MISSING_CLOCK_PERIOD assumption and uses a documented fallback with CONFIDENCE.LOW. Handles active edge from the sensitivity list. |
| `reset_rules.py` | ~80 | Associates detected resets to the appropriate clock domain; does not itself emit SDC but records reset metadata and triggers warnings about missing asynchronous constraints. |
| `io_rules.py` | ~100 | Generates `set_input_delay` / `set_output_delay` for every top-level in/out port not already constrained. Uses user config when present; otherwise emits a MISSING_IO_DELAY assumption and applies a documented default (20% of clock period, max delay) with LOW confidence. |
| `generated_clock_rules.py` | ~60 | Skeleton rule for detecting clock dividers / PLL outputs (preserved for extension; leaves candidates as GENERATED_CLOCK proposals rather than confirming them). |
| `engine.py` | ~100 | `InferenceEngine.run(design, tg, config, cset, ledger)` — runs all enabled rules in a fixed order (clocks → resets → generated clocks → IO), collects produced constraints, de-duplicates by target+type, and commits them to the ConstraintSet. Applies scenario defaults when MCMM is configured. |

### 5.9 `src/rca/sdc/` — SDC import & vendor rendering (WP-H, WP-I, WP-Q)

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | — | `get_backend(name) → SDCBackend` factory. |
| `base.py` | ~80 | `SDCBackend` ABC: `render(cset, mode=BALANCED, scenario=None) → str`, `file_extension`, `dialect`, `capabilities()`. |
| `parser.py` | ~350 | **SDC importer (WP-H)**. Tokenizes SDC/Tcl lines (with brace grouping `{...}` and Tcl `[substitutions]` preserved as targets). Implements explicit flag categorization: `no_value_flags` (-max/-min/-setup/-hold/-rise/-fall/-asynchronous/-exclusive/-logically_exclusive etc.), `single_value_flags` (-name/-period/-clock/-divide_by/-multiply_by/-source/-add_delay/-reference_pin etc.), multi-value list flags (-from/-to/-through/-group. Understands `create_clock`, `create_generated_clock`, `set_input_delay`, `set_output_delay`, `set_clock_groups`, `set_false_path`, `set_multicycle_path`, `set_clock_uncertainty`, `set_clock_latency`, `set_clock_transition`, `set_propagated_clock`, `set_driving_cell`, `set_input_transition`, `set_load`, `set_max_transition`, `set_max_capacitance`, `set_max_fanout`, `set_min_delay`, `set_max_delay`. Extracts targets from Tcl `[get_ports X]`, `[get_pins X]`, `[get_clocks X]` into leaf names. Handles clock groups with multiple `-group {a b} -group {c d}`. Unknown commands are collected as warnings (not errors). Imported constraints are marked with `source_kind=IMPORT`, `confidence=HIGH`, `status=CONFIRMED`. |
| `generic/backend.py` | ~200 | Dialect-agnostic "plain" SDC emitter — produces clean, widely supported SDC suitable as a baseline and for regression diffing. Uses SI units rendered as `ns`. Renders in canonical order via `cset.emittable(mode)`. Comments are prefixed `# RCA: <explain()>`. |
| `opensta/backend.py` | ~180 | OpenSTA dialect. Uses same core structure as generic but adds OpenSTA-specific comments and supports `set_propagated_clock [all_clocks]` and unit declarations (`set_units -time ns …`). |
| `synopsys/backend.py` | ~120 | PrimeTime/DC shell dialect skeleton. Adds Synopsys-specific header and variable setup; renders `set_min_delay/set_max_delay` and `set_scaling_*` stubs for operating conditions. |
| `cadence/backend.py` | ~110 | Tempus/EDI skeleton. Adds Cadence-style header and `set_db` comments. |
| Each backend `__init__.py` | ~3 | Re-exports the backend class. |

### 5.10 `src/rca/validation/` — constraint validation (WP-J, strengthened in Step 13)

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | — | Re-exports `run_validation` (as `validate`), `ValidationResult`, `ValidationIssue`, `ValidationReport`, `CoverageReport`. |
| `base.py` | ~220 | `ValidationIssue` (severity, category, code, message, constraint_id, related ids, object names, scenario id, evidence, suggestion, blocking, source_location, plus Step-13 provenance `source_kind`/`origin`/`assumption_ids`/`resolution_status`) and `ValidationReport` with per-layer summaries and `overall_status()`. Deterministic `issue_id`. |
| `engine.py` | ~150 | `run_validation(...) → ValidationResult`. Ordered pipeline: references → semantic → conflicts → exceptions+scenarios → completeness → backend → coverage → (optional) SDC-import → hydrate provenance. |
| `references.py` | ~180 | Reference integrity (§41): every port/pin/net/cell/register/clock referenced by a constraint must exist. Distinguishes `RESOLVED` (a known miss) from `UNRESOLVED` (design unavailable). Ref-kind-consistency checks (Step 13). |
| `semantic.py` | ~560 | Constraint-type semantic checks: clocks, generated clocks, I/O timing, clock groups, path selectors, plus Step-13 value/unit/range semantics for clock uncertainty/latency/transition, input transition, load, driving cell, design rules, min/max delay. |
| `conflicts.py` | ~420 | Conflict and overlap/shadow detection (§42): duplicate/conflicting clocks, IO delays, latency/uncertainty, min/max delay, plus Step-13 precedence-aware user-vs-inference conflicts and contradictory exceptions. |
| `coverage.py` | ~661 | Coverage analyzer (§43): clock-source, input/output timing path, reg-to-reg, CDC path, and clock-relationship coverage. `UNKNOWN` when graph unavailable; `NOT_APPLICABLE` when zero applicable; retains numerator/denominator evidence. |
| `completeness.py` | ~150 | Step-13 completeness / missing-info: unresolved clock relationships, generated-clock transforms, missing IO timing, unresolved timing environment. Never invents a value. |
| `exceptions.py` | ~300 | Exception sanity (§14) + scenario coherence (§15). Step-13 records formal-verification state via `verify_exceptions`; Step-14 accepts an optional `FormalBackend`, exposes counterexamples as blocking `EXCEPTION_FORMAL_INVALID`, backend errors as blocking `EXCEPTION_VERIFICATION_ERROR`, and retains unproven evidence as `EXCEPTION_UNVERIFIED`. |
| `sdc_import.py` | ~120 | Step-13 SDC import/parse classification (§16/Req 10): consumes importer diagnostics and classifies `SYNTAX_INVALID / SEMANTIC_INVALID / INCOMPLETE / COMPLETE / UNRESOLVED` without re-parsing. |
| `backend.py` | ~50 | Backend capability (§16): preflight via the chosen `SDCBackend`; vendor syntax checks stay behind the backend abstraction. |

### 5.11 `src/rca/exceptions/` — exception effectiveness & formal (WP-K, Step 14 adapter)

| File | Purpose |
|---|---|
| `__init__.py` | Re-exports the structural, verification, and concrete adapter APIs. |
| `analyzer.py` | Classifies false paths and multicycle paths by effectiveness: *necessary* (blocks a real failing path), *useless* (does not intersect any failing path — shadowed or redundant), *harmful* (hides a real timing problem that would otherwise be caught). Hooks into the QoR loop. |
| `verifier.py` | Sole formal orchestration path. Retains UCM `scenario_ids` in the proof input, dispatches false-path/multicycle proofs through `FormalBackend`, and defaults to the conservative UNRESOLVED backend. |
| `formal_backend.py` | Vendor-neutral `FormalBackend` / `VerificationResult` contract plus conservative and deterministic mock implementations. |
| `symbiyosys.py` | **Step 14.** Concrete `SymbiYosysFormalBackend`: safely invokes explicit user-authored `.sby` jobs, requires an unambiguous SBY `PASS` plus exit code 0 for `VERIFIED`, retains job/tool/run/counterexample provenance, and otherwise stays UNRESOLVED or reports an error. |

A timing exception selector is not itself a formal property. RCA therefore never synthesizes an assertion from an SDC selector: the user-owned `.sby` collateral supplies design-specific temporal assumptions and assertions, and `formal.proofs` maps that job to the exact UCM exception ID. This keeps UCM/SDC vendor-neutral and preserves the no-fabrication invariant.

### 5.12 `src/rca/equivalence/` — semantic constraint comparison (WP-L)

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | — | Re-exports. |
| `normalize.py` | ~70 | Constraint normalization (§50): before comparing two constraints, canonicalize their target lists, clock references, waveform representation, and unit formatting. This makes semantically identical constraints compare equal even when textually different (e.g., `10000ps` vs `10ns`, `-name clk` vs positional). |
| `semantic_compare.py` | ~100 | `compare(a: ConstraintSet, b: ConstraintSet) → ComparisonResult`: returns semantic equivalence/differences plus added/removed/modified constraint lists, deterministic `scenario_differences`, and source provenance summaries separate from semantic identity. It projects `scenario_ids=[]` to all active scenarios only when both UCMs provide comparable active matrices; one-sided matrices remain UNKNOWN. Used by `rca compare` and by the optimizer to detect regressions. |

### 5.13 `src/rca/source/` — source manifest and resolution (WP-A)

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | — | Re-exports. |
| `manifest.py` | ~80 | `resolve_sources(cfg) → list[Path]`: globs/expands files from a `ProjectConfig.rtl.files` list (relative to project root). `resolve_include_dirs(cfg) → list[Path]`: computes -I include paths. `hash_sources(...)` uses `utils.hashing` for cache keys. |

### 5.14 `src/rca/config/` — typed project configuration (Enhancement)

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | 3 | Re-exports `ProjectConfig`, `load_config`, `default_config`, `write_config`, `PROJECT_SCHEMA`, `SCHEMA_VERSION`, `write_schema`. |
| `model.py` | ~340 | Pydantic v2 models: `ProjectConfig` (project, sources, constraints, analysis, flow, optimization, scenarios, MCMM, and optional `formal` sections). Step 14 adds `FormalConfig` and `FormalProofSpec` for explicit SymbiYosys job mappings; paths resolve from the project YAML. `load_config(path) → ProjectConfig` reads YAML and validates; `default_config(top)` returns a starter config (used by `rca init`); `write_config(cfg, path)` writes YAML. |
| `schema.py` | 282 | Derives a **JSON Schema** (draft 2020-12) from the Pydantic model for editor support / external validation. `PROJECT_SCHEMA` is the schema dict; `SCHEMA_VERSION` is a monotonic integer; `write_schema(path)` serializes it to disk (already written to `configs/schemas/project.schema.json`). |

### 5.15 `src/rca/eda/` — EDA backends: synthesis / STA / PPA (WP-M)

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | — | Re-exports backend registry. |
| `base.py` | ~80 | Abstract `EDABackend` ABC: `synthesize(design, cset, workdir) → SynthesizeResult`, `run_sta(design, cset, workdir, sdc_file) → TimingResult`, `name`, `capabilities()`. Also defines result dataclasses `SynthesizeResult`, `TimingResult` (wns, tns, whs, ths, worst_path, endpoints, area, power, cells, raw_log). |
| `yosys/backend.py` | ~220 | **Yosys synthesis backend.** Writes a `synth.ys` script (`read_verilog`, `hierarchy -check -top <top>`, `proc; opt; fsm; opt; memory; opt`, `stat -top …`), executes Yosys via subprocess, parses stdout to extract cell counts and estimated area. Verifies the design elaborates and reports synthesis errors as diagnostics. Requires `yosys` on PATH (verified working: 24 cells on counter.sv — 8×DFFE, 8×AND, 7×XOR, 1×NOT). |
| `opensta/backend.py` | ~180 | OpenSTA backend skeleton. Writes a `.tcl` command file (`read_verilog`, `link_design`, `read_sdc`, `report_wns`, `report_hold`, `report_power`) and invokes `sta` if available. Real STA requires a Liberty standard-cell library (not present in the sandbox); falls back to "not available" gracefully. |
| `synopsys/__init__.py` and `cadence/__init__.py` | — | Placeholder subpackages for future PrimeTime/DC and Tempus/Genus adapters. |
| `common/__init__.py` | — | Re-exports shared EDA helpers. |
| `common/mock.py` | ~120 | **Mock EDA backend** — a fast, deterministic surrogate used for CI, unit tests, and optimization smoke runs. It returns plausible timing results derived from clock period, register count, and SDC budgets (setup/hold slacks are modeled as `period - delay_estimate - budget`), and deterministically produces area/power numbers for Pareto smoke tests. It deliberately never crashes, so closed-loop tests always run. Imports guarded by `TYPE_CHECKING` to avoid a circular import with `qor.model`. |

### 5.16 `src/rca/qor/` — QoR database + Pareto (WP-N, WP-O)

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | — | Re-exports. |
| `model.py` | ~100 | `QoRResult`: per-candidate metrics (`constraint_set_id`, `wns_setup`, `tns_setup`, `wns_hold`, `tns_hold`, `area`, `dynamic_power`, `leakage_power`, `fmax`, `runtime_seconds`, `feasible`, `fail_reason`, `raw_report_path`, `scenario`). Knows how to serialize to JSON lines and deserialize. |
| `metrics.py` | ~60 | Metric aggregation helpers: `combine_scenario_results(...)`, `score(cset, qor)` scalarization for ranking. |
| `pareto.py` | ~170 | `ParetoFront`: non-dominated-sort over (setup-slack, hold-slack, -area, -power). `dominates(a,b)` returns true if a is no worse in all objectives and strictly better in at least one (using epsilon tolerance). `feasible(qr)` rejects candidates with negative WNS (setup or hold) or with constraint violations. Used by the optimizer to select the Pareto set (WP-O). Unit tested. |

### 5.17 `src/rca/optimizer/` — closed-loop multi-objective optimizer (WP-O)

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | — | Re-exports. |
| `candidate.py` | ~90 | `Candidate`: wraps a `ConstraintSet` with an `id` (C000, C001…), a `parent_id`, `mutations_applied`, `feasible`, `qr: QoRResult | None`, and a `score`. |
| `budget.py` | ~80 | `TimingBudgetAllocator`: apportions available slack across input/output delays and clock uncertainty as the optimizer tightens/loosens budgets (Manual §122 — timing-margin utilization). |
| `base.py` | ~60 | Abstract `Optimizer` ABC with `step(...)` interface. |
| `search.py` | ~220 | `ParetoSearchOptimizer`: closed-loop driver. Iterates: (1) score current candidates, (2) reject infeasible (WNS<0 or hold<0), (3) filter Pareto front, (4) mutate the best candidates (tune I/O delays, uncertainty, design rules within bounds and **only for TUNABLE constraints — FIXED/FORMAL/USER are never touched**), (5) invoke the EDA backend on each, (6) record QoR, (7) stop on `max_iterations`, `convergence` (front stable for N iterations), `no_improvement`, or `timeout`. Produces `OptimizationResult` (pareto_front, history, best, stop_reason, n_eda_runs, elapsed). |

### 5.18 `src/rca/scenarios/` — MCMM scenario helpers (WP-P)

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | — | Re-exports `build_scenarios`. Contains a helper that constructs `Scenario` objects from a `ProjectConfig`'s `scenarios:` list (func/scan/test corners with PVT derates) and wires case-analysis / disabled-path sets. |

### 5.19 `src/rca/elaboration/`

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | 9 | Placeholder/docstring. At present elaboration is performed inside `slang_adapter.py`; this package is reserved for parser-independent elaboration passes (parameter binding, generate unrolling, hierarchy flattening). |

### 5.20 `src/rca/search/`

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | — | Reserved package for future "search for existing constraints" / knowledge-base lookup (Manual §146-149). |

### 5.21 `src/rca/artifacts/` — artifact & cache manager (WP-A, WP-N)

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | — | Re-exports. |
| `manager.py` | ~140 | `ArtifactManager`: writes runs into `<results_dir>/runs/<timestamp>-<slug>/`, writes SDC files, QoR JSONL, manifests, optimization history, and UCM snapshots. Uses the deterministic hashes from `utils.hashing` to skip redundant EDA runs when inputs are identical (Manual §66, §153). |

### 5.22 `src/rca/reports/` — timing and power report parsing

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | — | Re-exports. |
| `timing.py` | ~120 | Parses OpenSTA-style timing summaries and Yosys area/cell statistics conservatively. |
| `power.py` | ~300 | Parses only OpenROAD/OpenSTA `report_power` group summaries with explicit units and one total row; returns report provenance/status for the existing QoR model and never estimates power. |

### 5.23 `src/rca/explanation/` — natural-language explainability (Manual §151)

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | — | Re-exports. |
| `generator.py` | ~120 | `ExplainGenerator.explain(cset, constraint_id=None)`: walks each constraint and generates a short paragraph describing: what it is, where it came from (user/inferred/imported), what evidence supports it, what assumptions it rests on, and (when applicable) what EDA runs validated it. The output is human-readable text and is attached as comments in generated SDC (`# RCA: …`). |

### 5.24 `src/rca/validation/` — see §5.10

### 5.25 `src/rca/web/` — FastAPI dashboard (Enhancement)

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | 3 | Re-exports `app`. |
| `app.py` | 186 | FastAPI application exposing a small HTTP API and an embedded HTML dashboard: `GET /api/design`, `GET /api/constraints`, `GET /api/validation`, `GET /api/pareto`, `GET /api/clock_domains`, plus `GET /` returns a self-contained HTML page (inline CSS, Jinja2-style rendering) showing design summary, constraint list with badges for confidence/source, validation issues, and a tiny inline-SVG Pareto scatter when optimization results exist. Served via `uvicorn` on a configurable port (default 8765) by `rca dashboard`. |

### 5.26 `src/rca/cli/` — Typer/rich command-line interface (Enhancement)

| File | Lines | Purpose |
|---|---|---|
| `__init__.py` | 2 | Re-exports `app`, `main`. |
| `main.py` | 555 | Typer application `rca` with all subcommands (see §12): `init`, `analyze`, `infer`, `generate`, `validate`, `compare`, `coverage`, `explain`, `run-sta`, `optimize`, `inspect`, `report`, `dashboard`. Each command wires the pipeline pieces together, uses Rich for tables/panels/colors, prints diagnostics, and writes artifacts. Uses helper functions `_load(cfg_path)`, `_do_parse(cfg)`, `_do_timing(cfg, design)`, `_do_inference(...)`, `_do_validate(...)` to share the wiring across commands. |

---

## 6. Config, Scripts, and Supporting Directories

### 6.1 `configs/`

| Path | Purpose |
|---|---|
| `configs/examples/.gitkeep` | Empty marker; reserved for example/template project YAMLs. |
| `configs/schemas/project.schema.json` | Auto-generated JSON Schema (282 lines of Python → JSON) describing every field of a `project.yaml`. Editors can use it for autocomplete/validation. |

### 6.2 `scripts/`

Each subdirectory contains a `.gitkeep` placeholder; intended for:

| Path | Purpose |
|---|---|
| `scripts/setup/` | Future environment/bootstrap scripts (e.g., install Yosys+OpenSTA from source). |
| `scripts/regression/` | Future regression runner (loop over designs, diff SDC golden files, break on changes). |
| `scripts/eda/` | Future EDA-launch helpers (tcl templates, Liberty symlinks, corner configs). |

### 6.3 `results/`

- `results/.gitkeep` — marker for the artifact output directory. Each `rca analyze` / `optimize` / `report` run writes into here (or into `<example>/output/` for per-example runs) a timestamped folder containing `manifest.json`, SDC files, QoR `history.jsonl`, and UCM `snapshot.json`.

---

## 7. Examples

Three included example designs cover single-clock, multi-register, and multi-clock CDC scenarios. Each example has its own `rtl/` folder and a `project.yaml`; running `rca report project.yaml` inside the example (or `make example*`) prints the full design summary.

### 7.1 `examples/simple_counter/`

- **`rtl/counter.sv`** (≈50 lines): classic 8-bit up counter with `clk`, synchronous `rst_n` (active low), count-enable `en`, output `q[7:0]`.
- **`project.yaml`**: project name `simple_counter`, top `counter`, points to `rtl/counter.sv`; specifies 10 ns clock (fixed), `en` input delay 2 ns (max), `q` output delay 2 ns (max); enables mock EDA backend; strict mode off.

Running produces 3 constraints: `create_clock` (clk 10 ns), `set_input_delay` (en), `set_output_delay` (q). 100% coverage, zero errors/warnings. Yosys synthesis verified (24 cells).

### 7.2 `examples/pipeline/`

- **`rtl/pipeline.sv`**: 2-stage pipeline: `in_data[7:0]`, `in_valid` → stage1 register → stage2 register → `out_data[7:0]`, `out_valid`. Single `clk`, async active-low `rst_n`.
- **`project.yaml`**: name `pipeline`, top `pipeline`, 8 ns clock (fixed), `in_data` input delay 1.5 ns, `in_valid` 1.0 ns, `out_data` 1.5 ns, `out_valid` 1.0 ns; optimization enabled (4 iterations, 6 EDA runs, mock backend).

Produces 5 constraints (1 clock + 2 in + 2 out). 100% coverage.

### 7.3 `examples/multi_clock/`

- **`rtl/two_clocks.sv`**: two-clock design — domain A (`clk_a`, 10 ns, `a_data[7:0]`, `a_valid`) feeds a 2-flop synchronizer into domain B (`clk_b`, 15 ns) producing `b_data[7:0]`, `b_valid`. Asynchronous reset `rst_n`. Exposes one explicit CDC edge (`a_valid → sync_valid`).
- **`project.yaml`**: two fixed clocks (10 ns, 15 ns) with an *asynchronous* `set_clock_groups` relationship marked FIXED; inputs `a_data`/`a_valid` on clk_a, outputs `b_data`/`b_valid` on clk_b.

Produces 7 constraints (2 clocks, 2 in, 2 out, 1 clock group). Correctly identifies 2 clock domains and 1 cross-domain path; the async clock-group relationship is emitted with confidence HIGH.

### Example outputs (created at runtime, ignored by git)

Each example gets an `output/` directory on demand containing e.g.
`design.generic.sdc`, `design.opensta.sdc`, `snapshot.json`, QoR JSONL, and
Pareto history. These are **not** versioned.

---

## 8. Tests

### 8.1 Layout

```
tests/
├── __init__.py
├── conftest.py                 ← adds src/ to sys.path
├── unit/
│   ├── __init__.py
│   ├── test_units.py           (7 tests)
│   ├── test_parser.py          (7 tests)
│   ├── test_constraints.py     (5 tests)
│   ├── test_sdc_parser.py      (4 tests)
│   └── test_pareto.py          (6 tests)
├── golden/__init__.py          (empty, reserved)
├── integration/__init__.py     (empty, reserved)
├── regression/__init__.py      (empty, reserved)
└── stress/__init__.py          (empty, reserved)
```

### 8.2 What is covered (28 tests total, all passing)

| Test file | Tests | Covers |
|---|---|---|
| `test_units.py` | 7 | `to_ns/ps/fs`, `parse_time` (ns/ps/fs/sec), `parse_freq` (MHz/GHz/kHz/Hz → period), period↔freq inversion, `stable_hash` determinism and collision-avoidance on small inputs, `hash_file`. |
| `test_parser.py` | 7 | Parsing `examples/simple_counter/rtl/counter.sv` with pyslang: module/port/net/register counts, clock candidate detection (`clk`), reset detection (`rst_n`), register attributes (8-bit width, async active-low), port directions, zero diagnostics on a clean design, diagnostic emission for missing files. |
| `test_constraints.py` | 5 | ConstraintSet add/query, SDC emission for generic and opensta backends (3 commands with correct targets/values), SDC import round-trip (import exported generic SDC → equivalent ConstraintSet), `emittable()` respects safe mode (LOW-confidence proposals suppressed in STRICT). |
| `test_sdc_parser.py` | 4 | Parsing a hand-written 5-command SDC (create_clock + 2 input + 2 output delays) yields correct ConstraintSet, clock period value parses numerically (10 ns), unknown command produces a warning (not crash/error), and imported constraints carry `source_kind=IMPORT` with CONFIRMED status. |
| `test_pareto.py` | 6 | `dominates` semantics, non-dominated Pareto extraction on a 3-candidate tradeoff set, infeasible (negative WNS) rejection, hold-failure rejection, Pareto-set cardinality on a known-good example, FIXED constraints preserved (not mutated) through optimizer candidate generation. |

### 8.3 Running tests

```bash
make test            # pytest tests/ -v
make test-cov        # adds coverage report
python3 -m pytest tests/ -v
```

Current state: **28 passed, 0 failed, 1 cosmetic Pydantic V2 deprecation
warning (now fixed in source by migrating to `ConfigDict`).**

### 8.4 Reserved test trees

`golden/`, `integration/`, `regression/`, `stress/` are scaffolded as Python
packages (empty `__init__.py`) to be populated with:

- **golden**: known-good SDC/UCM snapshots; CI diffs against them.
- **integration**: end-to-end runs on each example invoking Yosys where
  available and the mock backend otherwise.
- **regression**: historical-bug reproductions.
- **stress**: parameterized large designs / many clocks / many scenarios.

---

## 9. Docs

| File | Purpose |
|---|---|
| `docs/references.md` | Reference URLs per Manual §152: Synopsys TCM and white paper, Cadence Conformal CCD, OpenSTA, OpenROAD, Surelog/UHDM, slang, Yosys, and the UCSD timing-exceptions paper. |
| `docs/decisions/ADR-001-universal-constraint-model.md` | Architecture Decision Record: why the UCM exists (vendor-neutral source of truth; SDC as derived rendering; strong typing with provenance), alternatives rejected (direct SDC strings, vendor-specific models with translators), consequences. |
| `docs/decisions/ADR-002-validation-engine.md` | Architecture Decision Record: strengthen the one existing validation model rather than adding a competing Step-13 model. |
| `docs/decisions/ADR-003-symbiyosys-formal-adapter.md` | Architecture Decision Record: use explicit user-authored SymbiYosys jobs through existing formal/validation abstractions; never generate a proof property from an SDC selector. |

Placeholders for future docs:

- `docs/architecture/`, `docs/constraint-rules/`, `docs/tool-adapters/`,
  `docs/optimization/` (directories exist implicitly via the architecture
  and will be filled as the system matures).

---

## 10. Universal Constraint Model (deep dive)

The UCM is the heart of RCA. It satisfies the three central Manual
invariants:

1. **UCM is source of truth** — no subsystem ever manipulates SDC text
   directly; all read/write goes through `ConstraintSet`.
2. **Never silently invent intent** — unknown clock periods, missing I/O
   delays, etc. are recorded in the `AssumptionLedger` and emitted with
   LOW confidence / REQUIRES_CONFIRMATION status, never fabricated
   silently.
3. **Correctness over QoR** — the optimizer will never mark a failing
   design "feasible" or relax a FIXED constraint to achieve better PPA.

### 10.1 Core objects

- `ConstraintSet` — a named, versioned container. Owns monotonically
  numbered IDs (`CLK0001`, `INP0002`, `OUT0003`, `CG0004`, `FP0005`, …)
  allocated by `_next_id(prefix)`. Holds both a `constraints: dict[id,Constraint]`
  map and `scenarios`, `assumptions`, and `metadata`.
- `Constraint` — single constraint with:
  - `id`, `type`, `target_objects`, `path_selector`, `values` dict.
  - `status`: FIXED / CONFIRMED / PROPOSED / REQUIRES_CONFIRMATION /
    INFEASIBLE.
  - `opt_status`: FIXED (immutable by optimizer) / TUNABLE / REJECTED.
  - `confidence` (LOW/MEDIUM/HIGH/USER_SPECIFIED) and
    `generation_confidence` (structural / heuristic / inferred_high /
    user_specified).
  - `source_kind`: USER / INFERENCE / IMPORT / FORMAL / EDA_BACKEND /
    SCENARIO_DEFAULT.
  - `provenance`: a `ProvenanceRecord` with evidence, assumptions, who
    created it, when.
  - `disabled` flag (for shadowed/superseded constraints).
- `PathSelector` — structured `-from/-to/-through` representation; can
  serialize to SDC argument lists and is used in equivalence comparison.
- `Scenario` — MCMM corner (see §15).

### 10.2 Canonical emission order (Manual §25)

`ConstraintSet.emittable(mode)` returns constraints sorted into:

1. `create_clock`
2. `create_generated_clock`
3. `set_clock_uncertainty`
4. `set_clock_latency`
5. `set_propagated_clock`
6. `set_input_delay`
7. `set_output_delay`
8. `set_driving_cell`
9. `set_input_transition`
10. `set_load`
11. `set_max_transition`
12. `set_max_capacitance`
13. `set_max_fanout`
14. `set_clock_groups`
15. `set_false_path`
16. `set_multicycle_path`
17. `set_min_delay`
18. `set_max_delay`

This deterministic ordering is what makes textual diffs stable between
runs.

### 10.3 Safe-mode filtering (see §14)

`Constraint.is_safe_to_emit(mode)` decides whether a constraint is written
out:

| Safe mode | Emits |
|---|---|
| `STRICT` | Only CONFIRMED/FIXED/IMPORT with HIGH/USER confidence. |
| `BALANCED` | CONFIRMED + PROPOSED with at least MEDIUM confidence (default). |
| `AGGRESSIVE` | Everything except INFEASIBLE/REJECTED/DISABLED. |

---

## 11. End-to-End Data Flow

Concrete trace of `rca analyze examples/simple_counter/project.yaml`:

1. **CLI** (`cli/main.py`): calls `_load(cfg_path)` → `config.load_config()`
   → `ProjectConfig` (Pydantic validated against schema).
2. **Source resolution** (`source/manifest.py`): returns `[Path("rtl/counter.sv")]`.
3. **Parser** (`parser/slang_adapter.py`): pyslang compiles+elaborates →
   builds `Design` with 1 module (`counter`), 4 ports (clk,rst_n,en,q), 4
   nets, 1 register (`q[7:0]` on posedge clk with async negedge rst_n).
4. **Timing graph** (`timing_model/timing_graph.py`): discovers 1 clock
   candidate (clk), 1 reset (rst_n, async active-low), 1 clock domain,
   0 cross-domain edges, 1 register in that domain.
5. **Assumption ledger** created (`provenance/assumption.py`).
6. **Inference engine** runs:
   - Clock rule: finds user-specified clk@10ns, FIXED → CLK0001.
   - Reset rule: associates rst_n with clk domain.
   - IO rule: user-specified en 2ns, q 2ns → INP0002, OUT0003.
7. **Validator** (`validation/engine.py`):
   - Reference check: all targets exist → no issues.
   - Conflict check: no overlapping constraints → no issues.
   - Coverage: 100% clock, 100% in, 100% out.
8. **Artifacts** written to `output/`: SDC (generic by default), snapshot,
   manifest.
9. **Report** rendered via Rich tables to console.

`rca optimize` adds:
10. **Optimizer** seeds a baseline `Candidate` from the inferred set.
11. For each iteration it invokes the EDA backend (mock/yosys/opensta),
    collects `QoRResult`, filters feasible, updates `ParetoFront`, mutates
    TUNABLE constraints via `TimingBudgetAllocator`.
12. Stops on convergence/max-iters; writes `pareto.json`, `history.jsonl`,
    final best SDC.

---

## 12. CLI Reference

Global entry point: `rca [COMMAND] [OPTIONS] [CONFIG]`. All commands
accept `--verbose/--quiet`, `--results-dir`, and `--safe-mode {strict,balanced,aggressive}`.

| Command | Purpose | Key options |
|---|---|---|
| `rca init [DIR]` | Scaffold a new project: writes a starter `project.yaml` and `rtl/` stub. | `--top NAME`, `--name NAME` |
| `rca analyze [CONFIG]` | Full pipeline: parse → timing → infer → validate → emit SDC. The default workhorse command. | `--backend {generic,opensta,synopsys,cadence}`, `--safe-mode`, `--eda {yosys,opensta,mock}`, `--out-dir PATH` |
| `rca infer [CONFIG]` | Run inference only; prints the proposed ConstraintSet without generating SDC. | `--show-assumptions` |
| `rca generate [CONFIG]` | Emit SDC from the current UCM (skip inference if a snapshot is present). | `--backend`, `--scenario NAME` |
| `rca validate [CONFIG]` | Run the validator against the current UCM/design and print issues. | `--strict` |
| `rca compare [CONFIG] --a FILE.sdc --b FILE.sdc` | Semantically compare two SDC files through the hardened SDC importer, normalization + `semantic_compare`. Prints equivalence/UNKNOWN verdict, field- and scenario-context differences, and added/removed/modified sets; `--json` emits deterministic machine output. | `--json` |
| `rca coverage [CONFIG]` | Print only the coverage metrics (clock/in/out %). |  |
| `rca explain [CONFIG] [CONSTRAINT_ID]` | Print natural-language explanation(s) of one or all constraints (evidence, assumptions, source). |  |
| `rca run-sta [CONFIG]` | Synthesize with Yosys and/or run OpenSTA with current SDC; print timing report. | `--eda {yosys,opensta,mock}`, `--sdc FILE` |
| `rca optimize [CONFIG]` | Closed-loop multi-objective Pareto optimization. | `--backend`, `--iterations N`, `--eda-runs-per-iter N`, `--timeout SECS`, `--eda {mock,yosys,opensta}` |
| `rca inspect [CONFIG] {module,port,net,register,clock,path}` | Structured inspection sub-tables of the design model (e.g. `rca inspect project.yaml port` prints all ports). |  |
| `rca report [CONFIG]` | Human-readable design report (clocks, resets, domains, missing info, validation, constraint list) — Rich formatted. |  |
| `rca dashboard [CONFIG]` | Start the FastAPI web dashboard (uvicorn). | `--port 8765`, `--open-browser/--no-open-browser`, `--host 0.0.0.0` |

All commands return exit code `0` on success, `1` on validation errors in
strict mode, `2` on CLI/usage errors, `3` on parser/elaboration errors.

---

## 13. Writing a Project YAML

A minimal example (validated against `configs/schemas/project.schema.json`):

```yaml
project:
  name: my_design
  top: my_design
  version: "1.0"

rtl:
  files:
    - rtl/*.sv
  include_dirs:
    - rtl/include
  defines: []
  parameters: {}

clocks:
  clk:
    period: 10ns        # parses via utils.units.parse_time → 1e-8 s
    fixed: true
    edge: posedge
    source: clk

constraints:
  io:
    inputs:
      in_data:
        delay: 1.5ns
        clock: clk
    outputs:
      out_data:
        delay: 1.5ns
        clock: clk
  clock_groups: []
  false_paths: []
  multicycle_paths: []

eda:
  backend: mock         # yosys | opensta | mock
  liberty: null
  sdc_backend: generic

optimization:
  enabled: false
  max_iterations: 8
  eda_runs_per_iteration: 4
  objectives: [setup_slack, hold_slack, area, power]
  timing_budget_utilization: 0.7

validation:
  safe_mode: balanced   # strict | balanced | aggressive
  coverage_required: true

output:
  dir: output
  formats: [sdc, json]
```

All values are validated by Pydantic; unknown keys raise an error.

### Optional SymbiYosys exception verification (Step 14)

Formal verification is opt-in and preserves the conservative default. To run a
reviewed, user-authored SymbiYosys proof job when validating a particular UCM
exception, add a top-level `formal:` block:

```yaml
formal:
  backend: symbiyosys                 # default: conservative
  symbiyosys_executable: sby          # optional; RCA_SYMBIYOSYS/PATH otherwise
  work_dir: output/formal
  timeout_seconds: 300
  proofs:
    - constraint_id: FP0001           # exact UCM false-path constraint ID
      exception_kind: false_path      # false_path | multicycle
      sby_file: formal/async_fifo.sby # user-authored proof collateral
      task: async_fifo_fp             # optional SBY task
```

The `.sby` file owns the RTL/formal source list, top module, assumptions,
assertions, engines, and any mode/corner setup. RCA does **not** infer a
property from an SDC path selector. It invokes `sby -f -d <derived-run-dir>
<file> [task]` without a shell; only a `PASS` marker and exit status zero
returns `VERIFIED`. `FAIL` returns `INVALID` with preserved counterexample
artifact paths. Missing mapping/tool/file, timeout, `UNKNOWN`, or no status
marker stay `UNRESOLVED`; ambiguous/error outcomes are blocking verification
errors. Relative proof and work paths are resolved from the project YAML.

`rca validate`, `rca report`, and `rca coverage` use this configuration through
the existing validation engine. By default (`formal.backend: conservative`),
no external proof process runs and the established `EXCEPTION_UNVERIFIED`
behavior remains unchanged.

### Configured power report ingestion (Step 20)

The only supported power input is an explicitly configured
OpenROAD/OpenSTA-style `report_power` group-summary text file. It is consumed
by a completed real `yosys_opensta` flow; RCA does not add `report_power` to a
Tcl script, estimate activity, or claim to run a power engine.

```yaml
flow:
  power_reports:
    - format: openroad_report_power
      path: reports/func_slow.power.rpt
      scenario_id: FUNC_SLOW
      # producer defaults to openroad_opensta; producer_version is optional
```

The report must identify the group table with Internal, Switching, Leakage,
and Total columns, use one unambiguous final `Total` row, and declare its unit
explicitly as W/Watts, mW, uW/µW, nW, or pW. Values are normalized to watts.
Total maps to `QoRResult.power` and `power_total`; dynamic maps to
Internal + Switching only when both cells are present; leakage maps directly.
A literal zero is valid available evidence. Detailed parser classifications are
`UNKNOWN` for missing/ambiguous report content, `UNAVAILABLE` for absent files,
`MALFORMED` for structural/numeric parse failures, `INVALID` for semantic
failures, and `UNSUPPORTED` for other formats/units; none receives a fabricated
numeric total. These are `PowerParseStatus` values stored in
`raw_reports["power"]["parsing_status"]`. Canonical `QoRResult.power_status`
remains the historical `PowerStatus` vocabulary only: `AVAILABLE`,
`UNAVAILABLE`, and compatibility-only `ESTIMATED`; every non-available parser
classification is canonical `UNAVAILABLE` with all canonical power fields
`None`.

For MCMM, every mapping requires an active `scenario_id`; global fallback and
duplicate mappings are rejected. A scenario with no usable power report leaves
the global power objective unknown rather than averaging other scenarios. The
configured source path, SHA-256, format/parser version, original/normalized
unit, producer/producer version, discovered tool version, scenario/mode/corner,
and diagnostics appear under the existing
`QoRResult.raw_reports["power"]`, summary output, and existing run manifest.
Report content and identity are part of the existing flow cache key. Mock flow
always remains explicitly mock and power-unavailable. `rca run-sta`,
`rca optimize`, and `rca report` display report-derived power and provenance;
`rca report` only shows already-recorded QoR and does not run a tool.

The checked-in fixture is representative syntax for tests, not a tool run in
this repository. See `STEP20_POWER_REPORT.md` for detailed status and
validation policy.

---

## 14. Safety, Confidence, Provenance, and Invariants

### Safe modes

- **STRICT**: for production tape-out sign-off — only USER/IMPORT/HIGH
  constraints emit. Used when you want RCA to refuse to emit anything it
  cannot prove.
- **BALANCED** (default): emits CONFIRMED/PROPOSED with ≥MEDIUM
  confidence; suitable for early design exploration.
- **AGGRESSIVE**: emits everything (for debugging what the inference
  engine would produce if given free rein).

### Confidence ladder

`GenerationConfidence`:
- `USER_SPECIFIED` — explicit in `project.yaml` or pre-existing SDC.
- `INFERRED_HIGH_CONFIDENCE` — multiple structural signals agree (e.g.,
  `always_ff @(posedge clk)` + signal named `clk`).
- `INFERRED_MEDIUM_CONFIDENCE` — single strong signal.
- `INFERRED_LOW_CONFIDENCE` — heuristic/fallback; always accompanied by an
  assumption in the ledger.

### Invariants (never violated by any code path)

1. **UCM as source of truth** — SDC is rendered, never parsed back except
   via `sdc/parser.py` (which constructs fresh `Constraint` objects).
2. **No silent fabrication** — every default is logged in the
   `AssumptionLedger` and reported under "Missing Information".
3. **Correctness over QoR** — the optimizer rejects candidates that fail
   setup or hold (`feasible = False`); FIXED constraints are immutable.
4. **Determinism** — hashes are stable, emission order is canonical, IDs
   are allocated in creation order; two runs on identical inputs produce
   identical SDC text (modulo timestamps in artifacts).
5. **Fail closed, not open** — an unknown parser error aborts the run with
   a diagnostic; an unknown SDC command produces a warning and is skipped
   (never silently mis-interpreted).

---

## 15. MCMM Scenarios

The `scenarios:` section of a project YAML lets the user define multiple
corners:

```yaml
scenarios:
  func_ss:
    mode: func
    process: ss
    voltage: 0.95
    temperature: 125
    derate_setup: 1.05
    derate_hold: 0.95
  func_ff:
    mode: func
    process: ff
    voltage: 1.05
    temperature: -40
  scan:
    mode: scan
    case_analysis: {scan_en: 1}
    disabled_paths: [false_path_from_scan_to_func]
```

Each `Scenario` is stored on the `ConstraintSet.scenarios` dict. The SDC
generator can render per-scenario SDC with the appropriate `set_operating_conditions`
/ derate commands (backend-dependent). The optimizer loops over active
scenarios when evaluating a candidate, combining QoR with worst-case WNS.

When Step-14 SymbiYosys verification is configured, an exception's UCM
`scenario_ids` are retained in proof provenance. RCA does not infer a
per-corner property from that membership: the user-authored `.sby` job/task
must explicitly establish the intended mode/corner assumptions.

---

## 16. Optimization and the Pareto Loop

`rca optimize` executes the **closed-loop multi-objective optimization**
described in Manual §120–§129:

1. **Baseline** — run inference; mark USER/FIXED constraints as
   `opt_status=FIXED`.
2. **Candidate mutation** — for TUNABLE constraints (input/output delays,
   clock uncertainty, max_transition/cap/fanout), the
   `TimingBudgetAllocator` makes small bounded adjustments; new
   `Candidate` objects are spawned with unique IDs (`C001…`).
3. **EDA evaluation** — each candidate is written as SDC and passed to the
   selected EDA backend; results are parsed into `QoRResult`.
4. **Feasibility filter** — any candidate with WNS<0, hold WNS<0, or
   validation errors is marked INFEASIBLE and dropped.
5. **Pareto filter** — non-dominated sort keeps the Pareto front over
   (setup_slack, hold_slack, -area, -power).
6. **Convergence** — loop stops when the Pareto front has been stable for
   `convergence_window` iterations, or on hitting max iterations/timeout.
7. **Output** — Pareto-front SDCs, QoR history (JSONL), and a final
   `best` candidate (chosen by a scalar score with user-selectable
   weights).

All FIXED constraints (USER clocks, USER clock groups, FORMALLY_VERIFIED
exceptions) are immutable across mutations — the optimizer will never
touch them, honoring Manual §128.

---

## 17. Web Dashboard

`rca dashboard [config]` launches a FastAPI app (`rca.web.app:app`) served
by uvicorn. It exposes:

- `GET /` — single-page HTML dashboard (inline styles, no external CDN)
  with design summary, constraint cards (colored badges for
  confidence/source), validation issue table, and (when optimization has
  run) an inline SVG Pareto scatter.
- `GET /api/design` — design summary JSON.
- `GET /api/constraints` — list of UCM constraint summaries.
- `GET /api/validation` — validation issues and coverage.
- `GET /api/pareto` — Pareto front and history.
- `GET /api/clock_domains` — discovered clock domains + CDC.

Defaults: host `0.0.0.0`, port `8765`, browser opens automatically
(`--no-open-browser` to disable). Verified to start cleanly and bind to
port 8765.

---

## 18. Docker

The `Dockerfile` provides a reproducible image:

- Base: `debian:trixie-slim`.
- System packages: `python3`, `pip`, `yosys` (real synthesis available),
  plus build deps (`cmake`, `ninja-build`, `clang`, `tcl-dev`, `swig`,
  `bison`, `flex`, `git`) for optional OpenSTA compilation.
- RCA installed via `pip3 install --no-cache-dir --break-system-packages -e .`.
- A commented block shows how to build OpenSTA from source into
  `/opt/OpenSTA` and symlink `sta` onto PATH.
- `WORKDIR /work`, `EXPOSE 8765`, `ENTRYPOINT ["rca"]`, `CMD ["--help"]`.

Build & run:
```bash
docker build -t rca .
docker run --rm -v $PWD:/work -p 8765:8765 rca report project.yaml
docker run --rm -v $PWD:/work -p 8765:8765 rca dashboard project.yaml --host 0.0.0.0
```

---

## 19. Known Gaps and Roadmap

Implemented as alpha-grade:
- OpenSTA/OpenROAD: the backend adapters exist and the CLI can invoke
  `sta`, but real STA requires a Liberty cell library. In the sandbox
  `pip openroad` is a 0.0.1 stub; full STA integration needs a source
  build of OpenROAD (scripted in the Dockerfile, commented out).
- Commercial backends (Synopsys PrimeTime/DC, Cadence Tempus/Genus) emit
  correct SDC headers and dialect notes but do not yet produce all the
  tool-specific Tcl prologue/epilogue.
- Formal verification of false paths / multicycle paths has an optional
  Step-14 `SymbiYosysFormalBackend` for explicit user-authored `.sby` jobs;
  the default remains conservative UNRESOLVED. RCA intentionally does not
  generate formal properties or bundle SymbiYosys/SMT tools. Additional
  commercial formal adapters remain future work.
- Hierarchy elaboration (parameter binding, generate-block unrolling) is
  handled by pyslang already; parser-independent elaboration passes in
  `rca.elaboration` are reserved.
- Additional tests (integration, golden, regression, stress) are
  scaffolded but not yet populated.
- `scripts/setup`, `scripts/regression`, `scripts/eda` are placeholders.
- Generated-clock inference rules are conservative; they surface
  candidates rather than emitting constraints.

None of these affect the core pipeline: parse → infer → validate → emit
→ optimize → report works end-to-end on all three examples with 28/28
unit tests passing.

---

## 20. Troubleshooting / FAQ

**Q: `pyslang` fails to import?**
A: `pip install pyslang>=11.0` (binary wheels on PyPI for Linux/macOS
x86_64/arm64). If building from source, ensure a C++17 compiler and
Python dev headers are present.

**Q: `Yosys not found`?**
A: Install via apt (`apt install yosys`) or from
https://github.com/YosysHQ/yosys. Yosys is optional — RCA falls back to
the mock backend when it is missing.

**Q: OpenSTA gives "liberty not specified"?**
A: Real gate-level STA requires a standard-cell Liberty (.lib) file for
your PDK; point at it via `eda.liberty:` in `project.yaml`. Without one,
only mock-based optimization and structural analysis run.

**Q: Why does RCA emit `# RCA: ASSUMPTION …` comments in my SDC?**
A: Those mark constraints that relied on a default/fallback assumption.
Set `--safe-mode strict` to suppress them, or supply the missing data in
`project.yaml` (clock periods, I/O delays) so the assumptions are not
needed.

**Q: The optimizer mutated one of my user-specified clocks!**
A: It shouldn't. Mark clocks `fixed: true` in project.yaml (they get
`opt_status=FIXED`). If you see a mutation on a fixed constraint, file a
regression test; `test_pareto.py::test_fixed_clocks_immutable` guards
this.

**Q: Where is output written?**
A: Defaults to `<project_dir>/output/` for example-local runs, or to
`results/` in the repo root when invoked without a project-local output
dir. Every run gets a timestamped subdirectory containing the SDC, UCM
snapshot, manifest, QoR history, and Pareto JSON.

**Q: How do I add a new parser (Surelog/UHDM, …)?**
A: Subclass `rca.parser.base.ParserAdapter`, implement `parse(...)` to
return a `Design`, register it in `rca.parser.__init__`, and add a
config flag to select it.

**Q: How do I add a new SDC backend?**
A: Subclass `rca.sdc.base.SDCBackend`, implement `render(...)` and
`capabilities()`, register it in `rca.sdc.__init__.get_backend`.

---

*End of document. Generated for RCA v0.1.0.*
