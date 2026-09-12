# Step 43 — Reproducibility, Regression Identity and Replay Evidence

`rca.reproducibility.assess_replay_evidence()` is a read-only composition of
canonical UCM identity, portable `ProjectConfig.engineering_dict()`, scenario
scope, retained EDA manifest hashes, bound formal results and package
verification. It emits stable `RPE-…` identities and component statuses without
writing history, tools, UCM or artifacts.

`rca replay-evidence CONFIG [--ucm SNAPSHOT] [--manifest RUN_MANIFEST]
[--release-package PACKAGE] --json` exposes the same projection. It explicitly
sets `automatic_replay_supported: false`: this is an investigation/reproduction
plan, never a request to execute a replay or a claim of equivalent results.

A manifest with available changed artifact bytes is `ARTIFACT_INTEGRITY_INVALID`.
A formal result without a current canonical binding is
`FORMAL_EVIDENCE_STALE`. Moved/unavailable locators retain identity evidence
but do not become validated files. Identity excludes timestamps, project-root
absolute paths and host-specific run/cwd locations; content hashes and existing
lifecycle identifiers remain retained.
