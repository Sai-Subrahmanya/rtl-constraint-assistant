# Step 4 — Hardened Constraint Inference & Missing-Information Workflow

This pass hardens the inference pipeline so RCA is genuinely
evidence-driven, never fabricates high-risk timing intent, and
surfaces missing/ambiguous information as structured, actionable
items instead of picking arbitrary defaults.

## What changed

### `src/rca/utils/enums.py`
Added two new enums used across the inference layer:
- `RequirementLevel` — REQUIRED / RECOMMENDED / OPTIONAL / UNSAFE_TO_INFER.
- `InferenceResultStatus` — APPLIED / PROPOSED / NO_FINDING /
  REQUIRES_CONFIRMATION / BLOCKED / UNSAFE_TO_INFER / ERROR.

### `src/rca/inference/rules.py`
Replaced the previous free-form result bag with an explicit contract:
- `ProposedConstraint` carries kind/object/values/confidence/status/
  source_kind/evidence/rationale/merge_key — everything needed to
  materialize a UCM constraint later, but no mutation of the
  ConstraintSet.
- `MissingInformation(id, category, object, severity,
  requirement_level, message, rationale, evidence,
  suggested_inputs, blocking, rule_id, possible_values)` is the
  structured record surfaced to users.
- `InferenceResult` now holds `result_status`, `proposed_constraints`,
  `missing_information`, `conflicts`, and structured evidence
  (proper `Evidence` objects). Legacy `constraints_added` /
  `add_constraint` shim kept so old code keeps working.
- `Rule` unchanged.

### `src/rca/inference/clock_rules.py` (rewritten)
- CLK-001 (structural clock candidate):
  * Establishes the clock role from `posedge/negedge` usage (HIGH).
  * **Does NOT emit `create_clock` without a known period**.
  * When period is unknown, produces a REQUIRED `clock_period`
    missing-information item and returns `BLOCKED`.
  * When period is known (USER/EXISTING_SDC/structural), proposes
    `create_clock` (and `set_clock_uncertainty` when applicable)
    with all evidence merged.
- CLK-002 (name hints):
  * LOW confidence, CORROBORATING ONLY.
  * Never creates a clock, never raises confidence to HIGH, never
    sets a period, never sets a relationship.
  * Adds ambiguity records for clock-like-named ports without
    structural evidence.
- CLK-003 (user clock spec):
  * USER precedence.
  * User clock with no structural match is preserved as USER with
    a warning that structural confirmation is absent (no silent
    fabrication, no dropping the user's data).
  * Missing period on a user clock → REQUIRED missing info.

### `src/rca/inference/reset_rules.py` (rewritten)
- RST-001 async reset detected only from structural sensitivity-list
  evidence. Recorded as a `reset_detection` proposal.
  * **No `set_false_path` / `set_clock_groups` produced.**
- RST-002 sync reset candidates flagged as REQUIRES_CONFIRMATION.
- RST-003 adversarial: reset-named signals used as data produce
  warnings, not reset records.

### `src/rca/inference/io_rules.py` (rewritten)
- NO `default_clock = next(iter(tg.clocks))` fallback. Removed.
- For every I/O port the rule:
  1. Honors explicit USER `clock` / `delay` (FIXED, USER source).
  2. Resolves structural clock association via fanout/fanin BFS over
     the structural graph (and `tg.input_clock_assoc` /
     `output_clock_assoc`).
  3. If multiple clocks are possible → REQUIRED `input_clock_
     association` / `output_clock_association` missing info with
     `possible_values` listing the candidate clocks; nothing is
     emitted.
  4. If zero clocks are found → REQUIRED missing info, nothing
     emitted.
  5. No numeric delay defaults ("20% of period"): missing delay is
     surfaced as `io_input_delay` / `io_output_delay` missing info
     with RECOMMENDED/REQUIRED level based on whether the clock is
     known.
- User delay strings are parsed via `parse_time_string` (so "2ns"
  and 2e-9 both work).

### `src/rca/inference/relationship_rules.py` (new)
- REL-001:
  * Default relationship is UNKNOWN (preserved from Step 2).
  * CDC path observation produces an UNSAFE_TO_INFER missing-info
    record — NEVER `set_clock_groups -asynchronous`.
  * Explicit user relationships (FIXED) produce
    `set_clock_groups -asynchronous` with USER provenance;
    synchronous/related relationships do not generate groups.

### `src/rca/inference/generated_clock_rules.py` (rewritten)
- GCLK-001 / GCLK-002 / GCLK-003 read the conservative candidates
  already produced by the TimingGraph and surface them as
  UNSAFE_TO_INFER missing-information / confirmation requests.
- No `create_generated_clock`, no `set_clock_groups`, no exclusivity
  assumptions are emitted automatically.

### `src/rca/inference/engine.py` (rewritten)
- Runs rules in precedence order: USER specs first, then structural,
  then heuristics, then relationship/candidates.
- Never catches rule errors silently: each rule failure produces an
  InferenceResult with status=ERROR and a warning.
- Collects results into an `InferenceReport` that contains
  per-rule status, warnings, ambiguities, conflicts, and structured
  `missing_information`. TimingGraph-level missing info is merged
  (de-duplicated by (category,object)).
- `_build_constraint` is the single place where
  `ProposedConstraint`s are turned into UCM `Constraint` objects:
  * Attaches proper `ProvenanceRecord` (rule_id, evidence list,
    created_by = originating rule(s), correct source_kind/
    confidence/status, Confidence enum, SourceKind enum).
  * Precedence: USER > EXISTING_SDC > INFERENCE; the highest
    confidence wins, but USER always wins over inference on
    conflicts.
  * Deduplicates by `merge_key` across rules; evidence from
    multiple rules is merged onto one constraint.
  * Never emits a constraint when required fields are missing
    (e.g. create_clock without period, set_input_delay without a
    resolved clock or delay).
- `_find_semantic_match` prevents duplicates when a semantically
  equivalent constraint already exists; user vs inference
  discrepancies are recorded as conflicts on the report (USER
  value retained, inference evidence attached).
- **The `default_clock = next(iter(tg.clocks.keys()))` hack has been
  removed.** I/O delays without a clock are always either
  structurally resolved or surfaced as missing information.
- Deterministic: report ordering is sorted by rule_id and by
  (category, object) for missing_information; all set-like
  iteration is sorted.

### `src/rca/cli/main.py`
- `rca infer` now prints a formatted "REQUIRED INFORMATION" table
  (id/category/object/message/blocking), plus conflicts and
  warnings, in addition to the proposed-constraint table. Required
  items are the things blocking complete SDC generation.

### `tests/unit/test_inference.py` (new, 36 tests)
36 tests covering the Step-4 acceptance matrix, including the 30
required cases and adversarial cases:

1. Clock detected structurally but unknown period → not emitted.
2. Clock-named signal used as data → no create_clock.
3. User clock overrides inference and is FIXED/USER.
4. Unknown clock period → REQUIRED missing info.
5. No guessed clock period (no magic defaults).
6. One structurally justified I/O clock (clock known + unknown cases).
7. Ambiguous input clock (multiple domains) → REQUIRED ambiguity, not emitted.
8. Unknown input clock → REQUIRED missing info.
9. NO default-first-clock fallback (multi-clock, unspecified clock).
10. Missing input delay → structured missing-info with all required fields.
11. Missing output delay → structured missing-info.
12. User I/O delay preserved (parsed, USER source).
13. User + inference → user wins, evidence merged, no silent overwrite.
14. Async reset detected from sensitivity lists; no false_path.
15. Sync reset is candidate only, not emitted.
16. Reset-named signal used as data → warning, not a reset.
17. Unknown clock relationship → missing info, no auto async.
18. Explicit async relationship → set_clock_groups USER.
19. Explicit synchronous/related → no clock_groups.
20. Generated-clock candidate not emitted automatically.
21. Gated-clock candidate not emitted automatically.
22. Clock-mux candidate not emitted automatically.
23. Ordinary data logic (`~d`) is NOT a generated clock.
24. Multiple evidence sources (user+structural+naming) merge onto a
    single constraint.
25. Every materialized constraint carries `provenance.rule_id` and
    evidence.
26. User values are USER evidence, not auto-created assumptions in
    the ledger.
27. Inference result statuses are correctly BLOCKED/PROPOSED/etc.
28. No duplicate semantic create_clock constraints.
29. Independent missing-info does not block unrelated inference
    (clock known → outputs/clock still reported; missing delay for
    one port does not hide missing clock period).
30. Inference report is deterministic across runs.
31–36. Adversarial: signal named `clk` used as data; `reset` used as
    data; two clocks same period but unknown relationship; unsupported
    event control (multiple posedge w/o clear reset) is conservative;
    data boolean (`clk & en`) feeding D input is not a gated clock.

### Unrelated systems
Structural graph, timing model (except adding USER_CONFIRMED→HIGH
which it already had), SDC parser/generator, optimizer, OpenSTA,
MCMM, dashboard, commercial backends, and the canonical UCM snapshot
from Step 3 were NOT modified. The timing graph's candidate
detectors were already conservative and were reused as-is.

## No-fabrication policy (explicit)

RCA does NOT invent, guess, or default:
- clock periods
- I/O delays
- clock relationships
- false paths
- multicycle exceptions
- generated clock divide factors / source alignment
- clock-gating intent
- clock-mux exclusivity

These are either:
1. supplied by the user (→ USER, HIGH, FIXED) or imported from
   existing SDC (→ EXISTING_SDC, HIGH), or
2. structurally justified with evidence and a single unambiguous
   choice (→ INFERENCE, HIGH/MEDIUM, CONFIRMED/PROPOSED), or
3. unknown/ambiguous → surfaced as structured `MissingInformation`
   with a RequirementLevel, blocking downstream emission only of
   what they actually block.

The engine never falls back to "first clock", "20% of period",
"same name implies synchronous", "different name implies async",
or "any `clk & x` is a gated clock".

## Precedence

For any constraint, the materialized source_kind and status honor:

    USER / EXISTING_SDC FIXED
        >
    USER / EXISTING_SDC CONFIRMED
        >
    strong structural inference (HIGH)
        >
    weak heuristic inference (LOW/MEDIUM, naming hints)

When a user value contradicts inference, the user value is kept and
the inference evidence is still attached (not dropped); the conflict
is recorded on the report.

## Dependency-integrity preservation

The canonical UCM snapshot changes from Step 3 are intact: all 192
pre-Step-4 tests continue to pass, including the strict
`repair_reverse_edges=False` default, `SnapshotFormatError` with
details, deterministic JSON, full round-trip, and assumption
invalidation.

## Test command and results

Command: `python -m pytest -q` (repo root `/home/user/rtl-constraint-assistant`).

```
collected 228 items
228 passed
0 failed
0 errors
0 skipped
```

File breakdown:

| File                                                | Tests |
|-----------------------------------------------------|-------|
| tests/golden/test_timing_golden.py                  | 6     |
| tests/unit/test_connectivity.py                     | 44    |
| tests/unit/test_constraints.py                      | 5     |
| tests/unit/test_expression_semantics.py             | 32    |
| tests/unit/test_inference.py                        | 36    |
| tests/unit/test_pareto.py                           | 6     |
| tests/unit/test_parser.py                           | 7     |
| tests/unit/test_sdc_parser.py                       | 4     |
| tests/unit/test_timing_model.py                     | 25    |
| tests/unit/test_ucm_provenance.py                   | 57    |
| tests/unit/test_units.py                            | 6     |
| **Total**                                           | **228** |

`pyslang==11.0.0` is installed in the environment; no tests are
environment-blocked.

## Remaining limitations

- The structural-graph / Slang adapter does not currently expose
  continuous-assignment (`assign x = a & b`) edges as comb edges
  (only procedural / data-fanout from registers/ports is traced in
  the current build). As a result, some gate/mux patterns that are
  expressed purely through `assign` are not picked up as gated/mux
  candidates by the TimingGraph. The inference rules are written to
  consume those candidates safely when present, and the tests for
  the no-auto-emission guarantee pass regardless. This is a
  structural-graph limitation, not an inference-policy issue — RCA
  will not invent those relationships.
- Sync-reset detection is MEDIUM confidence / candidate only. The
  Step-4 policy is conservative: we never emit timing exceptions
  from reset detection alone.
- GCLK divide-by factor and mux exclusivity remain confirmation
  items (UNSAFE_TO_INFER); no `create_generated_clock` is generated
  without user confirmation.
- I/O delay numbers are never invented. Designs that want a
  "reasonable default" must supply it via user config or later
  explicit policy knobs; the default is "ask, don't guess".

Step 4 is complete. I have NOT proceeded to SDC generation, the
optimizer, OpenSTA integration, MCMM, or commercial backends.

## Determinism / Reproducibility Fix (post Step-4 closure audit)

A cross-process reproducibility defect was identified during Step-4
closeout review: five rule files were using Python's built-in
`abs(hash(...))` to generate evidence IDs. Built-in `hash()` is seeded
randomly per interpreter process (`PYTHONHASHSEED`), so evidence IDs
differed between runs and canonical UCM snapshots were not
byte-reproducible. This has been fixed.

### Changes
- New shared helper module `src/rca/inference/_evidence.py` exposing
  `evidence_id(rule_id, kind, description, source_objects)` and
  `make_evidence(...)`. IDs are derived from `rca.utils.hashing.stable_hash`
  (SHA-256 over canonical JSON with sorted keys; source-objects tuple
  sorted), truncated to 12 hex chars and prefixed `ev_`. Timestamps,
  object addresses, PIDs, and run counters are intentionally excluded
  from evidence identity.
- All five rule files (`clock_rules.py`, `reset_rules.py`, `io_rules.py`,
  `generated_clock_rules.py`, `relationship_rules.py`) replaced their
  local `_ev`/`_evidence` helpers with thin wrappers around
  `make_evidence()`. No more `hash()` or `datetime.now()` in rules.
- `InferenceEngine.run(...)` now accepts an optional `run_ts: str | None`
  and stamps a single run-level timestamp into the ConstraintSet
  (`created_at`), all provenance records, and all normalized evidence
  records. When `run_ts` is provided, the canonical UCM snapshot is
  fully reproducible across processes.
- The engine no longer overwrites evidence IDs with
  `{kind}_{object}_ev{i}` (which leaked insertion-order); it now keeps
  the rule-computed stable IDs and deduplicates by them, sorted for
  deterministic ordering.
- Added `tests/unit/test_determinism.py`:
  * `test_cross_process_evidence_ids_and_canonical_json_identical` —
    spawns two independent Python subprocesses, parses and infers a
    small multi-clock design with fixed `run_ts` and fixed `run_id`,
    and asserts evidence IDs, constraint semantic keys, missing-info
    IDs, and canonical UCM JSON are byte-identical.
  * `test_same_evidence_same_id_in_process` — sanity that
    `make_evidence` is idempotent across timestamps, stable to
    source-object ordering, and distinguishes rule/kind/description
    changes.
  * `test_evidence_dedup_uses_stable_ids` — guards against duplicate
    evidence entries across rules.

### Test results
- Full suite: **231 passed, 0 failed** (up from 228 prior to the fix:
  the 3 new determinism tests all pass).
- Repository audit confirms zero remaining uses of built-in `hash()`
  in evidence/identity paths; the only hash primitives in the codebase
  are `rca.utils.hashing.stable_hash` (SHA-256), a legitimate
  `hashlib.md5` in file-content hashing (`hashing.hash_file`), and
  Python dict/set internals which are process-local.
