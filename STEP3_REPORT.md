# Step 3 — Provenance, Dependency Graph, Scenarios, and Versioned Canonical UCM Snapshot (Third Pass: dependency-integrity hardening)

This report covers the final (third) pass of Step 3. The second pass
delivered a canonical, versioned, deterministic, lossless UCM snapshot
with separate presentation `summary()`. This pass fixes the single
remaining correctness/auditability concern: **inconsistent
dependency/downstream edges are no longer silently repaired during
normal restoration**.

## Dependency-integrity policy

`ConstraintSet.from_snapshot_dict(snap, *, repair_reverse_edges=False, allow_cycles=True)`:

1. **Schema version check** — missing `schema_version`, future schema
   versions, or versions with no migration path raise
   `SnapshotFormatError`. The original `snap` dict is never mutated
   (restoration deep-copies every nested structure before touching it).
2. **Structural integrity checks** (always fatal, never repaired):
   * `MISSING_DEPENDENCY` — a constraint's `dependency_ids` references
     an id that is not in the snapshot's `constraints`.
   * `MISSING_DOWNSTREAM` — a constraint's `downstream_ids` references
     an id that is not in the snapshot's `constraints`.
   * `SELF_DEPENDENCY` — a constraint lists itself in either
     `dependency_ids` or `downstream_ids`.
   * `DEPENDENCY_CYCLE` — only when `allow_cycles=False` (default
     `True`, because the stale-set walker tolerates cycles; cycles are
     still reported at WARNING level by `validate()`).
   Invalid references are **never silently deleted**.
3. **Reverse-edge consistency** (the policy requested by the user):
   * **Default** (`repair_reverse_edges=False`): if
     `dep ∈ C.dependency_ids` does not imply
     `C.id ∈ constraints[dep].downstream_ids`, or vice versa, a
     `SnapshotFormatError` is raised. The `details` attribute lists
     every mismatch with the affected constraint ids and the
     conflicting edge sets. **No ConstraintSet is returned and the
     caller's snapshot is untouched.**
   * **Explicit repair** (`repair_reverse_edges=True`): reverse edges
     are rebuilt from forward edges, each repair is appended to the
     returned ConstraintSet's `.snapshot_repairs` list as a
     `SnapshotRepairRecord(code="REVERSE_EDGE_REPAIRED", subject=cid,
     before=..., after=...)`, and the restored set validates clean.
     The original `snap` dict is still NOT mutated (we deep-copied on
     entry). If post-repair verification finds remaining mismatches, a
     `SnapshotFormatError` is raised.
4. **Cycles** — allowed by default (`allow_cycles=True`) but recorded
     as a `SnapshotRepairRecord` with code `DEPENDENCY_CYCLE_DETECTED`
     and also surfaced as a WARNING-level `ValidationIssue` by
     `validate()`. Pass `allow_cycles=False` to reject them at restore
     time.
5. **validate()** now reports `SELF_DEPENDENCY` errors and
     `DEPENDENCY_CYCLE` warnings alongside the existing
     `BAD_DEPENDENCY`, `BAD_DOWNSTREAM`, and `REVERSE_EDGE_MISMATCH`
     checks, so in-memory models get the same invariants.

The invariants enforced are exactly:

* For every constraint `C`, for every `dep ∈ C.dependency_ids`:
  `dep` must exist in `constraints`, `dep != C.id`, and
  `C.id ∈ constraints[dep].downstream_ids`.
* For every constraint `C`, for every `down ∈ C.downstream_ids`:
  `down` must exist in `constraints`, `down != C.id`, and
  `C.id ∈ constraints[down].dependency_ids`.

When a snapshot is rejected, the exception (`SnapshotFormatError`)
carries `details: list[dict]` containing the original mismatch
descriptions (code, subject, missing id, conflicting edge sets) so
callers can log/persist an audit record alongside the untouched
original snapshot file.

## Files changed this pass

* `src/rca/constraint_model/constraint_set.py`:
  * Added `SnapshotFormatError(details=...)` exception class.
  * Added `SnapshotRepairRecord` pydantic model (code/subject/message/
    before/after).
  * Rewrote `from_snapshot_dict()` to:
    - Deep-copy all incoming data so the caller's snapshot is never
      mutated.
    - Perform structural integrity checks (missing refs, self-deps,
      cycles per `allow_cycles`) and raise `SnapshotFormatError` with
      detailed `details` instead of silently repairing.
    - Default `repair_reverse_edges=False`; when False, reverse-edge
      mismatches raise with a detailed list of conflicts. When True,
      rebuild reverse edges from forward edges and record each repair
      on `cs._snapshot_repairs`.
    - Refuse to return a ConstraintSet if any non-reverse-edge issue
      is present (cannot safely guess what to delete).
    - Post-repair verification ensures zero mismatches remain.
  * Added `snapshot_repairs` property on `ConstraintSet`.
  * Added Tarjan-based `_find_dependency_cycles()` helper.
  * `validate()` now reports `SELF_DEPENDENCY` errors and
    `DEPENDENCY_CYCLE` warnings.
  * Legacy `_from_legacy_summary()` (pre-v1) still rebuilds reverse
    edges best-effort, but v1 canonical snapshots do not.
  * Fixed classmethod indentation for `from_snapshot()` (was
    incorrectly nested inside `_find_dependency_cycles` after a prior
    edit, causing an AttributeError at collection time).
* `src/rca/constraint_model/__init__.py`: exports
  `SnapshotFormatError`, `SnapshotRepairRecord`, `ValidationIssue`,
  `UCM_SNAPSHOT_SCHEMA_VERSION`.
* `src/rca/constraint_model/UCM_SNAPSHOT.md`: updated with the
  reverse-edge policy (default no silent repair, explicit opt-in
  repair mode with recorded repairs).
* `tests/unit/test_ucm_provenance.py`: replaced the old
  reverse-edge-auto-repair test with dedicated tests for the new
  policy and added insertion-order determinism + double-roundtrip
  tests.

## Tests added

Appended to `tests/unit/test_ucm_provenance.py` (test_23_* / test_24_*):

* `test_23_reverse_edge_mismatch_default_raises` — by default
  `from_snapshot_dict` raises `SnapshotFormatError` with
  `REVERSE_EDGE_MISMATCH` details; original snapshot dict is not
  mutated.
* `test_23_reverse_edge_mismatch_explicit_repair_records` —
  `repair_reverse_edges=True` restores, repairs are visible on
  `.snapshot_repairs`, validates clean.
* `test_23_missing_dependency_reference_raises` — missing dep id
  raises `SnapshotFormatError` with `MISSING_DEPENDENCY` (not
  silently dropped).
* `test_23_self_dependency_raises` — self-dependency raises with
  `SELF_DEPENDENCY` detail.
* `test_23_cycle_rejected_when_policy_says_so` — cycles are recorded
  as repairs when allowed (default) and raise with `DEPENDENCY_CYCLE`
  when `allow_cycles=False`.
* `test_in_memory_validate_detects_reverse_mismatch` —
  `validate()` flags reverse mismatches on in-memory broken graphs.
* `test_24_canonical_json_deterministic_across_insertion_orders` —
  two independently built semantically identical ConstraintSets
  (constraints inserted in different orders, scenarios inserted in
  different orders, assumptions inserted in different orders,
  evidence added in different orders, dependency edges wired in
  different orders) produce byte-identical canonical JSON; restoring
  and re-serializing is also byte-identical.
* `test_24_full_canonical_roundtrip_double_json_equivalence` —
  comprehensive UCM (constraint + provenance + evidence + assumption
  + scenario + forward dependency + reverse dependency +
  PathSelector + ImportMetadata) → canonical JSON → restore →
  canonical JSON produces byte-identical output, validates cleanly,
  and stale-set invalidation still returns the correct stale
  constraints and analyses after assumption mutation.

Existing assumption-invalidation round-trip test
(`test_23_assumption_invalidation_roundtrip`) continues to pass;
enum-normalization tests for SourceKind / Confidence /
ConstraintStatus / OptimizationStatus / PathSelector continue to
pass.

## Test command and actual result

Command: `python -m pytest -q` (from repo root
`/home/user/rtl-constraint-assistant`).

```
collected 192 items
192 passed
0 failed
0 errors
0 skipped
```

Breakdown:

* tests/golden/test_timing_golden.py ..................... 6
* tests/unit/test_connectivity.py ....................... 44
* tests/unit/test_constraints.py ......................... 5
* tests/unit/test_expression_semantics.py ............... 32
* tests/unit/test_pareto.py .............................. 6
* tests/unit/test_parser.py .............................. 7
* tests/unit/test_sdc_parser.py .......................... 4
* tests/unit/test_timing_model.py ....................... 25
* tests/unit/test_ucm_provenance.py ..................... 57
* tests/unit/test_units.py ............................... 6
Total ................................................. 192

A manual end-to-end Slang→TimingGraph→InferenceEngine→snapshot→JSON→
restore flow validates cleanly; tampering with a reverse edge raises
`SnapshotFormatError` by default and succeeds with recorded repairs
when `repair_reverse_edges=True`, without mutating the caller's dict.

## Remaining limitations

* Schema migrations beyond v1 are still not defined; future
  `schema_version` values will be refused until an explicit migration
  is added.
* Pre-v1 (legacy) snapshots are rehydrated best-effort via
  `_from_legacy_summary()`; they never contained reverse edges in the
  first place, so reverse-edge reconstruction there is expected (and
  documented) rather than a "repair".
* Cycles are permitted by default. There is no known legitimate
  timing-constraint use for a forward-dependency cycle; the default
  is permissive because the stale-set walker handles them and the
  behavior is documented and surfaced as a WARNING. Callers that want
  strict cycle rejection can pass `allow_cycles=False` to
  `from_snapshot_dict`.
* Multi-clock gating, latch inference, CDC, and generated/gating/mux
  clock proposals remain LOW confidence / pending confirmation per
  the standing rules; none of that was touched in this pass.
* Unrelated subsystems (structural connectivity, timing model, SDC
  generation, validation engine, optimizer, OpenSTA, MCMM, dashboard,
  commercial backends) were not modified.

## Confirmation

**Inconsistent persisted UCM data is never silently mutated during
normal restoration.** The default path (`from_snapshot_dict(...)`
with `repair_reverse_edges=False`) raises `SnapshotFormatError`
listing every affected constraint id and both conflicting edge sets;
the caller's `snap` dict is deep-copied on entry and left untouched;
no ConstraintSet is returned. Opt-in repair is an explicit caller
choice (`repair_reverse_edges=True`) and every repair is recorded on
the returned object (`.snapshot_repairs`) so the inconsistency is
auditable. Missing dependency/downstream references and self
dependencies are always fatal — they are never deleted or guessed.
