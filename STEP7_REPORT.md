# Step 7 — Multi-Layer Constraint Validation Engine

This step replaces the earlier ad-hoc coverage + conflict reporting with a
proper multi-layer validation pipeline that operates **observationaly** (it
never mutates the UCM/constraint set) and emits structured, deterministic,
severity-tagged issues.

## Pipeline

The engine (`src/rca/validation/engine.py::run_validation`) runs phases in a
fixed, deterministic order:

| Order | Phase                     | Module                              | Purpose                                                        |
|------:|---------------------------|-------------------------------------|----------------------------------------------------------------|
| 1     | Reference integrity       | `validation/references.py`          | Targets, clocks, ports, nets, pins, path selectors              |
| 2     | Semantic (clocks/IO/etc.) | `validation/semantic.py`            | Clocks, generated clocks, I/O delays, clock groups, selectors   |
| 3     | Conflicts                 | `validation/conflicts.py`           | Period / IO / latency / uncertainty / min-max conflicts         |
| 4     | Exceptions + scenarios    | `validation/exceptions.py`          | False-path / multicycle sanity, scenario coherence              |
| 5     | Backend capability        | `validation/backend.py`             | Pre-flight every emittable constraint against backend caps      |
| 6     | Coverage                  | `validation/coverage.py`            | Graph-aware coverage w/ UNKNOWN when info missing               |

The resulting `ValidationResult` carries a status
(`PASS` / `PASS_WITH_WARNINGS` / `BLOCKED` / `ERROR`), full issue list,
structured coverage dict, and per-category summaries.

## Deterministic issue IDs

Every `ValidationIssue` computes an `issue_id` from the stable hash of
`(category, code, constraint_id, sorted related, sorted objects,
scenario_id, message[:160])`. Identical inputs produce identical IDs
regardless of iteration order — verified by `test_02_deterministic_issue_ids`.

## Severity & blocking policy

- `CRITICAL`, `HIGH`, `ERROR` block by default.
- `WARNING`, `MEDIUM`, `LOW`, `INFO` do not.
- Any phase may override per issue via explicit `blocking=`.
- Overall status:
  - any `CRITICAL` → `BLOCKED`
  - blocking errors present → `BLOCKED`
  - any `ERROR` or `WARNING` → `PASS_WITH_WARNINGS`
  - clean → `PASS`

## Coverage semantics (Step 7 §13 revisions)

Coverage is always computed over the **structural timing graph**
(`tg.paths`) when one is available.  When the design or timing graph
is unavailable the relevant sub-metrics are reported as `"UNKNOWN"`
rather than 0 or 100.  All percentages are computed from real
path/object counts; uncovered entries always identify a concrete
`startpoint -> endpoint` pair (or clock/object), the relevant launch
and capture clocks, and a `reason` string explaining why it is
uncovered.

Sub-metrics are separated so they cannot be conflated:

| Metric                                | Basis                                                                     |
|---------------------------------------|---------------------------------------------------------------------------|
| `clock_source_coverage_pct`           | Graph clock sources with a `create_clock` / `create_generated_clock`.     |
| `input_timing_path_coverage_pct`      | Structural `INPUT_TO_REG` + `INPUT_TO_OUTPUT` paths whose input port has a `set_input_delay` whose **`-clock` matches the structural `capture_clock`** of the path (and that clock is constrained). A `set_input_delay` referencing a different clock does NOT count as coverage for a path whose capture clock differs — the path is flagged `REQUIRES_USER_DECISION` with a clock-mismatch reason. |
| `output_timing_path_coverage_pct`     | Structural `REG_TO_OUTPUT` + `INPUT_TO_OUTPUT` paths whose output port has a `set_output_delay` whose **`-clock` matches the structural `launch_clock`** of the path (and that clock is constrained). Same clock-match rule applies; a wrong-clock delay triggers a clock-mismatch diagnostic. |
| `reg_to_reg_coverage_pct`             | Structural `REG_TO_REG` paths whose launch & capture clocks are defined and — if they differ — have an explicit relationship or path-level exception. |
| `cdc_path_coverage_pct`               | Structural `CDC`-typed paths that are explicitly handled by a **path-level** exception (`set_false_path` / `set_max_delay` / `set_min_delay` / `set_multicycle_path`) covering both clock endpoints. A `set_clock_groups -asynchronous` declaration improves *clock-relationship coverage* only — it does **not** by itself prove a CDC data path is safely constrained. Synchronizer existence is also not automatically accepted as proof of timing handling; formal/synchronizer verification metadata is a future step. |
| `clock_relationship_coverage_pct`     | Unordered clock pairs (from `domain_edges` and cross-clock paths) that have an explicit `set_clock_groups` declaration. |

**Key invariant.** `clock_relationship_coverage_pct` ≠ `cdc_path_coverage_pct`.
Relationship coverage measures whether the tool knows *that* two clocks are asynchronous/logically-exclusive/physically-exclusive; CDC-path coverage measures whether each actual structural CDC data path has an explicit, semantically appropriate timing-level handling recorded. An asynchronous clock group is necessary context but not sufficient evidence that the data path is safe.

### Coverage value vocabulary

Every coverage percentage field returns one of:

| Value              | Meaning                                                                 |
|--------------------|-------------------------------------------------------------------------|
| `"UNKNOWN"`        | Graph unavailable, or required structural information (launch/capture) is missing on the path. |
| `"NOT_APPLICABLE"` | Structural graph is available and the category genuinely contains zero applicable objects/paths (e.g. no CDC paths in the design). |
| `0.0` – `99.9`     | Applicable paths exist; the number is the percent that are covered.    |
| `100.0`            | Applicable paths exist and every one of them is covered.               |

The raw counters (`totals.cdc_paths`, `totals.reg_reg_paths`, etc.) remain
integers for downstream quantitative analysis; only the human-facing
`*_pct` fields use the `"NOT_APPLICABLE"` sentinel.

### UNKNOWN vs NOT_APPLICABLE vs 0% vs 100%

* **UNKNOWN** — the graph is unavailable (all pct fields return
  `"UNKNOWN"`) or a specific path has unresolved launch/capture
  information (that path is marked `UNKNOWN` in `uncovered` and does
  not increment the covered count).
* **NOT_APPLICABLE** — the graph is available, but the category
  genuinely has zero applicable objects/paths (e.g. no CDC paths in
  the design). The total counters are `0`/`0`; the percentage field
  carries the sentinel string `"NOT_APPLICABLE"` so it is not
  misread as "100% of CDC paths are safely handled."
* **0%** — applicable paths exist and none are covered.
* **100%** — applicable paths exist and every one of them is covered.

### Object-level vs path-level applicability

A port may have a `set_input_delay` / `set_output_delay` constraint and
still leave specific structural paths uncovered. Concretely:

* An input port with `set_input_delay -clock X` does **not** cover an
  input-to-register path whose structural capture clock is `Y ≠ X`.
* An output port with `set_output_delay -clock X` does **not** cover a
  register-to-output path whose structural launch clock is `Y ≠ X`.
* A path whose capture/launch clock is unresolved (`None`) cannot be
  claimed covered, even if the port has an I/O delay; it is classified
  `UNKNOWN`.
* Multiple delay constraints on the same port covering different clocks
  are matched per-path — the matching-clock constraint provides coverage
  for that path, and the path is not double-counted.

### Canonical TimingPath fields used

The coverage engine accesses the canonical `TimingPath` fields only:

* `p.startpoint`, `p.endpoint`
* `p.launch_clock`, `p.capture_clock` (not the stale `start_clock`/`end_clk`/`end_clock` names)
* `p.path_type` (`TimingPathClass`)

The exception sanity check module uses the same canonical names.

### Classifications

Uncovered items use the `UncoveredClassification` vocabulary:

* `UNCONSTRAINED` — path exists but has no applicable constraint.
* `REQUIRES_USER_DECISION` — cross-domain handling is missing/ambiguous.
* `REQUIRES_CONFIRMATION` — exists but needs user confirmation.
* `UNKNOWN` — launch/capture or structural information missing.
* `UNSUPPORTED` — constraint form unsupported by current analysis.
* `NOT_APPLICABLE` — path intentionally not constrained.
* `INTENTIONALLY_UNCONSTRAINED` — user explicitly marked unconstrained.

Determinism is guaranteed by sorting all path iteration by
`(startpoint, endpoint)` before emitting findings (verified by
`test_fd_coverage_deterministic_ordering`).

## Conflict/overlap taxonomy

| Classifier          | Meaning                                                         |
|---------------------|-----------------------------------------------------------------|
| DUPLICATE           | Identical semantics; harmless duplication                       |
| REDUNDANT           | Narrower constraint subsumed by a broader one                   |
| SHADOWED            | One exception masks another's paths                             |
| OVERLAPPING         | Partial overlap of targets                                      |
| CONFLICTING         | Contradictory values                                            |

Duplicate same-period `create_clock` is correctly classified as harmless
duplication, not period conflict (verified by
`test_40_duplicate_clock_same_period_is_overlap_not_conflict`).

## Exception sanity (conservative)

Exception checks are **structural** only — they flag structural red
flags (`BROAD`, `NO_EFFECT`, `SUSPICIOUS`, `BAD_CYCLES`,
`SETUP_HOLD_INCOHERENT`). The validator does NOT pronounce exceptions
"safe" — that requires formal verification (a later step).

## Backend capability

`validate_backend` runs each emittable constraint through the same
`preflight_constraint` check the SDC generator uses. This closes the
loop: the validator can never PASS constraints that the downstream
generator would silently drop.

## CLI

`rca validate` now prints a grouped severity summary, per-category
coverage percentages (or `UNKNOWN`), and a coloured issue table. It
exits with code 2 on `BLOCKED`/`ERROR`.

## Tests

- `tests/unit/test_validation.py` contains 62 unit tests covering all
  ten phases plus graph-aware coverage regressions:
  - same-clock reg->reg coverage
  - cross-clock reg->reg with/without relationship
  - missing launch/capture clock → UNKNOWN
  - clock domains with zero CDC paths (relationship coverage improves but
    CDC-path metric stays at 0/0 = 100%)
  - CDC path present, with async-group-only (NOT covered) vs explicit
    set_false_path (covered)
  - graph-aware input paths distinguishing fed vs unfed destinations
  - graph-aware output paths distinguishing fed vs unfed sources
  - port-level constraint present but path uncovered due to missing clock
  - graph unavailable → all graph metrics return "UNKNOWN"
  - deterministic uncovered-path ordering
- Full test suite: **413 passed, 0 failed**.

## Observability / immutability guarantees

- Validator never mutates constraint values, status, opt-status, or
  provenance — verified by `test_e2_observational_does_not_mutate_constraint_values`.
- Empty target collections are never silently accepted (e.g.
  single-group `set_clock_groups` errors out, empty groups error out).
- Invalid waveforms are rejected, not normalized.
- A generated clock is **not** treated as a known clock just because
  the timing model contains a node with that name.
