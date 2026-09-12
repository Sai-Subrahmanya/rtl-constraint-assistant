# Step 33 — Controlled Downstream Constraint Handoff

Step 33 adds `rca.handoff`: a read-only, deterministic target projection over
an existing verified Step-32 release package. It is not another UCM, release
record, artifact/cache authority, SDC renderer, EDA runner, or signoff system.

## Boundary

```text
verified release package -> handoff assessment -> explicit preparation -> target boundary
```

The release package remains the input authority. Handoff validates package
integrity through `verify_release_package`, the released record, canonical and
semantic UCM identities, exact MCMM scope, package artifact hashes, selected
target/dialect compatibility, and a supplied configuration identity. It never
modifies UCM/review/release/package records, broadens or drops scenarios,
generates SDC, or silently repairs package content.

`HandoffTarget` has `GENERIC`, `OPENSTA_OPENROAD`, `SYNOPSYS`, `CADENCE`, and
an explicitly unsupported `FUTURE_VENDOR` placeholder. Generic handoff can
carry a verified package without SDC. Vendor targets require a package SDC that
was supplied before release plus an explicit compatible dialect declaration;
unknown compatibility is a blocker rather than an assumption.

## Explicit states

`PACKAGE_READY`, `HANDOFF_READY`, `HANDOFF_PREPARED`, `HANDOFF_EXECUTED`,
`FAILED`, `UNAVAILABLE`, `UNSUPPORTED`, and `EDA_SIGNOFF_CONFIRMED` are distinct.
The Step-33 preparation path produces only `HANDOFF_PREPARED`. `--execute`
returns `UNAVAILABLE`, with `actual_external_tool_executed: false`, because it
does not secretly invoke a tool. `EDA_SIGNOFF_CONFIRMED` is never created by
handoff preparation and can only be represented by a later actual external
execution with retained evidence.

## API

```python
from rca.handoff import assess_constraint_handoff, prepare_constraint_handoff

assessment = assess_constraint_handoff(
    "artifacts/release-r1", target="OPENSTA_OPENROAD", policy=policy, config=config,
)
prepared = prepare_constraint_handoff(
    "artifacts/release-r1", target="OPENSTA_OPENROAD", policy=policy, config=config,
)
```

The models are frozen: `ConstraintHandoff`, `HandoffAssessment`,
`HandoffResult`, `HandoffIdentity`, `HandoffScope`, artifacts, evidence,
dependencies, issues, target, policy, and statuses. Identity binds package,
release/UCM/semantic identity, exact scope, target, policy, artifact hashes,
and configuration identity, not a package path, wall-clock, host, or worker.
`verify_constraint_handoff` rechecks a saved handoff JSON statelessly against
the package.

## CLI

```bash
# Assessment only: no package or UCM write, no SDC generation, no tool execution.
rca handoff project.yaml --release artifacts/release-r1 --target GENERIC --json

# Explicitly prepare a target projection. Save the deterministic JSON if desired.
rca handoff project.yaml --release artifacts/release-r1 --target OPENSTA_OPENROAD \
  --policy handoff-policy.json --prepare --json > handoff.json

# Verify an already saved handoff against the package.
rca handoff-verify handoff.json --release artifacts/release-r1 --json
```

A policy can require a supplied SDC, exact artifact kinds, verified package,
released record, and configuration identity. It must state `sdc_dialect` for
vendor compatibility. No CLI success denotes EDA, timing, power, formal, or
ASIC signoff; actual external execution remains the explicit EDA boundary.
