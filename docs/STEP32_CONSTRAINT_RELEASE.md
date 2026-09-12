# Step 32 — Constraint Release Baseline & Reproducible Signoff Package

## Purpose and boundary

Step 32 adds a deterministic, vendor-neutral **RCA constraint-release** layer
around one exact, already-reviewed canonical `ConstraintSet` (UCM) snapshot.
It is deliberately a governance and evidence-packaging boundary, not a second
constraint authority.

The governing separation remains:

```text
INFER != APPLY != VALIDATE != APPROVE != RELEASE != external EDA signoff
```

In particular, an RCA release is **not** an STA, physical, commercial, ASIC, or
foundry signoff. RCA never upgrades an external signoff status. An actual
external tool result may be supplied as an explicit referenced artifact, but
no EDA, timing analysis, physical flow, or formal proof is executed by this
layer.

## Authority and non-effects

| Concern | Step-32 behavior |
| --- | --- |
| Constraints | Reads exactly one existing canonical UCM. It never makes, repairs, applies, or substitutes constraints. |
| Review | Consumes an explicit existing Step-31 `ConstraintReview` or read-only assessment. It never creates, approves, rejects, defers, revokes, or bypasses review. |
| Readiness, validation, coverage, lineage, formal | References existing evidence; absent, stale, unresolved, conflicting, or unsupported required evidence fails closed. |
| SDC | Includes only an explicit pre-existing supplied file. A release request never generates SDC. |
| Artifact/cache/SQLite authority | Uses `ArtifactManager` only to atomically write an explicitly requested portable package. Existing run manifests, filesystem evidence, integrity caches, and SQLite history retain their established authority. |
| Time and identity | No wall-clock value, random UUID, machine path, or worker value contributes to release/package identity. Optional human action timestamps are display/audit fields only. |

`ConstraintRelease`, its snapshots, policies, issues, evidence, dependencies,
decisions, packages, and verification results are frozen typed records. A
release action, revocation, and supersession always return a distinct immutable
successor; earlier records are not edited.

## Release identity and scope

A release identity binds:

- canonical UCM content hash (`stable_hash_cset`) and the existing Step-9
  normalized semantic identity;
- exact project/configuration, design, and timing-graph identities when
  supplied;
- the Step-31 review identity and reviewed UCM identity;
- readiness, validation, coverage, formal, and Step-30 lineage identities;
- exact active/requested/released MCMM scenario scope and scenario-definition
  identity; and
- SHA-256 hashes of explicitly supplied SDC and package artifacts.

The scope is always one of `GLOBAL`, `SELECTED_SCENARIOS`, or
`ALL_ACTIVE_SCENARIOS`. The record retains the requested, active, and released
scenario IDs. Unknown scenario IDs, a selected-plus-all-active conflict, and
an all-active-policy shortfall are blockers—there is no silent broadening or
narrowing. With a config, the existing MCMM matrix supplies active IDs; without
one, canonical UCM scenarios are used. The legacy configured `S000` scope is
also retained when the matrix supplies it.

If the UCM contains Step-9 unsupported semantic options, the semantic identity
is explicitly empty/unknown. RCA still binds exact content, but cannot claim
semantic equivalence; conservative policy records a warning rather than
inventing an equivalence.

## Policy and assessment

`ReleasePolicy` is conservative by default:

- explicit current project/configuration, design, and timing-graph identities are required;
- explicit approved review is required;
- `READY` readiness, validation evidence, complete coverage, and lineage are
  required;
- all active scenarios are required;
- review/validation/readiness warnings are not automatically accepted;
- required formal classes, SDC, and named artifact kinds are explicit policy
  choices; and
- only explicitly listed unresolved-evidence categories may be downgraded to
  warnings.

`assess_constraint_release(...)` is read-only. It returns a
`ReleaseAssessment` with currentness, policy findings, blockers/warnings, and
whether an explicit `RELEASE` is possible. It never promotes a candidate.

Review states remain distinct. `APPROVED` may satisfy the review gate;
`APPROVED_WITH_WARNINGS` needs explicit policy and generally yields a release
warning. `NEEDS_REVIEW`/pending, `REJECTED`, `DEFERRED`, `INVALID`,
`INCOMPLETE`, `UNKNOWN`, `BLOCKED`, `STALE`, and `REVOKED` fail the gate.
The release layer neither reinterprets these as approval nor makes a review
current by repairing it.

## Explicit lifecycle API

The stateless public API is in `rca.release`:

```python
from rca.release import (
    ReleasePolicy,
    assess_constraint_release,
    create_release_candidate,
    release_constraint_set,
    create_release_package,
    revoke_release,
    supersede_release,
    verify_release_package,
)

candidate = create_release_candidate(
    reviewed_ucm,
    policy=ReleasePolicy(),
    review=approved_step31_review,
    readiness=existing_readiness,
    validation=existing_validation,
    lineage=existing_lineage,
)
assessment = assess_constraint_release(reviewed_ucm, release=candidate)
# Only an explicit caller decision can do this. It is not automatic.
released = release_constraint_set(candidate, reviewed_ucm, actor=release_actor)
```

`release_constraint_set` rejects stale, revoked, blocked, unresolved, or
warning-bearing candidates unless their recorded policy explicitly permits the
applicable warning release. `revoke_release` only revokes an explicit released
record and returns a successor. `supersede_release` makes a fresh candidate
that references the older release; it never changes historical release state.

## Reproducible package and verification

`create_release_package(released, ucm, output_dir, ...)` is separately
explicit. It writes these reproducible content-addressed members as applicable:

- `release_manifest.json` descriptor;
- `release.json` immutable release record and policy;
- `ucm_snapshot.json` canonical UCM snapshot;
- supplied review, readiness, validation, coverage, lineage, and formal
  evidence snapshots;
- an explicitly supplied `sdc/...` member; and
- explicitly supplied `artifacts/...` members.

The descriptor lists relative paths, artifact kinds, SHA-256 hashes, byte
sizes, requiredness, release identity, UCM/semantic identities, and exact
scope. It intentionally does not place its own recursive hash in the artifact
list. When a release snapshot has an identity-bound evidence source, packaging
requires its corresponding supplied snapshot; when policy requires SDC or an
artifact kind, packaging requires it as well.

`verify_release_package(path)` is stateless and read-only. It verifies safe
relative paths, required members, all listed hash/size pairs, descriptor
identity, release/package consistency, canonical UCM reconstruction/content and
semantic identities, review/readiness/validation/coverage/lineage/formal
identity-bound members, exact scope, and released/revoked state. It does not
execute EDA/formal, mutate a package, rewrite a manifest, regenerate a file, or
repair corrupt content. Any discrepancy produces `INVALID` findings.

## CLI

The command surface is deterministic JSON when `--json` is selected:

```bash
# Read-only assessment. The review is a pre-existing, explicit Step-31 record.
rca release project.yaml --ucm reviewed-ucm.json --review approved-review.json --json

# Explicit lifecycle action, with an optional explicit package directory.
rca release project.yaml --ucm reviewed-ucm.json --review approved-review.json \
  --decision RELEASE --releaser "release-owner" --comment "baseline R1" \
  --package-dir artifacts/release-r1 --json

# Add only already-existing SDC/artifacts; this never generates either one.
rca release project.yaml --ucm reviewed-ucm.json --review approved-review.json \
  --sdc existing.sdc --artifact timing-report=existing.rpt --json

# Stateless integrity/currentness verification of an existing package.
rca release-verify artifacts/release-r1 --json
```

`--decision` accepts only explicit `RELEASE` or `REVOKE`; omitting it cannot
auto-release. `--scenario` selects exact scenarios, `--all-active-scenarios`
selects the full active MCMM set, and the two cannot be combined. `--policy`
loads an explicit `ReleasePolicy` JSON object. `--release-record` reassesses a
prior release under its recorded policy; `--supersede` creates a successor
candidate. Each `--artifact` must be an unambiguous `KIND=PATH` pair.

CLI evidence derivation uses existing in-memory validation/readiness/lineage
projections only to assess currentness; it does not write UCM, SDC, artifacts,
history, SQLite, cache, review, or external-tool results unless
`--package-dir` was explicitly selected for the portable package.

## Test coverage

Step 32 adds focused immutable-model, policy, scope, warning, missing evidence,
formal-class, SDC, supplied-artifact, staleness, lifecycle, deterministic JSON,
package integrity, corruption, path traversal, CLI, and E2E tests. The golden
release projection contains 32 named deterministic policy/lifecycle/package
cases, including corrupted-package verification and explicit no-EDA-signoff
status.
