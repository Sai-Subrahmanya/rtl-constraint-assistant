# Step 9 Report — Semantic SDC Comparison / Equivalence Engine (WP-L)

This step adds a semantic equivalence/comparison engine that compares
SDC at the **UCM (Universal Constraint Model) level** rather than by
raw text. It extends `src/rca/equivalence/` in place per repository
policy (no duplicate/v2 modules).

## 1. Files modified / added

Existing files modified:

- `src/rca/utils/enums.py` — added `ComparisonLevel`,
  `ConstraintPairStatus`; extended `EquivalenceResult` with
  `EQUIVALENT_AFTER_NORMALIZATION`, `DIFFERENT`, `ERROR`; extended
  `DiffAction` with field-level diff, duplicate, redundant,
  conflicting, unknown actions.
- `src/rca/equivalence/normalize.py` — full rewrite: type-specific
  semantic field rules (`SEMANTIC_FIELDS`), unit normalization to
  SI seconds, unordered-collection sorting while preserving ordered
  `-through` stages, ordered waveform/edges/edge-shape handling,
  clock-group partition normalization, UNKNOWN detection for
  unsupported/unresolved options.
- `src/rca/equivalence/semantic_compare.py` — full rewrite:
  `ComparisonResult`, `PairResult`, `FieldDifference`,
  `DuplicateRecord` dataclasses; deterministic key-based pairing;
  multi-set / duplicate classification (DUPLICATE / REDUNDANT /
  CONFLICTING); field-level diffs; provenance-vs-semantic separation;
  scenario-aware grouping; stable deterministic output using
  `stable_hash`; `compare_sdc_text()` high-level entry point that
  drives the existing Step-5 `SdcImporter` safely (no Tcl execution);
  ERROR rollup for import failures.
- `src/rca/equivalence/__init__.py` — updated exports (added
  `compare_sdc_text`).
- `src/rca/cli/main.py` — extended the existing `compare` command to
  report status / equivalent / different / only-in-A / only-in-B /
  unknown counts, top semantic field differences, duplicates, and
  normalization notes; adds `--json` mode.

New file:

- `tests/unit/test_equivalence.py` — 62 unit tests covering all
  required categories from Step 9 §23 plus golden cases (§24),
  adversarial cases (§25), `compare_sdc_text()` integration tests,
  SDC round-trip test, and a true cross-process determinism test.

No duplicate test frameworks or v2 modules were created.

## 2. Comparison levels (Step 9 §1)

Per-pair `ComparisonLevel`:

| Level | Meaning |
|-------|---------|
| `TEXTUAL` | Raw SDC textually identical (informational; comparison operates on UCM). |
| `NORMALIZED` | Normalized forms identical (units, ordering, defaults resolved). |
| `SEMANTIC_EQUIVALENT` | Normalized semantic signatures equal; timing intent identical. |
| `SEMANTIC_DIFFERENT` | Field-level difference proven. |
| `UNKNOWN` | Cannot prove equivalence (unsupported options / unresolved targets / PARTIAL import). |

Overall `EquivalenceResult`:

| Status | Meaning |
|--------|---------|
| `EQUIVALENT` | All pairs match, provenance-equal. |
| `EQUIVALENT_AFTER_NORMALIZATION` | All pairs match but some differ in presentation/provenance. |
| `DIFFERENT` | At least one semantic difference detected. |
| `PARTIALLY_EQUIVALENT` | Some match, some differ (legacy; DIFFERENT preferred). |
| `UNKNOWN` | At least one pair cannot be classified; no differences proven. |
| `ERROR` | Importer / pipeline error. |

The pipeline: `SDC A → importer → UCM A → normalize → semantic compare`
(with an identical branch for B). Raw-text regex comparison is never
used.

## 3. Semantic normalization rules

Implemented in `normalize.py`; type-specific rules in `SEMANTIC_FIELDS`.

### 3.1 Timing quantities (Step 9 §4)

All timing-valued fields are normalized to SI seconds (float) via
`parse_time_string`/`to_seconds`. Comparison uses `math.isclose` with a
tight absolute tolerance (`1e-15` s). String forms such as `10ns`,
`10000ps`, `0.00000001s`, `2.5 ns` all compare equal. Canonical
application:

- `period` (create_clock)
- `waveform` edges
- `delay` (set_input/output_delay, set_min_delay, set_max_delay)
- `uncertainty`
- `latency` (incl. `early_latency`, `late_latency`, `source_latency`)
- `transition`
- `edge_shift` (generated clocks)

Numeric floats are rounded to 15 decimal places for stable comparison.

### 3.2 Target collection ordering (Step 9 §5)

Unordered collections are sorted deterministically before comparison:
- `target_objects`, `source_objects`, `clock_refs`
- PathSelector `from_set`, `to_set`, `from_clock`, `to_clock`
- within a single `-through` stage (OR-set semantics)

Ordered structures are **not** sorted:
- outer list of `-through` stages (subsequence semantics; `-through B
  -through C` is different from `-through C -through B`)
- generated-clock `edges` (ordered integer triple)
- `edge_shift` list
- `waveform` list
- `scenario_ids` are sorted because scenario membership is a set.

### 3.3 Path selector normalization (Step 9 §6)

`PathSelector.semantic_key()` is reused; it sorts the unordered parts
(from/to/clocks/ref sets, objects within each through stage) while
keeping the ordered list of through stages and preserving qualifiers:
`edge`, `min_max`, `setup_hold`, `add_delay`, `reset_path`,
`from_clock`, `to_clock`, `through_clock`, `scenario`.

### 3.4 Default / explicit equivalence

Per-type defaults are applied before comparison. Examples:
- `set_input_delay -max` (default) ≡ explicit `-max`; `-min` ≠ `-max`.
- `set_multicycle_path -setup` (default) ≡ explicit `-setup`;
  `-hold` is different.
- `create_clock -add` defaults to False; `-add` being absent is
  equivalent to explicit `-add` False.
- `set_propagated_clock` identity is purely its clock set.

### 3.5 Clock groups (Step 9 §11)

Clock groups form a partition. Normalization: sort clocks within each
group, then sort groups themselves. Result: `{A B} {C}` ≡ `{C} {A B}`
(order of groups and order inside a group don't matter), but `{A} {B}
{C}` ≠ `{A B} {C}` (different partition).

### 3.6 Exceptions (Step 9 §12)

`set_false_path`, `set_multicycle_path`, `set_min_delay`, `set_max_delay`:
full path selector comparison (ordered through, sorted from/to), plus
qualifiers (`min_max`, `setup_hold`, `add_delay`, `reset_path`, `edge`).
A broad false-path is not equivalent to a narrow one. Multicycle
`cycles` must match exactly (integer); `divide_by=2` is not treated as
equivalent to `multiply_by=0.5` (unsafe cross-conversion).

### 3.7 Created/generated clocks (Step 9 §8, §9)

`create_clock`: name (identity), period, waveform, sources/targets,
`add` flag. Clocks with different names are never equivalent — even if
periods match — unless the caller explicitly aliases them (not
attempted here; safe default). `create_generated_clock`: name, source
pin, master clock, divisor/multiplier (integer-strict), edges,
edge_shift, invert, duty_cycle, combinational, add. Divide and multiply
are compared independently; cross-conversion is conservatively NOT
applied (UNKNOWN is preferred over unsafe equivalence).

### 3.8 I/O delays (Step 9 §10)

`set_input_delay`/`set_output_delay`: compare delay (s), associated
clock, targets, `min_max`, `edge` (rise/fall/both), `add_delay`,
`clock_fall`. `-min 1 data` ≠ `-max 1 data` even with identical numeric
value.

## 4. Duplicate / multi-set semantics (Step 9 §13, §18)

Within each side, constraints sharing a semantic match key are grouped
and classified:

- **DUPLICATE** — identical normalized signatures.
- **CONFLICTING** — same scalar identity (e.g. same clock name) but
  different semantic values (e.g. two create_clock clk with different
  periods).
- **REDUNDANT** — overlapping but not identical (e.g. broad + narrow
  selector on the same endpoint).

Duplicates are reported separately and do not cause spurious
"only_in_A" counts.

## 5. Pairing algorithm (Step 9 §17 + correction #3/#5)

1. Build deterministic groups on each side using `semantic_match_key`
   `(type, scenarios, identity_keys, target/clock/from/to sets)`.
2. Detect side-internal duplicates/conflicts (DUPLICATE/REDUNDANT/
   CONFLICTING).
3. For each key, run a multiset pairing function `_pair_multiset` that
   matches in decreasing strength:
   a. **Exact normalized signature** (greedy one-to-one). This is the
      strongest signal and handles multiplicities correctly.
   b. **Unambiguous identity-sig match** within the same match-key
      group. The identity signature includes scalar identity fields
      (clock name, source, master, min/max, edge, partition, path
      selector key, setup/hold). If more than one candidate has the
      same identity sig, we do NOT pair — pairing would be ambiguous.
   c. **Single-remainder fallback**: if exactly one unmatched
      constraint remains on each side of the key group, pair them to
      produce a field-level diff.
   d. Otherwise, unmatched constraints are classified as
      `ONLY_IN_LEFT` / `ONLY_IN_RIGHT`. We never force-pair two
      unrelated constraints just to produce a diff.
4. Field differences are produced only for pairs that pass step 3a,
   3b, or 3c. `ONLY_IN_LEFT` / `ONLY_IN_RIGHT` are used when
   correspondence cannot be safely established.
5. All output lists are sorted via `stable_hash` so results are
   deterministic across runs and across processes (Step 9 §27). We
   never use Python's built-in `hash()`.

## 5a. `compare_sdc_text()` — high-level text comparison (correction #1)

`compare_sdc_text(a_text, b_text, *, importer=None, design=None,
tg=None, source_a="<a>", source_b="<b>")`:

1. Load the existing Step-5 `SdcImporter` lazily.
2. If `importer` is supplied it is reused; otherwise a fresh
   `SdcImporter(design=design, tg=tg)` is constructed per side.
3. Call `imp.from_text()` on each SDC string. This is a pure
   lexer/parser/normalizer; **no Tcl is executed, no shell commands
   invoked, no `eval`/`source`/`exec` allowed** (the importer's
   `_FORBIDDEN_COMMANDS` set rejects those commands with a SECURITY
   diagnostic).
4. Import failures (lexer/parser ERROR diagnostics, caught exceptions)
   produce a `ComparisonResult` with `overall_status == ERROR` and
   diagnostic messages; raw exceptions are not propagated.
5. Successful imports hand the two UCM ConstraintSets to `compare()`
   and the result is merged (diagnostics, normalization notes,
   duplicates, pair lists).

UNKNOWN vs ERROR (correction #8):
- **ERROR**: malformed SDC, import failure, importer unavailable.
- **UNKNOWN**: import succeeded but semantic equivalence cannot be
  proven (unresolved collections, unsupported options preserved on
  the constraint, PARTIAL imports).

## 6. Provenance vs semantic equality (Step 9 §19)

Provenance fields (`source_kind`, `provenance`, `confidence`, `status`,
`opt_status`, `comment`, `generated_text_by_backend`, ids, dependency
edges, evidence/assumption ids) are excluded from the semantic
signature. A pair whose provenance differs but whose semantics match
is reported as `EQUIVALENT_AFTER_NORMALIZATION` and each pair record
exposes `a_source_kind`, `b_source_kind`, and `provenance_equal`.

## 7. Scenario-aware comparison (Step 9 §20)

`scenario_ids` are sorted and included in the semantic match key.
Constraints in different scenarios never pair as equivalent. For
example, a constraint in `func` and one in `scan` with otherwise
identical fields are reported as DIFFERENT (only_in_left/only_in_right
within their scenario buckets).

## 8. UNKNOWN policy (Step 9 §15)

Equivalence is **never** inferred from an absence of differences.
`UNKNOWN` is returned for a pair when:
- the constraint type is in `_FALLBACK_UNKNOWN` (e.g.
  `set_case_analysis`, `set_disable_timing`, design-rule constraints
  for which we have not published equivalence rules);
- any typed `TargetRef` has `CollectionKind.UNRESOLVED` or `EXPR`
  (unresolved collection, `[get_pins …]` containing an expression);
- the constraint's import status is `PARTIAL` or `UNRESOLVED`.

Formal INVALID-equivalent behavior: we never claim equivalence when
the model cannot support it.

## 9. Difference report (Step 9 §14)

Each different pair carries a list of `FieldDifference` entries:
`field`, `value_a`, `value_b`, `explanation`. Examples emitted by the
CLI:

```
- [create_clock] CLK1 → CLK1
    period: A=1e-08  B=8e-09
      numeric timing value 'period' differs after unit normalization.
- [set_false_path] FP1 → FP1
    path_selector: ...
      from/to/through selectors differ (order preserved for through stages).
```

## 10. CLI summary

`rca compare <config> --a a.sdc --b b.sdc [--json]` prints:

```
SDC COMPARISON
Status: DIFFERENT
Equivalent: N   Different: M   Only in A: X   Only in B: Y   Unknown: U
Duplicates in A: P   Duplicates in B: Q
<top semantic field differences>
<only-in-A, only-in-B, unknown sections>
```

`--json` emits `ComparisonResult.to_dict()` for machine consumption.

## 11. Cross-backend comparison (Step 9 §22)

Because comparison runs on the UCM, backend-specific rendering
differences (vendor dialects, ordering, whitespace) are normalized
away; only UCM-level semantic differences are reported. No backend is
invoked during comparison and no Tcl is executed (Step 9 §26).

## 12. Tests added / modified

New file `tests/unit/test_equivalence.py` — 62 tests.

Required Step 9 §23 matrix:

1. identical SDC (`test_01`)
2. whitespace/ordering — represented as UCM construction order
   differences (`test_03`, `test_06`)
3. command-order (`test_03`)
4. unit differences (`test_04`, golden_B)
5. numeric formatting (`test_05`)
6. equivalent target ordering (`test_02`, `test_06`)
7. equivalent unordered collections (`test_07`)
8. different clock period (`test_08`, golden_C)
9. different waveform (`test_09`)
10. different clock identity (`test_10`)
11. input-delay min/max difference (`test_11`)
12. rise/fall difference (`test_12`)
13. add_delay difference (`test_13`)
14. clock-group partition difference (`test_14`, golden_D)
15. ordered through difference (`test_15`, adv_same_through_reversed_order)
16. false-path scope difference (`test_16`, golden_E)
17. multicycle count difference (`test_17`, adv_exception_same_target_different_cycles)
18. multicycle setup/hold difference (`test_18`)
19. generated-clock divide/multiply difference (`test_19`)
20. unsupported option → UNKNOWN (`test_20`, golden_F)
21. unresolved target → UNKNOWN (`test_21`)
22. duplicate multiplicity (`test_22`, golden_G)
23. provenance difference but semantic equality (`test_23`,
    adv_different_provenance_same_semantics)
24. scenario-aware equality (`test_24`)
25. scenario mismatch (`test_25`)
26. deterministic comparison (`test_26`)
27. cross-process deterministic comparison (`test_27_cross_process_determinism_subprocess`): launches two independent Python interpreter processes via `subprocess`; each builds the same UCM pair, calls `compare()`, prints the SHA-256 `stable_hash` of `ComparisonResult.to_dict()`. Digests must be equal across processes.

Required Step-9 correction #6 (compare_sdc_text() tests with real importer):
- `test_sdc_A_identical_strings_equivalent`
- `test_sdc_B_equivalent_unit_differences` (10ns vs 10000ps)
- `test_sdc_C_different_period`
- `test_sdc_D_different_io_delay`
- `test_sdc_E_unsupported_causes_unknown`
- `test_sdc_F_malformed_sdc_causes_error` (structured ERROR, not UNKNOWN)
- `test_sdc_G_provenance_does_not_affect_equality`
- `test_sdc_H_design_context_not_required_for_basic`

Additional correction-pass tests:
- `test_pairing_min_max_does_not_falsely_pair` — min/max I/O delays pair correctly by identity
- `test_pairing_ambiguous_candidates_not_force_paired` — identity sig pairs p0↔p0 and p1↔p1 across list ordering
- `test_sdc_roundtrip_generate_import_compare` — import → UCM → generic renderer → re-import → clocks remain semantically equivalent

Golden cases (Step 9 §24) `test_golden_A`–`test_golden_G`.

Adversarial cases (Step 9 §25): same-value-different-clock,
same-objects-different-min/max, reversed through, different
partition, same generated-clock target different master, same
exception target different cycle count, same-provenance-different-
semantics, different-provenance-same-semantics.

Additional coverage: latency / transition / propagated_clock /
min_delay / max_delay equivalence and differences; within-stage OR
equivalence for `-through {B D}` vs `{D B}`; JSON-serializable
report; deterministic signature tuples.

## 13. Test execution

Executed in sandbox (other failures remain the pre-existing
`pyslang is not installed` RuntimeError, unrelated to Step 9):

```
$ python -m pytest tests/unit/test_equivalence.py -q
============================== 62 passed in 0.99s ==============================

$ python -m pytest tests/unit/test_exceptions.py tests/unit/test_equivalence.py \
      tests/unit/test_constraints.py tests/unit/test_sdc_parser.py \
      tests/unit/test_pareto.py tests/unit/test_units.py \
      tests/unit/test_validation.py tests/unit/test_sdc_generation.py \
      tests/unit/test_ucm_provenance.py -q
============================= 316 passed in 1.09s =============================
```

Full-suite counts (sandbox, pyslang-related suites skipped due to env):

- **collected**: 487 (non-pyslang core: 316; pyslang-dependent remainder in
  parser/timing/inference/connectivity/expression-sdetherminism suites
  fail with pre-existing `RuntimeError: pyslang is not installed`)
- **passed (Step 9 + dependent core)**: 316
- **failed (Step 9 related)**: 0
- **skipped**: 0
- **errors (Step 9 related)**: 0
- **dependency-blocked**: ~171 tests in parser/timing/inference/
  connectivity/expression_semantics/partial determinism modules due
  to missing `pyslang`; not regressions from Step 9.

Cross-process determinism: **PASSED** (subprocess test
`test_27_cross_process_determinism_subprocess` launches two
interpreters and asserts equal SHA-256 digests).

## 14. Known limitations / deliberate UNKNOWN

- `set_case_analysis`, `set_disable_timing`, design-rule constraints
  (`set_driving_cell`, `set_load`, `set_input_transition`,
  `set_max_transition/capacitance/fanout`) are not yet documented with
  per-type equivalence rules; pairs involving them are reported
  UNKNOWN to avoid unsafe equivalence claims.
- Target collections containing `[get_*]` expressions or unresolved
  names force UNKNOWN (we cannot prove the sets are equal without a
  resolved design).
- Clock identity aliasing (e.g. treating two different clock names as
  equivalent via an explicit rename mapping) is not performed; use
  the UCM directly to rename/alias clocks before comparing if needed.
- Generated clock `divide_by=2` is not auto-equated to
  `multiply_by=0.5`; multiply_by is required to be integral in SDC
  semantics, so cross-conversion would be unsafe.
- Backend-specific semantic quirks (e.g. vendor-specific default
  waveform edges beyond the UCM's representation) are not modeled;
  they surface as UNKNOWN via the import status `PARTIAL` mechanism.
- The comparison engine is non-executable: it consumes only UCM
  ConstraintSets; no Tcl evaluation or external tools are invoked.
