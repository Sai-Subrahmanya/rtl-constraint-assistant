# ADR-001: Universal Constraint Model (UCM)

## Context
SDC is a vendor-specific Tcl dialect. Different tools (Synopsys PrimeTime,
Cadence Tempus, OpenSTA) share the core command set but diverge on options,
unit conventions, and extensions. If RCA generated SDC text directly, it
would become tightly coupled to one backend and would be unable to compare
constraints semantically across vendors or revisions.

## Decision
RCA operates on a **vendor-independent Universal Constraint Model**
(`rca.constraint_model.ConstraintSet`). Constraints are strongly typed
objects with provenance, evidence, assumptions, and confidence metadata.
SDC is a rendering produced by a pluggable backend (`rca.sdc.SDCBackend`).

## Alternatives considered
- **Direct SDC strings**: fastest to implement; impossible to validate, diff
  semantically, or target multiple backends.
- **Vendor-specific models + translators**: N×M complexity.

## Reason
- Allows semantic comparison, validation, optimization, and equivalence
  without touching parser logic.
- Backend capabilities can be negotiated (§52).
- Provenance and assumption tracking attach naturally.

## Consequences
- Renderer code must exist per backend.
- Import (SDC→UCM) must also exist.
- New constraint types require updating the model, then all backends.
