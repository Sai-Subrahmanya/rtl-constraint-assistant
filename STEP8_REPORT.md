# Step 8 — Hardened Timing Exception Analysis & Verification

This step adds conservative structural analysis for timing exceptions, a
formal-backend abstraction that never pretends to prove what it has not
proved, a blast-radius model, and a strict/balanced/exploratory emission
gate. The validator/coverage/SDC subsystems from Steps 1–7 are unchanged.

## Files modified

- `src/rca/utils/enums.py`
  - Expanded `VerificationStatus` to include
    `UNCHECKED / PROPOSED / STRUCTURALLY_ANALYZED / VERIFIED / INVALID /
    UNRESOLVED / ERROR / NOT_APPLICABLE`. `UNCERTAIN` kept as a legacy
    alias for `UNRESOLVED`.
  - Added `ExceptionRisk` (`LOW / MEDIUM / HIGH / CRITICAL`) and
    `ExceptionFindingKind` (BROAD, NO_EFFECT, CLOCK_DOMAIN_CROSSING,
    RESET_RELATED, TEST_MODE, USER_INTENT_REQUIRED,
    REQUIRES_FORMAL_VERIFICATION, CYCLE_COUNT_INVALID,
    SETUP_HOLD_INCOHERENT, MULTICYCLE_NO_EVIDENCE, CLOCK_GROUP_OVERLAP,
    UNRESOLVED_SELECTOR).
- `src/rca/exceptions/formal_backend.py` — replaced the dict-returning
  backend with a structured `VerificationResult` dataclass carrying
  constraint id, status, property, evidence, counterexample, tool,
  tool_version, runtime, message. Default backend is still
  `ConservativeFormalBackend`, which returns `UNRESOLVED` for every
  query (never `VERIFIED`). Added `MockFormalBackend` for deterministic
  tests.
- `src/rca/exceptions/analyzer.py` — new structural analyzer:
  - resolves -from/-to/-through selectors against the `TimingGraph`,
    expanding clock names to their driven registers so that
    `-from $clk` matches paths launched by `$clk`;
  - enumerates affected structural paths;
  - computes `ExceptionBlastRadius` (path_count, endpoint_count,
    clock_count, scenario_count, plus concrete startpoint/endpoint/
    clock/scenario lists);
  - emits structural findings for false-path and multicycle
    exceptions (BROAD, NO_EFFECT, CDC, RESET/TEST hints, cycle-count
    errors, setup/hold incoherence, etc.);
  - classifies risk LOW/MEDIUM/HIGH/CRITICAL;
  - synthesizes lifecycle (`STRUCTURALLY_ANALYZED` / `INVALID` /
    `NOT_APPLICABLE`);
  - provides `final_status` and `is_emittable(mode)` safe-gate:
    - **strict**: only `VERIFIED` (or user-FIXED) may emit; CRITICAL
      risk without user confirmation is blocked; INVALID/ERROR/NOT_APPLICABLE blocked.
    - **balanced**: VERIFIED + UNRESOLVED (non-CRITICAL) emit;
      INVALID/ERROR/NOT_APPLICABLE blocked.
    - **exploratory**: all but INVALID/ERROR emit.
  - Observational: never mutates ConstraintSet/Design/TimingGraph.
- `src/rca/exceptions/verifier.py` — `verify_exceptions(...)` now
  returns an `ExceptionAnalysisReport` combining structural findings
  with per-exception `VerificationResult` from the backend. If a
  backend raises, status is ERROR (not silently UNRESOLVED). If the
  structural layer marked a constraint INVALID, the backend is not
  invoked and the result is marked INVALID with structural evidence.
- `src/rca/exceptions/__init__.py` — re-exports updated API.
- `tests/unit/test_exceptions.py` (new) — 33 unit tests covering
  false-path broad/no-effect/narrow/CDC/reset findings, multicycle
  valid/invalid/setup-hold/no-effect/cross-clock, blast radius,
  clock-group overlap, CDC + exception interaction, formal-backend
  UNRESOLVED/VERIFIED/INVALID/ERROR, counterexample preservation,
  strict/balanced/exploratory emission gate, user-confirmed policy,
  determinism, UCM immutability, and adversarial cases (auto-false-path
  from unrelated clocks/sync/naming/poor timing never occurs).

## Exception lifecycle

```
PROPOSED
   ↓
STRUCTURALLY_ANALYZED   (structural analysis complete; findings + blast radius)
   ├── NO_EFFECT selector  → NOT_APPLICABLE
   ├── hard contradiction  → INVALID
   └── otherwise           → STRUCTURALLY_ANALYZED
                                 ↓
                ┌──────────┬────────────┬──────────┐
             VERIFIED    INVALID     UNRESOLVED    ERROR
             (formal     (counter-   (no proof,   (backend
              proof or    example,    structural    raised)
              user-      structural  ok)
              confirmed)  proof of
                          bug)
```

`final_status` drives the emission gate. `UNCERTAIN` is preserved as an
alias for `UNRESOLVED` so legacy code doesn't break.

## Verification-state model

Verification and approval are **separate** axes; one never implies the other.

**`verification_status`** (evidence alone — independent of user approval):

| Status | Meaning |
|--------|---------|
| UNCHECKED | Not yet analyzed. |
| PROPOSED | Entered into the set; awaiting analysis. |
| STRUCTURALLY_ANALYZED | Structural findings + blast radius computed (lifecycle state). |
| VERIFIED | A qualifying formal backend returned VERIFIED for this exception. |
| INVALID | Structural contradiction (bad cycle count, etc.) or formal counterexample. Cannot be overridden by user approval. |
| UNRESOLVED | Structural OK but no formal proof. |
| ERROR | Backend/analysis error. |
| NOT_APPLICABLE | Selector matches zero structural paths. |

`UNCERTAIN` remains as a legacy alias for `UNRESOLVED`.

**`approval_status`** (user intent, separate from proof):

| Status | Meaning |
|--------|---------|
| NONE | No user decision recorded. |
| USER_CONFIRMED | User has explicitly approved (e.g. `ConstraintStatus.FIXED`, or listed in `user_confirmed_ids`). Audit trail preserved. This **never** flips verification_status to VERIFIED. |
| USER_REJECTED | User has rejected the exception; emission is blocked regardless of proof. |

**`emission_status`** (final decision combining the three axes + risk):

| Status | Meaning |
|--------|---------|
| ALLOWED | Formally VERIFIED; eligible for emission. |
| ALLOWED_USER_CONFIRMED | Unverified but explicitly USER_CONFIRMED and narrow scope; eligible for balanced/exploratory modes only — must be labeled USER_CONFIRMED, never VERIFIED. |
| BLOCKED_INVALID | Contradiction / counterexample. Never emittable. |
| BLOCKED_UNVERIFIED | No proof and no user approval; blocked in strict. |
| BLOCKED_REJECTED | User rejected. |
| BLOCKED_CRITICAL_RISK | Broad/CRITICAL scope (e.g. blanket false_path) without VERIFIED + USER_CONFIRMED. |
| BLOCKED_NO_EFFECT | Selector matches zero paths; emission suppressed. |
| BLOCKED_ERROR | Analysis/backend error. |

A `VerificationResult` carries: constraint_id, status, property_checked,
evidence dict, counterexample (dict), tool, tool_version,
runtime_seconds, message, source_constraint_id. Counterexamples are
preserved in the report; they are not discarded.

## Blast-radius model

`ExceptionBlastRadius` reports deterministic, sorted lists:

* path_count — number of structural `TimingPath`s matched.
* endpoint_count — distinct endpoints reached.
* clock_count — distinct launch/capture clocks crossed.
* scenario_count — distinct scenarios attached to the exception.
* affected_startpoints / endpoints / clocks / scenarios — explicit
  object lists for explanation/reporting.

Risk is LOW for <1 path (already NOT_APPLICABLE), MEDIUM for narrow
intra-domain exceptions, HIGH for cross-clock or >20-path scopes,
CRITICAL for selectorless (blanket) exceptions or >200-path scopes.
Blast radius is a review aid, not a validity verdict.

## Formal-backend interface

* Abstract `FormalBackend.prove_false_path(constraint_id, spec)` /
  `prove_multicycle(constraint_id, spec, cycles)` return
  `VerificationResult`.
* Default `ConservativeFormalBackend` returns UNRESOLVED for every
  query — it never claims a proof it didn't run.
* `MockFormalBackend` lets tests inject deterministic VERIFIED /
  INVALID / UNRESOLVED / ERROR verdicts.
* Backend exceptions are caught and converted to VerificationStatus.ERROR
  rather than silently swallowed.
* No SymbiYosys backend is implemented in this step; the abstraction is
  ready but the policy (do not fake formal verification) is enforced.

## Safe-mode emission gate

| Mode | ALLOWED (VERIFIED) | ALLOWED_USER_CONFIRMED | BLOCKED_UNVERIFIED (narrow) | BLOCKED_INVALID/ERROR/REJECTED/NO_EFFECT/CRITICAL |
|------|-----|-----|-----|-----|
| strict | emit | **blocked** | blocked | blocked |
| balanced | emit | emit (non-CRITICAL) | emit with warning (LOW/MEDIUM risk) | blocked |
| exploratory | emit | emit | emit (non-CRITICAL) | blocked |

Key invariant: **User confirmation does not change verification_status.**
In strict mode a USER_CONFIRMED but UNRESOLVED exception is still
blocked; in balanced/exploratory it is emitted but its
`emission_status` is `ALLOWED_USER_CONFIRMED` and reporting labels it
"USER_CONFIRMED", never "VERIFIED".  INVALID / ERROR / REJECTED /
NOT_APPLICABLE are never emittable, regardless of mode. CRITICAL-broad
scope is blocked unless the exception is both VERIFIED and
USER_CONFIRMED.

## Through-selector semantics

`PathSelector.through_set` is an ordered list of stages, each stage
being a list of names (one `-through` argument). Matching follows SDC/UCM
semantics:

* **Ordered AND across stages**: for `-through A -through B` the path
  must contain `A` (at some position) followed later by `B`. The reverse
  (`B` before `A`) does NOT match.
* **OR within a stage**: a single `-through {A B}` (one stage with
  multiple names) is satisfied if ANY of its names appears at that
  ordered position in the path.
* **Endpoint/startpoint precedence**: a matching `-through` object never
  compensates for a wrong `-from` or `-to`; all three selectors must be
  satisfied simultaneously.
* **No selector**: no through restriction.
* **Zero match**: when a `-through` stage has no matching object on any
  path, the exception is classified NO_EFFECT.

The haystack used for ordered matching is
`[startpoint] + combinational_elements + [endpoint]`, preserving signal
flow order from launch to capture.

## Determinism

* Paths and selectors are iterated in sorted order; startpoints,
  endpoints, clocks, scenarios are returned as sorted lists.
* Structural finding IDs are derived from `stable_hash(constraint_id,
  kind, message[:160])` (not Python `hash()`).
* Two runs of `analyze_exceptions(...)` on identical inputs produce
  identical `to_dict()` output (verified by `test_20` and `test_21`).

## Safety invariants enforced by tests

1. `test_12`, `test_24`, `test_25`: unrelated clocks and synchronizer
   structure never cause an exception to be marked VERIFIED.
2. `test_26`: reset-named signals are flagged with a RESET_RELATED hint
   but are not automatically considered safe.
3. `test_02`, `test_09`, `test_28`: zero-match selectors become
   NOT_APPLICABLE and are never counted as VERIFIED or emittable.
4. `test_27`, `test_33`: broad CRITICAL-blast-radius exceptions are
   blocked in strict mode even when a formal verdict is absent.
5. `test_13`, `test_14`, `test_15`, `test_31`: when no backend exists
   the result is UNRESOLVED; counterexamples from a backend are
   preserved on INVALID.
6. `test_22`: analysis does not mutate the ConstraintSet.

## Tests added

`tests/unit/test_exceptions.py` — 50 tests covering:
broad FP, zero-match NO_EFFECT, narrow match, CDC FP, reset-named FP,
valid multicycle, invalid cycle count, setup/hold mismatch, multicycle
no-effect, broad CRITICAL risk, clock-group overlap, CDC sync not
auto-verified, unavailable formal → UNRESOLVED, VERIFIED backend,
INVALID backend with counterexample, INVALID never emittable,
UNRESOLVED suppressed in strict, user-confirmed vs verification
separation (user-confirmed ≠ VERIFIED, user-confirmed+VERIFIED stays
VERIFIED, user-confirmed+INVALID stays INVALID and blocks,
user-rejected always blocks, FIXED does not imply VERIFIED, strict
requires true VERIFIED), through selector semantics (one-stage match,
ordered two-stage match, reversed stages do NOT match, multi-object OR
within a stage, wrong through, wrong endpoint, correct-through-wrong-endpoint,
multi-stage across longer paths, no-through no restriction, zero through
match → NO_EFFECT, through narrows blast radius), provenance roundtrip,
blast-radius determinism, verification-result determinism, no UCM
mutation, dict snapshot/restore, unrelated clocks not auto-false, sync
data path unresolved without explicit exception, reset-named not safe,
large wildcard critical, zero-match not verified, multicycle across
unrelated clocks, poor-timing not auto-falsed, counterexample storage,
backend error → ERROR status, emittable filter by mode.

## Test execution

In a sandbox environment where `pyslang` is installed (otherwise
parser/timing/inference/connectivity tests are dependency-blocked
with a pre-existing `RuntimeError`):

```
python -m pytest tests/unit/test_exceptions.py -q   → 50 passed
python -m pytest tests/unit/test_exceptions.py \
                   tests/unit/test_constraints.py \
                   tests/unit/test_sdc_parser.py \
                   tests/unit/test_pareto.py \
                   tests/unit/test_units.py \
                   tests/unit/test_validation.py \
                   tests/unit/test_sdc_generation.py \
                   tests/unit/test_ucm_provenance.py -q
                                                    → 253 passed, 0 failed
```

All 17 new regression tests for the approval-separation and ordered-
through fixes pass; all 33 prior exception tests remain green after
adaptation to the split `verification_status` / `approval_status` /
`emission_status` model (the old `final_status`/`user_confirmed`
attributes were removed because they encouraged the incorrect conflation
we are fixing).

Counts after this correction pass: **50/50 in
test_exceptions.py, 253/253 in the pyslang-independent core unit
suite; 0 failures attributable to the Step 8 changes.**

## Known limitations

- No concrete SymbiYosys/VC formal backend is wired up yet — the
  abstract interface is ready and conservative behavior is correct.
- Clock-group overlap detection reports overlap but does not
  automatically remove the exception (redundancy is flagged for user
  review, per Step 8 scope).
- Reset/test classification is based on naming hints only (structure-
  aware reset-detection is a later refinement); the hint is MEDIUM,
  never auto-safe.
- Set_min_delay / set_max_delay structural analysis is conservative
  (generic checks for zero-match and blast radius); dedicated
  semantics may be added later.
- Selector resolution uses clock-to-register expansion based on
  `clock.registers_driven`; through-stage matching requires a through
  name to appear in the path's combinational_elements or endpoints.
