# ADR-003: Explicit SymbiYosys Proof Jobs for Timing Exceptions (Step 14)

## Context

RCA’s Step-8 exception analysis is deliberately structural and conservative.
It can identify selector scope, blast radius, and red flags, but cannot prove
that a false path is unsensitizable or that a multicycle timing assumption is
valid. The previous `FormalBackend` interface correctly returned
`UNRESOLVED` by default; the roadmap identified a SymbiYosys adapter as the
next concrete integration.

A tempting implementation would auto-generate an assertion from
`set_false_path` or `set_multicycle_path`. That would be unsound: an SDC
selector does not contain the design-specific temporal property, clock/reset
assumptions, legal-environment assumptions, or mode/corner constraints needed
for a formal proof. It would violate RCA’s no-fabrication and provenance
invariants.

## Decision

Add `SymbiYosysFormalBackend`, a concrete subclass of the existing
`FormalBackend` interface. The backend runs an **explicit user-authored** `.sby`
job mapped to a UCM exception ID and expected exception kind.

- Only SymbiYosys `PASS` plus process exit code `0` produces `VERIFIED`.
- `FAIL` produces `INVALID` and preserves a bounded list of counterexample
  artifacts.
- Missing mapping/job/tool, timeout, `UNKNOWN`, and indeterminate output
  produce `UNRESOLVED`.
- Contradictory status markers, a PASS/non-zero discrepancy, or execution error
  produce `ERROR`; validation reports this as blocking.
- The adapter records the source job/task, job hash, stable run ID, output
  location, argv, version, status markers, bounded diagnostic tails, and
  scenario applicability passed from the UCM.

The optional top-level `formal:` project configuration selects the backend.
The default remains `conservative`, so existing users and library callers
continue to receive the established unverified-safe behavior. The CLI builds
and passes this backend through the existing `run_validation()` pipeline; no
new validation data model or UCM field is added.

## Alternatives considered

1. **Generate `.sby` files/properties from SDC selectors.** Rejected: it would
   invent formal intent and allow a misleading proof claim.
2. **Store SymbiYosys details in UCM constraints.** Rejected: UCM must remain
   a vendor-neutral source of timing intent. Tool job paths/tasks belong in
   project configuration and proof provenance.
3. **Treat successful process exit as a proof.** Rejected: an SBY run can exit
   successfully without a clear PASS, and `expect` settings can alter expected
   non-zero outcomes. RCA requires an unambiguous PASS marker plus zero exit.
4. **Create a Step-14 validation report type.** Rejected: this duplicates the
   Step-13 `ValidationIssue`/`ValidationReport` architecture and would split
   provenance and CLI behavior.

## Consequences

- Users who want a proof must author and review formal collateral and map it to
  the exact current UCM exception ID.
- `formal.backend: symbiyosys` may run external proof tools during `validate`,
  `report`, or `coverage`; the default never does.
- Proof results are reproducibly identifiable but real elapsed time and tool
  diagnostics naturally vary by environment.
- MCMM scenario membership is retained as proof-input provenance. RCA does not
  assume that one generic property proves every scenario; per-mode/corner
  setup remains explicit in the mapped job/task.
