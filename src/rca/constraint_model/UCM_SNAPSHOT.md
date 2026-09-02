# UCM Canonical Snapshot Format

The Universal Constraint Model exposes **two** serializations. They serve
different purposes and must not be confused.

---

## 1. Presentation summary — `Constraint.summary()` / `ConstraintSet.snapshot()`

* Intended for: CLI output, dashboards, compact progress reports.
* Properties:
  * Human-friendly.
  * **Lossy** — it collapses evidence lists to counts, drops most
    `PathSelector` fields, omits downstream dependency edges, and
    elides full import metadata.
  * Suitable for diffing / quick eyeballing; **NOT** a persistence
    format.

## 2. Canonical snapshot — `ConstraintSet.to_snapshot_dict()` / `from_snapshot_dict()`

* Intended for: persistence, cache keys, reproducibility, candidate
  reconstruction, audit logs, cross-run comparison.
* Properties:
  * **Lossless** for semantic UCM state.
  * Explicitly versioned (`schema_version`, currently `1`).
  * Deterministic — identical logical UCMs serialize to byte-identical
    JSON (via `to_canonical_json()`).
  * Bidirectional: `from_snapshot_dict(to_snapshot_dict())` reconstructs
    a UCM whose constraints have identical `semantic_key()` values,
    intact provenance, intact assumptions, and a consistent
    bidirectional dependency graph.

### Excluded (transient) fields

The following fields are intentionally NOT part of the canonical
snapshot. They are presentation/runtime caches and must never carry
semantic state:

* `Constraint.generated_text_by_backend`
* `Constraint.equivalent_forms`
* Python object identities / memory addresses
* Logging / ephemeral run handles

### Schema (schema_version = 1)

```text
{
  "schema_version": 1,
  "name": str,
  "run_id": str | null,
  "created_at": ISO8601 UTC str,
  "metadata": { ... sorted keys ... },

  "scenarios": {
    <scenario_id>: {
      "id": str,
      "mode": str,
      "corner": str,
      "libraries": [str, ...],
      "parasitics": str | null,
      "sdc_set_id": str | null,
      "environment": { ... sorted keys ... },
      "active": bool,
      "analysis_count": int,
      "parent": str | null
    },
    ...
  },

  "constraints": {
    <constraint_id>: {
      "id": str,
      "type": str,                # ConstraintType.value
      "target_objects": [sorted str],
      "source_objects": [sorted str],
      "through_objects": [sorted str],
      "clock_refs": [sorted str],
      "values": { ... sorted keys, values normalized ... },
      "source_kind": str,         # SourceKind.value
      "provenance": {
        "created_by": str,
        "created_at": ISO8601,
        "source_kind": str,
        "rule_id": str | null,
        "evidence": [ {id, kind, description, detail, source_objects,
                        location, confidence, rule_id, created_by,
                        created_at}, ... sorted by id ],
        "assumption_ids": [sorted str],
        "dependency_ids":   [sorted str],
        "downstream_ids":   [sorted str],
        "affected_paths":   [sorted str],
        "scenario_ids":     [sorted str],
        "import_meta": {
          "source_file": str,
          "source_line": int | null,
          "original_command": str | null,
          "source_format": str,
          "import_run_id": str | null,
          "import_timestamp": ISO8601,
          "extra": { ... sorted keys ... }
        } | null,
        "explanation": str,
        "confidence": str        # Confidence.value
      },
      "confidence": str,
      "status": str,
      "opt_status": str,
      "generation_confidence": str,
      "evidence_ids": [sorted str],
      "assumption_ids": [sorted str],
      "dependency_ids": [sorted str],
      "downstream_ids": [sorted str],
      "affected_paths": [sorted str],
      "dependent_analyses": [sorted str],
      "scenario_ids": [sorted str],
      "precedence": int,
      "disabled": bool,
      "comment": str | null,
      "path_selector": {
        "from_set": [sorted str],
        "through_set": [[sorted stage], ...],
        "to_set": [sorted str],
        "edge": "rise"|"fall"|"both"|null,
        "min_max": "min"|"max"|"both",
        "setup_hold": "setup"|"hold"|"both",
        "add_delay": bool,
        "from_clock": [sorted str],
        "to_clock":   [sorted str],
        "through_clock": [sorted str],
        "scenario": str | null
      } | null
    },
    ...
  },

  "assumptions": {
    "assumptions": [ {
        "id": str,
        "statement": str,
        "origin": str,
        "evidence": [ ... Evidence records ... ],
        "confidence": str,
        "severity": "REQUIRED"|"RECOMMENDED"|"INFO"|str,
        "user_confirmed": bool,
        "fixed": bool,
        "default_value": <jsonable>,
        "current_value": <jsonable>,
        "dependent_constraints": [sorted str],
        "dependent_analyses":    [sorted str],
        "created_at": ISO8601,
        "notes": str
      }, ... sorted by id ],
    "next_id": int
  } | null
}
```

### Restoration integrity policy

`ConstraintSet.from_snapshot_dict(snap, *, repair_reverse_edges=False,
allow_cycles=True)` enforces the following invariants on the
dependency graph before returning a ConstraintSet:

1. For every constraint `C`, every id in `C.dependency_ids` must
   exist in the snapshot.
2. For every constraint `C`, every id in `C.downstream_ids` must
   exist in the snapshot.
3. No constraint may list itself in `dependency_ids` or
   `downstream_ids` (self-dependencies are rejected).
4. **Reverse-edge consistency**: for every `dep ∈ C.dependency_ids`,
   `C.id ∈ constraints[dep].downstream_ids`; and vice versa.
5. Cycles are reported as warnings and (by default, `allow_cycles=True`)
   permitted; pass `allow_cycles=False` to reject them.

On violation:

* Default (`repair_reverse_edges=False`): `SnapshotFormatError` is
  raised with a `details` list describing every problem (code,
  subject, affected ids, conflicting edge sets). **No ConstraintSet
  is returned and the caller's `snap` dict is NOT mutated** (the
  loader deep-copies all nested structures before modifying
  anything).
* Explicit opt-in repair (`repair_reverse_edges=True`): reverse
  edges are rebuilt from forward edges. Each repair is recorded on
  the returned ConstraintSet in `.snapshot_repairs` (a list of
  `SnapshotRepairRecord`) with `code="REVERSE_EDGE_REPAIRED"`, the
  affected constraint id, and the before/after edge lists, so the
  persisted inconsistency remains auditable. Post-repair
  verification confirms zero mismatches remain; otherwise
  `SnapshotFormatError` is raised.

Missing references and self-dependencies are always fatal — they
cannot be safely guessed, and the loader never silently deletes
them.

### Normalization rules

* Enum fields (`source_kind`, `confidence`, `status`, `opt_status`,
  `type`, `generation_confidence`, PathSelector enums) are stored as
  their `.value` strings; on restoration they are converted back to the
  corresponding enum members.
* String-valued enum inputs are accepted and coerced to the canonical
  enum at construction (`_normalize` validators on `Constraint`,
  `ProvenanceRecord`, `Evidence`, `Assumption`). The string `"IMPORT"`
  is coerced to `SourceKind.EXISTING_SDC` for compatibility.
* Numeric lists that are sets semantically (targets, sources,
  clock_refs, scenario_ids, evidence_ids, assumption_ids,
  dependency_ids, downstream_ids, affected_paths, dependent_analyses,
  `from_set`, `to_set`, clock filters, evidence `source_objects`) are
  sorted before serialization; order is restored deterministically and
  is not semantically meaningful. `through_set` preserves stage order
  but sorts each stage.
* Time-like `values` entries (`*_seconds`, `*_delay`, `*_period`,
  `*_latency`, `*_uncertainty`) are serialized as floats with
  15-significant-digit rounding to absorb float-noise; `semantic_key()`
  treats `10ns` and `10000ps` as identical (when parsed through
  `parse_time_string`).
* Dictionary keys are recursively sorted; nested dicts/lists are deep
  copied so serialization cannot mutate caller state.
* Unknown top-level constraint fields encountered during restoration
  with `unknown_field_policy="error"` raise `ValueError`. With the
  default `"keep"` policy they are preserved in an `_extensions`
  sub-dict for forward-compatibility.
* `from_snapshot_dict()` calls `_repair_reverse_edges()` which rebuilds
  `downstream_ids` from `dependency_ids` and reports mismatches via
  `validate()` if they remain.
* Future `schema_version` values will be refused with a clear error
  until a migration path is implemented; older versions will be
  migrated.

### Invalidation is preserved

After a round-trip, `stale_set()` returns the same stale sets as the
original UCM: changing an assumption's `current_value` in the restored
ledger invalidates the same downstream constraints and analyses.
