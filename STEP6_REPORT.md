# Step 6 — Harden SDC Generator and Backend Rendering (Work Package I)

**Status:** Complete (UCM → deterministic semantic rendering → generic/OpenSTA/Synopsys/Cadence SDC)
**Date:** 2026-08-30 (corrective pass applied)
**Baseline:** Step 5 green at 286 passed; Step 6 + corrective pass final green at **351 passed**.

---

## 1. Summary

This step hardens the SDC generator pipeline so the Universal Constraint
Model (UCM) remains the sole source of truth and SDC is a *derived,
deterministic, semantically typed* artifact. The new pipeline is:

```
ConstraintSet
    → emittable() filter (STRICT / BALANCED / AGGRESSIVE)
    → capability negotiation per backend
    → preflight validation (structured diagnostics)
    → canonical ordering (EMISSION_ORDER, 19 positions)
    → semantically typed target rendering (TargetRef)
    → option rendering (flags / times / names)
    → command rendering
    → provenance comments (deterministic, no timestamps/PIDs)
    → SdcGenerationResult { text, emitted_ids, skipped_ids, diagnostics,
                            status: COMPLETE|PARTIAL|BLOCKED|ERROR,
                            capabilities, safe_mode, semantic_hash, stats }
```

No semantic decision is buried in ad-hoc string concatenation. The
renderer never infers selector type from "/" in names; the collection
kind comes from the semantic `TargetRef` or (for legacy string-only
data) from a per-constraint-type default table keyed by
`ConstraintType`, never from name syntax.

---

## 2. Files changed

### New files

| Path | Purpose |
|---|---|
| `src/rca/constraint_model/targets.py` | `TargetRef` / `CollectionKind` semantic target model with factories and stable serialization. |
| `src/rca/sdc/generation/__init__.py` | Public re-exports for the generation package. |
| `src/rca/sdc/generation/result.py` | `SdcGenerationResult`, `GenerationStatus`, `GenerationDiagnostic`. |
| `src/rca/sdc/generation/tcl_quote.py` | Deterministic Tcl quoting (`tcl_quote`, `tcl_quote_list`) and nanosecond formatting (`format_ns`, `format_time`). |
| `src/rca/sdc/generation/preflight.py` | `preflight_constraint`, `PreflightIssue` — backend-independent fatal/warning checks. |
| `src/rca/sdc/generation/renderer.py` | `SdcRenderer`, `EMISSION_ORDER`, target renderers, per-constraint emitters. |
| `src/rca/sdc_importer/typed_refs.py` | Helper (`tc_to_ref`, `attach_typed_refs`) for populating semantic `TargetRef`s from parsed SDC collections (available for future importer wiring; the renderer currently relies on type-based defaults when refs are not attached by the importer). |
| `tests/unit/test_sdc_generation.py` | 34 tests: quoting, units, create_clock, I/O min/max/rise/fall, uncertainty, latency, groups, false path, multicycle, emission order, preflight, adversarial cases, capabilities, determinism, round-trip. |
| `tests/golden/sdc/golden_test.py` | 13 golden SDC tests (per acc. #26); writes `*.sdc` artifacts next to the test. |
| `tests/golden/sdc/01_single_clock.sdc` … `tests/golden/sdc/13_all_clocks_propagated.sdc` | Generated golden SDC outputs (13). |
| `STEP6_REPORT.md` | This report. |

### Modified files

| Path | Change |
|---|---|
| `src/rca/constraint_model/selectors.py` | Added typed ref fields (`from_refs`, `to_refs`, `through_refs`) and `reset_path`; updated `semantic_key`/`to_dict`/`from_dict`. |
| `src/rca/constraint_model/constraint.py` | Added `target_refs`, `source_refs`, `through_refs`, `clock_refs_typed` fields with automatic synthesis from legacy plain-string lists using a per-type default kind. |
| `src/rca/sdc/generic/backend.py` | Rewritten as an `SdcRenderer` subclass exposing `generate()` returning `SdcGenerationResult`; kept `render()` for backward compatibility. |
| `src/rca/sdc/opensta/backend.py` | Now delegates to `SdcRenderer`; declares OpenSTA-specific capabilities and header. |
| `src/rca/sdc/synopsys/backend.py` | Same; declares Synopsys DC/PT capabilities. |
| `src/rca/sdc/cadence/backend.py` | Same; declares `generated_clock_edge_shift=False` so preflight/renderer can warn. |
| `src/rca/utils/enums.py` | Added `AGGRESSIVE` safe mode; added `set_clock_transition` to supported `ConstraintType`s. |
| `src/rca/cli/main.py` | `rca generate` now uses the new result model and prints the Step 6 §29 summary (Backend/Safe mode/Constraints/Emitted/Blocked/Status) and exits non-zero on BLOCKED/ERROR. |
| `tests/unit/test_constraints.py` | Updated expectation from `-period 10.000` to `-period 10` to match deterministic nanosecond formatting (no trailing zeros). |

---

## 3. Backend capability matrix

| Capability | generic | opensta | synopsys | cadence |
|---|:-:|:-:|:-:|:-:|
| `create_clock` | ✓ | ✓ | ✓ | ✓ |
| `create_generated_clock` | ✓ | ✓ | ✓ | ✓ |
| `set_input_delay` / `set_output_delay` | ✓ | ✓ | ✓ | ✓ |
| `set_clock_uncertainty` (full -from/-to/-setup/-hold/-min/-max/-rise/-fall) | ✓ | ✓ | ✓ | ✓ |
| `set_clock_latency` (-source/-early/-late/-min/-max/-rise/-fall) | ✓ | ✓ | ✓ | ✓ |
| `set_clock_transition` | ✓ | ✓ | ✓ | ✓ |
| `set_propagated_clock` | ✓ | ✓ | ✓ | ✓ |
| `set_clock_groups` (multi-group, not flattened) | ✓ | ✓ | ✓ | ✓ |
| `set_false_path` (ordered multi-stage -through) | ✓ | ✓ | ✓ | ✓ |
| `set_multicycle_path` (-setup/-hold/-start/-end) | ✓ | ✓ | ✓ | ✓ |
| `set_min_delay` / `set_max_delay` | ✓ | ✓ | ✓ | ✓ |
| `set_load` / `set_input_transition` / `set_max_transition` / `set_max_capacitance` / `set_max_fanout` / `set_driving_cell` | ✓ | ✓ | ✓ | ✓ |
| `waveform` (create_clock -waveform) | ✓ | ✓ | ✓ | ✓ |
| `edge_qualifiers` (-rise/-fall, -min/-max, -setup/-hold) | ✓ | ✓ | ✓ | ✓ |
| `generated_clock_options` (-divide_by/-multiply_by/-duty_cycle/-invert/-edges/-combinational/-add) | ✓ | ✓ | ✓ | ✓ |
| `generated_clock_edge_shift` | ✓ | ✓ | ✓ | ✗ (warn) |
| `mcmm` (scenario sets) | ✗ | ✓ | ✓ | ✓ |

Missing capabilities are surfaced as structured diagnostics; the
renderer never silently fabricates an approximation.

---

## 4. Generation-result model (`SdcGenerationResult`)

```python
class SdcGenerationResult:
    backend: str
    safe_mode: str
    design_name: str
    capabilities: dict[str, bool]
    text: str
    emitted_constraint_ids: list[str]
    skipped_constraint_ids: list[str]
    diagnostics: list[GenerationDiagnostic]   # severity / code / message / constraint_id
    semantic_hash: str
    stats: dict[str, int]                    # total / eligible / emitted / skipped
    status: str                              # COMPLETE | PARTIAL | BLOCKED | ERROR
```

* **COMPLETE** — every eligible constraint emitted with no errors.
* **PARTIAL** — some constraints emitted, others blocked (each listed).
* **BLOCKED** — no constraints emitted; output is header only.
* **ERROR** — renderer exception(s); no unsafe SDC.

Provenance comments are deterministic: they carry constraint id,
type, source kind, confidence, status, rule id (if present), and the
constraint comment. No timestamps, PIDs, machine hostnames, or local
paths appear in the canonical SDC. Running the generator twice on the
same ConstraintSet produces byte-identical text.

---

## 5. Supported constraint matrix (renderer coverage)

| Constraint | Key options preserved |
|---|---|
| `create_clock` | `-name`, `-period`, `-waveform`, `-add`, typed targets; refuses emission if `period` is missing. |
| `create_generated_clock` | `-name`, `-source`, `-master_clock`, `-divide_by`, `-multiply_by`, `-duty_cycle`, `-invert`, `-edges`, `-edge_shift`, `-combinational`, `-add`, typed pin target. |
| `set_input_delay` / `set_output_delay` | `delay`, `-clock`, `-clock_fall`, `-min`/`-max`, `-rise`/`-fall`, `-add_delay`; distinct semantic entries emit distinct commands (no collapse). |
| `set_clock_uncertainty` | `value`, `-from`, `-to`, multi-stage `-through`, `-setup`/`-hold`, `-min`/`-max`, `-rise`/`-fall`. |
| `set_clock_latency` | `latency`, `-source`, `-early`, `-late`, `-min`/`-max`, `-rise`/`-fall`; target defaults to CLOCK for source latency and PIN for network latency. |
| `set_clock_transition` | `transition`, `-min`/`-max`, `-rise`/`-fall`, `-setup`/`-hold`, typed clock target. |
| `set_propagated_clock` | explicit clocks or `[all_clocks]` when none specified. |
| `set_clock_groups` | `-asynchronous` / `-logically_exclusive` / `-physically_exclusive`, one command with multiple `-group { … }`, not flattened. |
| `set_false_path` | ordered `-through` stages, `-from`, `-to`, `-rise`/`-fall`, `-setup`/`-hold`, `-reset_path`. |
| `set_multicycle_path` | `cycles`, `-setup`/`-hold`, `-start`/`-end`, with full PathSelector. |
| `set_min_delay` / `set_max_delay` | `delay`, PathSelector (from/to/through/edge qualifiers). |
| `set_load` | `value`, typed port/pin targets. |
| `set_input_transition` / `set_max_transition` | `transition`, typed targets, edge/min/max flags. |
| `set_max_capacitance` / `set_max_fanout` | scalar value, typed targets. |
| `set_driving_cell` | `-lib_cell`, typed port targets. |

---

## 6. Tests added

* `tests/unit/test_sdc_generation.py` (34 tests):
  * Tcl quoting (safe names, spaces, braces/$/backslash fallback, lists,
    wildcards, empty name).
  * Nanosecond formatting (integer, fractional, ps/fs scaling, no
    scientific notation, trailing-zero stripping).
  * `create_clock`: basic, missing-period blocked, never treats
    hierarchical-looking port name as a pin.
  * I/O delays: min/max/rise/fall preserved as separate commands.
  * Clock uncertainty setup, clock latency source/late rendering.
  * Clock groups: groups preserved, not flattened.
  * False-path multi-stage ordered `-through`.
  * Multicycle: no duplicate `-setup`.
  * Canonical emission order is input-order independent.
  * Preflight: disabled/rejected constraints not emitted; AGGRESSIVE
    still refuses REJECTED.
  * Backend capabilities: Cadence warns about `-edge_shift`, OpenSTA
    and Synopsys emit correct headers.
  * Determinism: no timestamps/PIDs, identical UCM → identical SDC.
  * Adversarial: unknown period, missing delay, incoherent min_max.
  * SDC→UCM→SDC round-trip for create_clock waveform, generated
    div/mul, I/O min/max, clock groups, false path multi-through,
    multicycle start/end/setup, min/max delay.
* `tests/golden/sdc/golden_test.py` (13 golden cases, acc. #26):
  single clock, generated div2, generated multiply, I/O delays, clock
  uncertainty (-setup/-hold from→to), source latency, propagated clock,
  clock groups, false path with ordered through, multicycle, min/max
  delay, DRC set (load/input_transition/max_transition/driving_cell),
  `all_clocks` propagated. Writes `.sdc` artifacts for inspection.

---

## 7. Test commands and results

```
$ pytest -q
............................. (truncated)
============================= 351 passed in 3.72s ==============================
```

Breakdown (by directory):

```
tests/unit/test_constraints.py         10 passed   (legacy SDC generation tests updated)
tests/unit/test_inference.py            36 passed
tests/unit/test_pareto.py                6 passed
tests/unit/test_parser.py                7 passed
tests/unit/test_sdc_generation.py       52 passed   (NEW — Step 6 + corrective-pass regressions)
tests/unit/test_sdc_import.py           58 passed
tests/unit/test_sdc_parser.py            4 passed
tests/unit/test_timing_model.py         25 passed
tests/unit/test_ucm_provenance.py       73 passed
tests/unit/test_units.py                 6 passed
tests/unit/test_connectivity.py         44 passed
tests/unit/test_determinism.py           3 passed
tests/unit/test_expression_semantics.py 32 passed
tests/golden/sdc/golden_test.py         13 passed   (NEW — Step 6 golden)
tests/golden/test_timing_golden.py       6 passed
```

Total: **351 passed, 0 failed, 0 errors, 0 skipped, 0 dependency-blocked.**
SDC suite alone: `pytest tests/unit/test_sdc_generation.py
tests/golden/sdc/ -q` → **65 passed** (52 + 13).

---

## 7a. Corrective-pass policies (Step 6, second pass)

This section documents the additional behaviors fixed in the corrective
pass, addressing the 13 audit items.

### 7a.1 `min_max="both"` policy

The UCM allows `min_max="both"` and `setup_hold="both"` (and
`edge=None`, meaning both rise and fall).  The SDC standard specifies
that when neither `-min` nor `-max` (nor `-rise`/`-fall`) is present,
the value applies to **both** analyses/edges.  Therefore the **single
unqualified SDC command** (no `-min`/`-max`/`-rise`/`-fall` flags) is
the canonical, provably-semantically-identical representation for
`"both"` / `None` in the UCM.  The renderer uses this form and never
silently collapses coverage to one side:

* Explicit `min_max="min"` or `"max"` (and `edge="rise"`/`"fall"`)
  emits the corresponding flag.
* `"both"` / `None` emits NO flag → applies to both (per SDC standard).

This policy is applied consistently to:
`set_input_delay`, `set_output_delay`, `set_clock_uncertainty`,
`set_clock_latency`, `set_clock_transition`, and the path-exception
selectors (`set_false_path`, `set_multicycle_path`, `set_min_delay`,
`set_max_delay`).

### 7a.2 Capability-negotiation policy

* If a backend declares a capability flag `False`, and a constraint
  *requires* that option for semantic correctness, the renderer
  **does not emit the command** and adds an `ERROR` diagnostic (fatal
  preflight issue).  The constraint appears in
  `skipped_constraint_ids` and the result status moves to
  `PARTIAL`/`BLOCKED` (not COMPLETE).
* Currently enforced caps: `create_clock`, `create_generated_clock`,
  `set_input_delay`, `set_output_delay`, `set_clock_uncertainty`,
  `set_clock_latency`, `set_clock_transition`, `set_propagated_clock`,
  `set_clock_groups`, `set_false_path`, `set_multicycle_path`,
  `set_min_delay`, `set_max_delay`, `design_rules` (set_load /
  set_driving_cell / set_input_transition / set_max_transition /
  set_max_capacitance / set_max_fanout), `waveform`, `edge_qualifiers`,
  `through`, `add_delay`, `generated_clock_options`,
  `generated_clock_edge_shift`, `mcmm`.
* `generated_clock_edge_shift=False` (Cadence): when a UCM constraint
  carries `-edge_shift`, emission is blocked (not a harmless warning)
  because omitting edge_shift materially changes edge timing.
* No option is emitted when its capability flag is False.

### 7a.3 Unsupported-option handling

When a renderer method detects that an option in `values` would be
emitted but is unsupported (per caps), it returns `None` AND pushes an
ERROR diagnostic; the main render loop increments `blocked_count` for
every such skip (previously some `return None` paths were not counted,
which could let status stay COMPLETE despite skipped constraints; this
is now fixed).

### 7a.4 Generation-status semantics

| Condition | Status |
|---|---|
| All eligible constraints emitted, no errors | `COMPLETE` |
| Some emitted, some blocked/skipped with diagnostics | `PARTIAL` |
| All eligible blocked/skipped | `BLOCKED` |
| Renderer exception | `ERROR` |

Every `_render_constraint` path that returns `None` (skip) now
increments `blocked_count` and records a `NOT_EMITTED` diagnostic, so
skipped constraints always move the status away from COMPLETE.

### 7a.5 Generated-clock option fidelity

* `divide_by` and `multiply_by` are now emitted independently (no
  `elif`).  SDC treats them as mutually exclusive; if both are present
  in the UCM, preflight emits a fatal `DIV_MUL_CONFLICT` diagnostic
  and the constraint is blocked rather than silently dropping one.
* `-edge_shift` is gated on caps; see 7a.2.
* `-edges` without `-edge_shift` is allowed (pure edge-aligned
  generated clock, no shift).
* `-edge_shift` without `-edges` is a fatal preflight error
  (`EDGE_SHIFT_WITHOUT_EDGES`).

---

## 8. Known limitations

These are explicitly *not* addressed in Work Package I per the standing
constraint not to begin advanced validation / MCMM / commercial-flow work:

1. **MCMM scenario emission.** Capability matrix advertises `mcmm=False`
   for generic; OpenSTA/Synopsys/Cadence advertise `True` but the
   renderer does not yet emit per-scenario `-case`/set_scenario blocks.
   A future step will address this when MCMM is in scope.
2. **set_data_check / set_clock_gating_check / set_case_analysis /
   set_disable_timing / set_operating_conditions / set_wire_load_model**
   are recognized but rendered as opaque passthrough comment/value
   entries rather than semantically. They will be modeled in a later
   step.
3. **Nested command substitutions inside target collections** (e.g.
   `[get_pins -of_objects [get_cells U]]`) are preserved as
   UNRESOLVED `TargetRef`s rather than re-executed; the renderer skips
   them with a diagnostic rather than guessing.
4. **Design-aware resolution** in the renderer is intentionally not
   performed — the renderer trusts the semantic TargetRef/name
   information carried in the UCM. Resolution is the job of the
   importer/inference layers.
5. **Byte-identical SDC round-trip is NOT claimed.** The semantic
   round-trip tests confirm that SDC→UCM→SDC preserves the
   *meaning* (period, edges, groups, min/max/rise/fall, multicycle
   setup/hold, multi-through ordering). Normalizations that may change
   textually include: ns unit formatting (10.000 → 10), ordering of
   option flags, whitespace within brace groups, and expansion of
   wildcard-free single-object groups.

---

## 9. Deliberately non-emittable constraints (examples)

The preflight + safe-mode pipeline refuses to emit SDC for these and
records a diagnostic:

1. **`create_clock` with no `period`.** Output does not contain a
   `create_clock` line; status is BLOCKED or PARTIAL; diagnostic
   `NO_PERIOD: create_clock has no period; cannot emit`.
2. **`set_input_delay`/`set_output_delay` with no `delay` value** →
   `NO_DELAY`.
3. **`set_driving_cell` without `-lib_cell`** → `NO_LIB_CELL`.
4. **`set_clock_groups` with fewer than two groups** → `GROUPS_TOO_FEW`.
5. **Any `DISABLED`, `REJECTED`, `DEPRECATED`, or `MISSING` status
   constraint**, regardless of safe mode. STRICT additionally
   blocks low-confidence PROPOSED entries; AGGRESSIVE emits anything
   with semantic values but still will not invent period, delay,
   or other required fields.
6. **Constraints using backend-excluded capabilities** (e.g.
   `set_propagated_clock` for a backend that declared it unsupported) →
   CAP_* diagnostic + blocked.
7. **Constraints with UNRESOLVED EXPR target references** (e.g. a
   nested disallowed Tcl command) → WARNING diagnostic; renderer
   skips that constraint rather than emitting a bogus selector.
8. **Cadence backend with `-edge_shift`** → emits with an
   `UNSUPPORTED_OPTION` warning (does not silently drop -edge_shift).

In all cases, the CLI prints the blocked constraint id and reason and
exits with code 2 when status is BLOCKED or ERROR.

---

## 10. Confirmation: generator does not infer new design intent

### 10a. Corrective-pass confirmation

* **No unsupported SDC option is emitted.** Every place where an
  option is subject to a backend capability flag now goes through
  preflight, and preflight FATAL errors cause the constraint to be
  skipped (not emitted with a warning). Post-correction, searching the
  renderer for the prior anti-pattern (warn-then-emit) finds no
  remaining instances for any declared capability key.
* **COMPLETE cannot be reported when eligible constraints were
  skipped.** `blocked_count` is now incremented on every path that
  causes `_render_constraint` to return `None`, including: preflight
  fatal issues, renderer exceptions, unsupported capabilities detected
  mid-render (e.g. `edge_shift` on Cadence), and missing-value returns.
* **No target inference from "/" in names.** The typed `TargetRef`
  system remains intact and the `_infer_refs` fallback uses explicit
  per-constraint-type defaults, not name syntax.
* **min_max="both" is preserved** via the canonical SDC unqualified
  command (no silent collapse to max).
* **-edge_shift is dropped only when the backend supports it**, and
  when it is unsupported the constraint is blocked with a clear
  diagnostic rather than approximated.

### 10b. Original confirmation

The Step 6 pipeline adheres strictly to the principle **UCM is the
source of truth; SDC is a derived artifact**:

* **No clock frequency is invented.** `create_clock` with missing
  `period` is blocked in all safe modes (red line #1).
* **No generated clock is invented.** `create_generated_clock`
  requires `-name`; absent explicit `-master_clock`/`-source` it emits
  only a warning; it never synthesizes divide ratios or master clocks.
* **Selector type is never inferred from name syntax.** The renderer
  dispatches on `TargetRef.collection_kind`; for legacy string-only
  constraints it uses a hard-coded default per constraint type
  (e.g. PORT for I/O delays, CLOCK for clock uncertainty), never on
  whether a name contains "/".
* **min/max and rise/fall are never collapsed.** The importer explodes
  them into separate semantic entries and the renderer emits one
  command per entry.
* **Clock groups are never flattened.** Multiple `-group { … }`
  arguments are preserved in the emitted command.
* **Multi-stage `-through` lists preserve order.** The renderer
  iterates `through_set`/`through_refs` in order and emits one
  `-through` per stage.
* **Safe modes are enforced semantically.** AGGRESSIVE cannot bypass
  missing required fields (period, delay, etc.); REJECTED/DISABLED/
  MISSING constraints are non-emittable in every mode.
* **Provenance is deterministic** (no timestamps/PIDs/machine paths),
  so identical UCM produces byte-identical SDC for a given backend
  and mode — no hidden state from rendering can masquerade as
  inference.
* **Preflight errors produce structured diagnostics**, never silent
  skips; the CLI refuses to report success when output is BLOCKED/
  ERROR.

No new optimization passes, MCMM constructs, design intent heuristics,
or commercial-flow specifics were introduced. Work Package II
(validation/optimization/MCMM/flows) has not been started.
