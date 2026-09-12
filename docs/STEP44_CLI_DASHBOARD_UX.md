# Step 44 — Coherent CLI and Dashboard UX

The CLI preserves the governed sequence rather than replacing owning commands:
`infer` is advisory; `apply` is explicit; `validate`, `readiness`, `lineage`,
`review`, `release`, `handoff`, `run-sta`, and formal each retain their own
authority. New convenience views are composition-only:

- `rca run CONFIG [--ucm ...] [--review ...] [--release-package ...] --json`
  projects lifecycle state and next action.
- `rca replay-evidence CONFIG [--ucm ...] [--manifest ...]
  [--release-package ...] --json` assesses retained replay identity; it does
  not execute a replay.
- `--report workflow_report.json` or `--report replay_evidence.json` is an
  opt-in presentation artifact. Each filename must remain below the configured
  `flow.output_dir`; traversal and arbitrary names are rejected.

The existing FastAPI dashboard is extended—not replaced—with `/api/workflow`
and `/api/replay-evidence`, rendering these opt-in artifacts when present.
The UI escapes report-derived values before inserting them into HTML. It does
not invoke CLI actions or alter lifecycle state. The existing design,
constraints, validation, coverage and optimization panels remain intact.
