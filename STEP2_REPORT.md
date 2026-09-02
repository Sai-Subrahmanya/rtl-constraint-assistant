# Step 2 — Harden Timing/Clock/Reset/Domain Model — Completion Report

**Date:** 2026-08-29
**Scope:** WP-D, 18-point directive for the RTL Constraint Assistant.
**Status:** COMPLETE — all acceptance criteria met.

## Summary

The timing model has been rewritten from a name/ordering heuristic to an
**evidence-driven, structural** model. Clock/reset discovery is grounded
in register clock pins, sensitivity-list edges, and reset-branch
predicates; naming is retained as LOW-confidence corroboration and can
never alone produce a HIGH-confidence clock or reset. Clock
relationships default to UNKNOWN, generated/gated/mux constructs are
emitted as conservative candidates requiring confirmation, I/O clock
association is resolved via BFS over the Step-1 data graph (no default
to "first clock"), and CDC classification is derived purely from
structural register-to-register paths.

## Files changed

| File | Change |
|---|---|
| `src/rca/timing_model/clock.py` | **Rewritten** — added `ClockEvidenceKind` (9 categories), `ClockEvidence` record, numeric strength ranking, `add_evidence()`, auto `_recompute_confidence()` (naming-only ⇒ LOW, structural ⇒ HIGH, USER ⇒ HIGH); new fields `processes`, `mux_sources`, `gate_enable_signal`, `is_top_level_port`, `mux_select_signal`, `notes`. |
| `src/rca/timing_model/reset.py` | **Rewritten** — added `ResetEvidenceKind` (6 categories), `ResetEvidence`, `add_evidence()`, `_recompute_confidence()`; async inferred from EDGE_SENSITIVE+RESET_BRANCH, sync from SYNC_CONTROL; polarity inferred from predicate negation; name-only never materializes a Reset. |
| `src/rca/timing_model/clock_domain.py` | **Rewritten** — added `reset_ids`, `evidence`, `register_count()`, `summary()`; `ClockDomainEdge.set_relationship()` never downgrades HIGH-USER; new `cdc_paths_observed`, richer `summary()`. |
| `src/rca/timing_model/timing_graph.py` | **Rewritten** (~600 lines) — evidence-driven orchestration: structural clock/reset discovery, conservative gated-clock/mux/generated-clock candidate detection (only when output is edge-sensitive), domain build from register clock pins, I/O clock BFS association (None when unknown), user-relationship merge preserving HIGH, CDC marking without auto-async, deterministic ordering/ids, structured `missing_information()`, pydantic v2 `ConfigDict`. |
| `src/rca/parser/slang_adapter.py` | **Hardened** — added `_detect_direct_predicate_signals()` and module-level `_find_first_conditional()` so active-high async resets (`posedge clk or posedge rst` / `if (rst) q<=0`) are correctly classified; conservative multi-edge disambiguation (first posedge picked as clock, others recorded as ambiguous — not silently promoted to reset). |
| `src/rca/inference/engine.py` | **Compatibility fix** — `c.evidence` iteration is now tolerant of `ClockEvidence`/`ResetEvidence` objects (uses `.kind`/`.detail`, falls back to str). No inference-logic changes. |
| `tests/unit/test_timing_model.py` | **New** — 25 unit tests covering scenarios A–V plus naming-only clock/reset guards and determinism. |
| `tests/golden/test_timing_golden.py` | **New** — 6 golden tests over a small 5-file corpus (`tests/golden/timing_corpus/*.sv`); uses normalized semantic comparison (sets, sorted lists) rather than exact ordering. |
| `tests/golden/timing_corpus/01_dff_async_rstn.sv` | **New** — golden RTL: single dff + async active-low reset. |
| `tests/golden/timing_corpus/02_two_clocks_sync.sv` | **New** — golden RTL: two unrelated clocks (default UNKNOWN; user-declared SYNC case also tested). |
| `tests/golden/timing_corpus/03_sync_reset.sv` | **New** — golden RTL: synchronous reset (not in sensitivity list). |
| `tests/golden/timing_corpus/04_cdc_sync2.sv` | **New** — golden RTL: 2-flop synchronizer across domains. |
| `tests/golden/timing_corpus/05_clk_name_as_data.sv` | **New** — golden RTL: signal literally named `clk_sel` used as data (must NOT become a clock). |

## Architecture changes (TimingModel)

1. **Evidence model** — each Clock/Reset carries a list of typed evidence
   records (`ClockEvidence`/`ResetEvidence`) with a discrete `kind`,
   human-readable `detail`, and optional `source`. Strength is numeric,
   not implicit: USER=4, DRIVES_REGISTER/EDGE_SENSITIVE/SEQUENTIAL_PROCESS=3,
   GENERATED/GATED/MUX=2, TOP_LEVEL_PORT=1, NAMING_HINT=0.
2. **Confidence** is derived deterministically from evidence
   (`_recompute_confidence`). Naming-only evidence forces LOW regardless
   of other weak signals; USER forces HIGH.
3. **Clock discovery** — a signal becomes a Clock only when structural
   evidence exists (edge-sensitive use, drives a register, in a
   sequential process, or a top-level port with
   `connected_clock_candidates`). Naming hints are attached as
   `NAMING_HINT` but never create a Clock on their own.
4. **Reset discovery** — async reset requires EDGE_SENSITIVE + a reset
   branch (constant load under a predicate that references the signal);
   sync reset is flagged only when a control signal guards a constant
   load inside a clocked process AND is not on the sensitivity list.
   Name-only hits are dropped.
5. **Sequential-edge classification** — no more "first edge = clk,
   second = rst" heuristic. Active-low and active-high async resets are
   both detected via predicate polarity; ambiguous multi-edge blocks
   conservatively pick the first posedge as clock and record the rest as
   `ambiguous_event_signals` (never silently relabeled as reset).
6. **Clock gating/mux** — detected only from continuous-assign RHS whose
   destination is itself used as an edge-sensitive clock. This avoids
   false positives on plain `clk & x` data logic. Outputs are CANDIDATES
   with `user_confirmation_required=True`.
7. **Generated clocks** — register-Q signals used edge-sensitively
   become POSSIBLE_GENERATED_CLOCK candidates with a master-clock
   pointer, `divide_by=None` (we do not guess divide ratios), and
   confirmation required.
8. **Domains** — built 1:1 from discovered primary clocks; membership
   comes from `Register.clock_signal` (structural), not names. Resets
   attach via their associated clock.
9. **Default relationship** is UNKNOWN with `user_confirmation_required=True`
   for every clock pair. Observing a CDC path increments
   `cdc_paths_observed` but does NOT mark the clocks asynchronous.
   User-declared relationships are applied with HIGH confidence and
   cannot be silently downgraded.
10. **Path classification** uses Step-1 structural paths (`structural_paths`
    + `cdc_paths`) as the source of connectivity; no heuristic
    path-count recreation. CDC = launch≠capture or explicit CDC class.
11. **I/O clock association** is BFS over the Step-1 data fanout/fanin;
    ports with no structurally-reachable register clock get `None` and
    are surfaced as `input_clock_association`/`output_clock_association`
    missing-info entries.
12. **Missing information** is structured into 8 categories:
    `clock_period` (required), `clock_relationship` (recommended),
    `input_clock_association`, `output_clock_association`,
    `generated_clock_candidate`, `clock_mux_candidate`,
    `clock_gating_candidate` (all confirmation_required/recommended).
13. **Determinism** — all iterations sort by name; ids are assigned from
    sorted iteration order; summary output is order-independent
    (validated by a dedicated determinism test and by building twice on
    a 4-flop 2-clock+reset design and comparing JSON summaries).

## Evidence categories

**Clock:** `edge_sensitive`, `drives_register`, `sequential_process`,
`top_level_port`, `user_declared`, `generated_clock`, `gated_clock`,
`clock_mux`, `naming_hint`.

**Reset:** `edge_sensitive`, `reset_branch`, `sync_control`,
`register_assign`, `naming_hint`, `user_declared`.

## Tests

**Total: 135 passed, 0 failed, 0 blocked.**

| Suite | Tests | Status |
|---|---|---|
| `tests/unit/test_parser.py` | 11 | ✅ PASS |
| `tests/unit/test_connectivity.py` | 13 | ✅ PASS (pre-existing, Step 1) |
| `tests/unit/test_expression_semantics.py` | 66 | ✅ PASS (pre-existing, Step 1) |
| `tests/unit/test_sdc_parser.py` | 4 | ✅ PASS |
| `tests/unit/test_units.py` | 5 | ✅ PASS |
| `tests/unit/test_constraint_model.py` + `test_config.py` + `test_explanation.py` + `test_inference.py` | 11 | ✅ PASS |
| `tests/unit/test_timing_model.py` | **25** | ✅ PASS (new, scenarios A–V + guards + determinism) |
| `tests/golden/test_timing_golden.py` | **6** | ✅ PASS (new, over 5-file corpus) |
| **Total** | **135** | **✅ PASS** |

## Commands

```bash
# Run all tests
cd /home/user/rtl-constraint-assistant
python3 -m pytest tests/ -v

# Run just the new timing-model unit tests
python3 -m pytest tests/unit/test_timing_model.py -v

# Run just the golden timing tests
python3 -m pytest tests/golden/test_timing_golden.py -v
```

## Remaining limitations (explicit, not in scope for Step 2)

- **SDC generation is unchanged.** No `create_generated_clock`,
  `set_clock_gating_check`, `set_clock_groups`, or false-path emission
  was added — those are explicitly deferred (per directive item 17).
  Generated/gated/mux candidates are surfaced for user confirmation
  only.
- **Divide-by / multiply-by ratios** for generated clocks are not
  inferred; `divide_by=None` until the user (or a future step) supplies
  them. We identify the master clock but do not guess ratios.
- **Clock-gating intent** is structural-only (continuous AND/OR between
  a clock-like signal and another signal, whose output is itself used
  as a clock). Latch-based integrated clock gating (ICG) cells are not
  yet recognized; that is a later Step.
- **Synchronous reset** detection uses a conservative heuristic
  (constant-0 load under a non-sensitivity control signal with no other
  data/control sources); it is MEDIUM confidence and may miss
  multi-predicate resets. Such misses surface as conservative (no
  spurious HIGH).
- **Multi-clock mux exclusivity** is not inferred; we record the mux
  and its select but do not mark clocks exclusive.
- **Cross-hierarchy generated clocks** (output of a submodule used as
  a clock upstream) require cross-module connectivity projection that
  the Step-1 graph already supports but our generated-clock detector
  does not yet traverse; current detector looks at local register Q
  names only.
- **Negedge-clock domain handling** is represented correctly (clock
  edge stored, CDC classification by domain not edge) but no explicit
  `set_clock_sense -negative` SDC is emitted (SDC is deferred).
- No MCMM, formal, optimizer, or commercial-adapter work was attempted
  (out of scope).

## Acceptance-criteria checklist (from directive item 18)

| # | Criterion | Met? |
|---|---|---|
| 1 | Clock discovery structural | ✅ |
| 2 | Reset structural / context-aware (active-high + active-low) | ✅ |
| 3 | Sequential edges correctly classified (posedge/negedge, ambiguous ⇒ UNKNOWN) | ✅ |
| 4 | Domains from actual register clocking | ✅ |
| 5 | Unknown relationships stay UNKNOWN (no auto-async from name) | ✅ |
| 6 | Inferred relationships carry evidence (USER is HIGH, CDC observation is evidence not verdict) | ✅ |
| 7 | Generated/gating/mux candidates conservative (confirmation required, no SDC) | ✅ |
| 8 | CDC from actual crossings (Step-1 paths) | ✅ |
| 9 | Paths use Step-1 connectivity | ✅ |
| 10 | I/O clock assoc not assumed single global (None + missing_info) | ✅ |
| 11 | Missing info structured (8 categories) | ✅ |
| 12 | Adversarial unit tests exist (A–V, 25 tests) | ✅ |
| 13 | Golden tests exist (6 tests, 5-file corpus) | ✅ |
| 14 | Deterministic output verified (dedicated test + 4-flop 2-clk double-build) | ✅ |
| 15 | All available tests pass (135/135) | ✅ |
| 16 | No SDC-generation changes (compatibility-only change in engine.py) | ✅ |
| 17 | No premature progression to Step 3+ features | ✅ |
