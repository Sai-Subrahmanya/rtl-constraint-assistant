# Step 36 — Assisted Constraint Workflow

The user-facing assisted workflow is built from existing RCA intelligence, not
a new reasoning engine. `rca run CONFIG [--ucm SNAPSHOT] [--review REVIEW]
[--release-package PACKAGE] --json` emits a deterministic lifecycle view and
its human output lists stage status and the next explicit action.

It exposes design analysis, offline knowledge availability, advisory inference
and missing information, explicit application receipts, canonical UCM,
validation/coverage/MCMM, readiness, lineage, review, release, package
verification, handoff, EDA, and formal state. The report intentionally says
`CONFIRMATION_REQUIRED`, `REVIEW_REQUIRED`, `RELEASE_BLOCKED`,
`HANDOFF_BLOCKED`, or `EDA_UNAVAILABLE` instead of silently proceeding.

The command is presentation/coordinator only. Candidate application remains
`rca apply`; review decisions remain `rca review`; release remains `rca
release`; handoff remains `rca handoff`; actual tool work remains the explicit
EDA/formal flows. It never turns suggestions into constraints, treats unknown
as success, or claims EDA execution.
