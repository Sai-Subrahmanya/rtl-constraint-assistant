# Step 31 — Constraint Review, Approval & Signoff Boundary

> **An RCA review approval is a governance decision about an exact reviewed
> constraint snapshot. It is not timing, STA, physical, or commercial EDA
> signoff.**

## Purpose and authority boundary

Step 31 adds an explicit, deterministic human-review boundary after the
canonical UCM and its existing evidence have been produced:

```text
canonical UCM → validation / coverage → readiness → lineage → review assessment
                                                        ↓
                                             explicit reviewer decision only
```

The review package is not an inference engine, controlled-application path,
validation/coverage/readiness replacement, formal backend, EDA flow, UCM,
provenance store, artifact/manifest authority, cache, or SQLite database. It
only references data already owned by those systems.

| Question | Authoritative source projected by review |
|---|---|
| Current timing intent | canonical `ConstraintSet` / UCM |
| Constraint lifecycle, candidate/application/knowledge links | Step-30 lineage, when supplied |
| Validation and coverage | supplied `ValidationResult` / `CoverageReport` |
| Closure readiness | supplied Step-29 `ConstraintReadinessReport` |
| Formal result | supplied existing verification result(s) |
| MCMM definition/scope | canonical scenarios and existing MCMM matrix |
| External flow evidence | explicit supplied external artifact references only |
| Review decision | immutable Step-31 review record |

No review API mutates a `ConstraintSet`, its constraints/scenarios/provenance,
inference candidate, application receipt, validation report, coverage report,
readiness report, artifact, manifest, cache, or SQLite history. It never
applies a candidate, emits SDC, starts EDA, or runs a formal tool.

## Review versus readiness, validation, lineage, and EDA signoff

- **Readiness** says what existing evidence permits under Step 29. Review
  records a human governance decision that may be made only under the explicit
  Step-31 policy.
- **Validation and coverage** remain validation/coverage evidence. They do not
  become reviewer intent or proof merely because review consumed them.
- **Lineage** remains the Step-30 lifecycle traceability authority. Review
  stores its report/snapshot and candidate/application/event references; it
  does not reconstruct missing lineage.
- **Formal results** remain exactly as supplied. `UNRESOLVED`, `UNKNOWN`, and
  `UNVERIFIED` are never restated as a proof.
- **External EDA signoff** is always separate. Default output is
  `EXTERNAL_EDA_SIGNOFF_UNKNOWN`. Supplying an external artifact reference
  yields only `EXTERNAL_EDA_SIGNOFF_EVIDENCE_SUPPLIED`, not a signoff claim.

Accordingly, use **“RCA review approved”**, **“constraint review approved”**,
and **“reviewed constraint snapshot”**. Do not read `APPROVED` as "timing
signoff completed," "STA signoff completed," or "physical signoff completed."

## Public API

```python
from rca.review import (
    ReviewActor, ReviewPolicy, ReviewDecisionKind,
    assess_constraint_review, create_constraint_review,
    approve_review, approve_review_with_warnings,
    reject_review, defer_review, revoke_review, supersede_review,
)

# Assessment is read-only and does not implicitly approve.
assessment = assess_constraint_review(
    canonical_ucm,
    policy=ReviewPolicy(),
    lineage=lineage_report,            # existing Step-30 report, optional
    readiness=readiness_report,        # existing Step-29 report, optional
    validation=validation_result,      # existing validation/coverage, optional
    formal_results=(formal_result,),   # existing result(s), optional
)
review = assessment.review             # status remains NEEDS_REVIEW

# A decision is a new immutable review-record revision; UCM is unchanged.
approved = approve_review(
    review, canonical_ucm,
    actor=ReviewActor.from_value("reviewer@example.com", role="constraint-owner"),
    comment="Reviewed the supplied evidence.",
)

# A changed UCM causes an old approval to assess as STALE.
current = assess_constraint_review(newer_ucm, review=approved)
new_review = supersede_review(approved, newer_ucm)
```

`ConstraintReviewEngine().assess/create/decide` is a stateless façade. It does
not retain current review state; callers explicitly retain immutable records if
they need historical governance history.

## Typed model and lifecycle

The package exports these frozen records:

- `ConstraintReview`, `ConstraintReviewAssessment`
- `ConstraintReviewStatus`, `ReviewDecisionKind`
- `ReviewPolicy`, `ReviewSnapshot`, `ReviewScope`
- `ReviewActor`, `ReviewApproval`
- `ReviewEvidence`, `ReviewEvidenceRef`, `ReviewFinding`, `ReviewBlocker`
- `ExternalEDASignoffStatus`

Review states are deliberately not collapsed:

| State | Meaning |
|---|---|
| `NEEDS_REVIEW` | Assessment has not received an explicit decision. |
| `APPROVED` | The exact supplied UCM snapshot was explicitly reviewed and approved under the recorded policy. |
| `APPROVED_WITH_WARNINGS` | Explicit policy permitted warning acknowledgement and the reviewer explicitly selected it. |
| `REJECTED` / `DEFERRED` | Explicit governance decision; neither changes engineering state. |
| `STALE` | Assessment found the old reviewed evidence/snapshot is no longer current. |
| `INVALID` | Scope/record is invalid, for example unknown/conflicting scenario selection. |
| `UNKNOWN` | Required supplied evidence is unknown; no approval conclusion is represented. |
| `BLOCKED` / `INCOMPLETE` / `UNSUPPORTED` | Required existing evidence reports that state; this remains an unapproved governance assessment. |
| `REVOKED` | An explicit successor revocation was recorded; the old record remains unchanged. |

There is no auto-approval. `assess_constraint_review()` can say approval is
possible but always creates a record with `NEEDS_REVIEW`. `approve_review()`
and `approve_review_with_warnings()` are the only approval APIs.

Each explicit action creates a `ReviewApproval` with a deterministic `DEC-*`
identity. The returned successor review has a deterministic `REV-*` identity,
links `previous_review_id`, and retains `decision_history`. The original review
object is never changed. `supersede_review()` creates a separate unapproved
review and records `supersedes_review_id`; it does not revise the old review.

`revoke_review()` is explicit and permitted only for an approved record. It
creates a successor with a `REVOKE` action, including both old and new decisions
in its preserved history. A revocation can be recorded after the reviewed
snapshot is stale because it cannot approve anything.

## Exact snapshot identity and staleness

`ReviewSnapshot` captures identities without using wall-clock time as an
identity input:

- existing canonical `stable_hash_cset()` content identity;
- Step-9-normalized UCM semantic identity when all semantics are supported;
- canonical constraint and scenario IDs;
- existing config/design/timing identities if supplied;
- Step-30 lineage snapshot/report identity when supplied;
- supplied readiness, validation, coverage, and formal evidence identities.

The semantic identity is empty (shown as `UNKNOWN`) when Step-9 says a
constraint has unsupported semantics; review never manufactures equivalence.
The content identity still binds the exact UCM snapshot.

When an old review is assessed against current inputs, a difference in UCM
content/semantic identity, scenario IDs/definitions, supplied config/design/
timing identity, supplied readiness identity, or supplied lineage identity
makes its **current** state `STALE`. The historical review record and its
original `APPROVED` decision remain intact. An approval action fails closed on
stale input.

A supplied lineage or readiness report whose own UCM snapshot does not match
the UCM being reviewed is a review blocker. Missing current evidence is never
silently treated as a pass.

## Evidence classification and policy

Review findings are deterministic `BLOCKER`, `WARNING`, or `INFORMATION`:

- blockers prevent either approval decision;
- warnings require `APPROVE_WITH_WARNINGS` and an explicit policy allowance;
- information is recorded but does not independently gate approval.

`ReviewPolicy` makes gates visible and typed. Its conservative defaults require
readiness, a `READY` readiness state, validation evidence, complete coverage,
and all active scenarios; `allow_approval_with_warnings` defaults to false.
Formal proof is **not** universally required. An API caller may explicitly set
`require_formal_for_constraint_types`, for example for selected exception
classes, and then only supplied `VERIFIED`/`PASS`/`PROVEN` results satisfy that
gate. `allowed_unresolved_evidence_categories` explicitly downgrades only the
listed unresolved category to a warning; it never fabricates evidence.

The policy is serialised into every review record. A stored review must be
assessed under its recorded policy; it cannot be silently reinterpreted under a
new one.

## MCMM scope

A review records requested, active, reviewed, and unknown scenario IDs plus a
deterministic scenario-definition identity. It supports:

- `GLOBAL` — review of the global UCM, covering all active scenarios;
- `SELECTED_SCENARIOS` — explicitly limited review; default policy blocks it if
  all active scenarios are required;
- `ALL_ACTIVE_SCENARIOS` — an explicit all-active MCMM review.

An unknown scenario, or contradictory use of selected scenarios with
`all_active_scenarios`, is `INVALID`. A review limited to `FAST` never claims
approval for `SLOW`.

## CLI

```bash
# Read-only assessment: status remains NEEDS_REVIEW without --decision.
rca review project.yaml --ucm reviewed-ucm.json --json

# Explicit review decision. This produces JSON on stdout only; it writes no artifact.
rca review project.yaml --ucm reviewed-ucm.json \
  --decision APPROVE --reviewer "alice@example.com" \
  --comment "Reviewed snapshot" --json

# Explicit warning policy + decision and a selected MCMM scope.
rca review project.yaml --ucm reviewed-ucm.json \
  --policy review-policy.json --allow-warnings \
  --scenario FAST --decision APPROVE_WITH_WARNINGS \
  --reviewer "alice@example.com" --json

# Assess a prior immutable record against a current UCM, or create a superseding review.
rca review project.yaml --ucm current-ucm.json --review old-review.json --json
rca review project.yaml --ucm current-ucm.json --supersede old-review.json --json
```

`--policy` is a JSON object matching `ReviewPolicy`; `--allow-warnings` is an
explicit override that sets only `allow_approval_with_warnings`. With no policy,
the conservative typed defaults are still emitted in JSON. A CLI review builds
in-memory generic validation/readiness/lineage context through their existing
owners, never invokes formal or EDA backends, and never persists that context.
It does not invoke inference or controlled application.

There is intentionally no `--output`: stdout is the only CLI output. A caller
that elects to persist a review record must do so explicitly through the
project's existing artifact conventions; Step 31 creates no database, schema,
manifest, cache, or hidden review-history file.

## Determinism and limitations

`REV-*`, `DEC-*`, `RVE-*`, `RVF-*`, and `RVB-*` identifiers are content hashes
of their governing typed content. Timestamps (`recorded_at` if a caller
supplies one) are display metadata and never identity inputs. Reviewer identity
and comment **are** decision-identity inputs because they are intentional human
review content. Collections serialize in deterministic order, except preserved
`decision_history`, whose explicit append order represents the governance
sequence.

A review is only as complete as the supplied UCM and evidence. It cannot
reconstruct missing lineage, application receipts, external reports, reviewer
identity, or proof. `UNSPECIFIED` reviewers, missing evidence, unsupported
semantics, unresolved formal status, unknown scope, and stale evidence remain
visible. Review does not establish numerical timing correctness, coverage
completion beyond the supplied report, formal proof, QoR, or any external EDA
signoff.
