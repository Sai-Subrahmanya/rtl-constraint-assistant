# Step 34 — Complete E2E Pipeline Orchestration

`rca.workflow` provides a **composition-only** projection of the existing RCA
lifecycle. `build_complete_workflow(cset, inputs=WorkflowInputs(...))` accepts
existing analysis, knowledge, inference, application, validation, readiness,
lineage, review, release, package-verification, handoff, EDA-manifest, and
formal-result outputs. It neither reimplements nor mutates any authority.

The deterministic stage projection covers analysis → knowledge → inference →
explicit application → canonical UCM → validation/coverage/MCMM → readiness →
lineage → review → explicit release → package verification → handoff → EDA and
formal evidence. Missing, failed, pending, stale, blocked, and unavailable
inputs remain explicit. In particular, missing review is `REVIEW_REQUIRED`,
release eligibility remains an explicit human action, and absent actual EDA
manifest evidence is `EDA_UNAVAILABLE`; the coordinator cannot manufacture a
later success.

`rca run CONFIG --json` is the user-facing read-only orchestration command. It
uses the existing parser, advisory inference, validation, readiness, and
lineage services in memory. An optional existing UCM/review/release-package can
be supplied. It does **not** apply candidates, make a review decision, release,
write UCM/SDC/history, run EDA/formal, or turn an unavailable tool into a mock
claim. Use each owning explicit command for those transitions.

The integration E2E test composes existing release/package/handoff evidence and
checks deterministic JSON, no UCM mutation, MCMM scope preservation, explicit
release/handoff state, and the honest unavailable-EDA stage.
