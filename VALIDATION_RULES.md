# Validation Rules — RCA Constraint Validation Engine (Step 13)

This document is the authoritative reference for the RCA constraint
validation engine. It defines the structured result model, the
severity/category/code taxonomy, and the per-layer validation rules that
`rca validate` and `src/rca/validation/` implement.

The engine is *observational*: validation never mutates the
`ConstraintSet`, `Design`, or `TimingGraph`. It reports findings on the
existing `ValidationIssue` / `ValidationReport` model (there is **one**
validation data model — no parallel/duplicate implementation).

---

## 1. Result model

### 1.1 `ValidationIssue`

| Field | Meaning |
|---|---|
| `severity` | `CRITICAL / HIGH / MEDIUM / LOW / INFO / ERROR` |
| `category` | `MODEL / REFERENCE / CLOCK / TIMING / CONFLICT / OVERLAP / COVERAGE / EXCEPTION / SCENARIO / BACKEND / SYNTAX / COMPLETENESS / PROVENANCE` |
| `code` | stable `ErrorCode` (see §3) |
| `message` | human-readable finding |
| `constraint_id` | offending constraint (if any) |
| `related_constraint_ids` | other constraints involved (conflicts/redundancy) |
| `object_names` | affected ports/pins/nets/cells/registers/clocks |
| `scenario_id` | scenario the finding belongs to (never collapsed) |
| `evidence` | structured proof/context (`ref`, `role`, `available`, ...) |
| `suggestion` | what to fix |
| `blocking` | whether the finding gates PASS → BLOCKED |
| `source_location` | `{file, line}` when available |
| `source_kind` | provenance: `RTL / USER / EXISTING_SDC / LIBRARY / TOOL / INFERENCE / DERIVED` |
| `origin` | rule/inference origin (e.g. `CLK-001`) or `constraint:<category>` |
| `assumption_ids` | assumptions the finding depends on |
| `resolution_status` | `RESOLVED / UNKNOWN / UNRESOLVED / REQUIRES_USER_INPUT` |

`issue_id` is deterministic: derived from
`(category, code, constraint_id, related, objects, scenario_id, message,
source_kind, origin, resolution_status)` and hashed to `V<8 hex>`.

### 1.2 `ValidationReport` / `ValidationResult`

- `ValidationReport` collects `issues`, `checks_run`, and per-layer
  summaries (`conflict_summary`, `overlap_summary`, `reference_summary`,
  `exception_summary`, `scenario_summary`, `completeness_summary`).
- `ValidationResult` exposes `status` (`PASS / PASS_WITH_WARNINGS /
  BLOCKED / ERROR`), `errors()`, `warnings()`, `infos()`, `blocking`,
  `coverage`, and `as_dict()`.

### 1.3 Blocking policy

`CRITICAL / HIGH / ERROR` block by default. `WARNING / MEDIUM / LOW /
INFO` do not. A caller may override `blocking` explicitly.

---

## 2. Deterministic, ordered pipeline

```
references → semantic → conflicts → exceptions+scenarios → completeness
→ backend → coverage → sdc_import(optional) → hydrate_provenance
```

Every layer appends a marker to `checks_run`. Inputs are never mutated.

---

## 3. Rule taxonomy (by `ErrorCode`)

### 3.1 Reference integrity (`REFERENCE`)

- `REF_UNKNOWN` — a referenced port/pin/net/cell/register/clock is not in
  the design/clock index. When the design is unavailable the finding is
  `UNRESOLVED`; when the design is available it is `RESOLVED` (a known
  miss). Validity is never invented.
- `REF_KIND_INCONSISTENT` — the collection kind conflicts with the
  constraint type (e.g. `set_input_delay` on a `pin`).
- `REF_UNSUPPORTED_SELECTOR` — unresolved selector expression.
- `REF_EMPTY_SELECTOR` — a target list is empty for a constraint that
  requires one.

### 3.2 Semantic (`CLOCK` / `TIMING`)

Clocks: `CLOCK_PERIOD_MISSING`, `CLOCK_PERIOD_INVALID`,
`CLOCK_WAVEFORM_INVALID`, `CLOCK_WAVEFORM_INCOHERENT`, `CLOCK_MULTIPLE`.
Generated clocks: `GCLK_*`. I/O: `IO_CLOCK_UNKNOWN`, `IO_DELAY_INVALID`,
`IO_MIN_MAX_INCOHERENT`, `IO_WRONG_DIRECTION`, `IO_DUPLICATE`.
Value/unit/range checks (additive in Step 13): `CLOCK_UNCERTAINTY_INVALID`,
`CLOCK_LATENCY_INVALID`, `CLOCK_TRANSITION_INVALID`, `IO_TRANSITION_INVALID`,
`LOAD_INVALID`, `DRIVING_CELL_INVALID`, `DESIGN_RULE_INVALID`,
`MINMAX_DELAY_INVALID`, `SEMANTIC_INCOMPATIBLE_OPTION`.

Rule: required values must be present, finite, and within a sane range;
contradictory/illegal combinations are rejected; **no silent repair** —
the value is only reported.

### 3.3 Conflicts (`CONFLICT`)

`CONFLICT_CLOCK_PERIOD`, `CONFLICT_IO_DELAY`, `CONFLICT_LATENCY`,
`CONFLICT_UNCERTAINTY`, `CONFLICT_MINMAX_DELAY`, `GROUPS_CONTRADICTORY_RELATIONSHIP`,
`CONFLICT_USER_VS_INFERENCE`, `CONFLICT_EXCEPTION`, `CONFLICT_PRECEDENCE`.

Precedence order (highest wins) used to *report*, never to auto-resolve:
1. explicit fixed user intent (`FIXED` / user `CONFIRMED`)
2. user/`USER` source
3. project config / trusted imported (`EXISTING_SDC`)
4. tool/library (`LIBRARY`, `TOOL`, `PHYSICAL_DATA`)
5. strong RTL inference (`RTL`)
6. weak heuristic (`INFERENCE`, `DERIVED`)

Equal/near-equal conflicts are reported, not silently dropped; the winner
is recorded in `evidence`.

### 3.4 Overlap / shadowing (`OVERLAP`)

`OVERLAP_DUPLICATE`, `OVERLAP_REDUNDANT`, `OVERLAP_SHADOWED`,
`OVERLAP_OVERLAPPING`, `EXCEPTION_BROAD`. Broad exceptions with no
`-from/-to/-through` are always flagged; a narrower exception fully
subsumed by a broader one is `OVERLAP_REDUNDANT`.

### 3.5 Coverage (`COVERAGE`) & completeness (`COMPLETENESS`)

Coverage computes structural percentages with retained
numerator/denominator (`totals.*`). When the graph is unavailable the
metric is `UNKNOWN`, never a fabricated percentage; `0` objects in a
category is `NOT_APPLICABLE`, not `100%`.

Completeness surfaces missing info (`COMPLETENESS_CLOCK_PERIOD`,
`COMPLETENESS_CLOCK_RELATIONSHIP`, `COMPLETENESS_IO_TIMING`,
`COMPLETENESS_GENERATED_CLOCK`, `COMPLETENESS_ENVIRONMENT`,
`COMPLETENESS_UNRESOLVED`) and classifies each as `REQUIRES_USER_INPUT`,
`UNKNOWN`, or `UNRESOLVED` — never as valid/invalid.

### 3.6 Exception safety (`EXCEPTION`)

`EXCEPTION_BROAD`, `EXCEPTION_NO_EFFECT`, `EXCEPTION_SUSPICIOUS`,
`EXCEPTION_BAD_CYCLES`, `EXCEPTION_SETUP_HOLD_INCOHERENT`,
`EXCEPTION_UNVERIFIED`.

Reusing `src/rca/exceptions` (`verify_exceptions`), the engine records
the formal-verification state. With no formal backend the
`ConservativeFormalBackend` returns `UNRESOLVED`, so the exception is
surfaced as `EXCEPTION_UNVERIFIED` (`resolution_status=UNRESOLVED`). An
exception is **never** concluded safe merely because it improves timing.

### 3.7 Scenarios (`SCENARIO`)

`SCENARIO_UNKNOWN`, `SCENARIO_MISMATCH`, `SCENARIO_UNKNOWN_ID`,
`SCENARIO_CONFLICT`. Empty `scenario_ids` ⇒ applies to all active
scenarios; non-empty ⇒ only the listed active scenarios. Nonexistent or
inactive ids are reported. Scenario-specific issues keep `scenario_id`.

### 3.8 Backend & SDC import

`BACKEND_UNSUPPORTED`, `BACKEND_BLOCKED` (vendor preflight), `SYNTAX_ERROR`,
`SDC_IMPORT_INCOMPLETE`, `SDC_IMPORT_SEMANTIC`. SDC import is validated
after normalization by consuming the importer's existing diagnostics —
the parser is never duplicated. Import is classified
`SYNTAX_INVALID / SEMANTIC_INVALID / INCOMPLETE / COMPLETE / UNRESOLVED`.

---

## 4. Invariants

1. One validation data model only; `ValidationIssue`/`ValidationReport` are extended, not duplicated.
2. `UNKNOWN`/`UNRESOLVED` are never converted to valid/invalid without evidence.
3. Fixed user-intent constraints are never modified.
4. Coverage never fabricates a percentage.
5. Exceptions are conservative; unverified = `UNRESOLVED`.
6. Repeated validation is deterministic (identical `issue_id` order).
7. Backend-specific syntax checks stay behind the backend abstraction.
8. Provenance is derived from the UCM (source kind, rule origin), never invented.
