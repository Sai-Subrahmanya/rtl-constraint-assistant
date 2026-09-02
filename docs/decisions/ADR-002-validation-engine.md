# ADR-002: Strengthen the Existing Validation Engine (Step 13)

## Context
Step 13 requires a robust constraint validation engine (severity/category/
code taxonomy, reference integrity, constraint-type semantic checks,
conflicts, overlap/shadowing, coverage, completeness, exception safety,
scenario-aware validation, SDC-import classification, backend hooks, and
provenance). A natural but risky implementation would create a second,
parallel validation model. That would break determinism, provenance, and
the existing CLI/reporting contract.

## Decision
**Extend the existing `ValidationIssue` / `ValidationReport` / `ValidationResult`
model and the existing six-layer engine.** There is one validation data
model, and every new layer (completeness, SDC-import classification,
ref-kind consistency, value/unit/range semantics, precedence conflicts,
exception verification, scenario coherence) writes into that same model.

The engine is *observational*: it never mutates the `ConstraintSet`,
`Design`, or `TimingGraph`, and never rewrites a constraint value.

## Alternatives considered
- **New `*Step13*` issue/report classes**: rejected — duplicates the model,
  breaks `issue_id` determinism and the `as_dict()`/`summary()` contract.
- **Re-writing values to fix findings**: rejected — "no silent repair"
  policy; findings only report, remediation is a suggestion.

## Reason
- Keeps one contract for the CLI (`rca validate`, `rca coverage`), the
  report, and tests.
- `issue_id` stays deterministic and provenance (`source_kind`, `origin`,
  `resolution_status`) attaches cleanly.
- Reuses the existing exception analyzer/verifier and SDC parser/importer —
  no duplication.

## Consequences
- New `ErrorCode` values were added to `rca.utils.enums` (additive; existing
  codes unchanged).
- `ValidationIssue` gained optional provenance fields (`source_kind`,
  `origin`, `assumption_ids`, `resolution_status`) — all defaulted so the
  existing Step-7 `ValidationIssue` construction remains compatible.
- `ValidationReport` gained `completeness_summary`.
- `ValidationResult.as_dict()` and `ValidationReport.summary()` now include
  `completeness_summary`.
- `CONFLICT_PRECEDENCE` / `CONFLICT_USER_VS_INFERENCE` report but never
  silently resolve a conflict.
