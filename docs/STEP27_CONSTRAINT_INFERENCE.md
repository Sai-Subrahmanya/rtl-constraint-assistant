# Step 27 — Constraint Inference & Intent Completion Engine

Step 27 adds a conservative **advisory** layer to the established inference
engine. It does not add a second UCM, validator, provenance format, or timing
intent engine.

## Public API

```python
from rca.inference import InferenceEngine

engine = InferenceEngine()
report = engine.infer_candidates(design, timing_graph, config, existing_ucm)
# report.candidates are advisory and existing_ucm is unchanged

result = engine.accept_candidate(report.candidates[0], existing_ucm, design, timing_graph)
```

`InferenceEngine.run()` remains the legacy materialization API for existing
generate, validation, and optimizer paths. `infer_candidates()` is the
non-mutating API used by `rca infer`.

## Facts are not timing intent

The report separates direct structural observations (`structural_facts`) from
proposed timing intent (`candidates`). Facts can say that a port drives
sequential registers or that a CDC path was observed. They do **not** assert a
clock period, I/O budget, generated-clock ratio, clock relationship, false
path, multicycle path, or asynchrony.

Candidates have separate `status` and `decision` axes:

| Status | Meaning |
| --- | --- |
| `INFERRED` | A complete, structurally supported UCM template is available. |
| `CONFIRMATION_REQUIRED` | Structural candidate requires explicit human intent. |
| `INSUFFICIENT_EVIDENCE` | Required data is absent; no value was guessed. |
| `AMBIGUOUS` | Multiple hypotheses are preserved; ordering chooses none. |
| `UNSUPPORTED` | The available semantics cannot safely be compared or materialized. |
| `CONFLICTING` | Existing UCM or advisory knowledge disagrees; both are reported. |
| `REJECTED` | Candidate cannot be acted on. |

The status is not a UCM lifecycle status, evidence confidence, validation
outcome, or knowledge trust level. `decision` is independently one of
`ACCEPTABLE`, `ALREADY_PRESENT`, `REQUIRES_CONFIRMATION`, or `REJECTED`.

## Acceptance safety

Only `INFERRED` plus `ACCEPTABLE` candidates with complete canonical templates
can be accepted. Acceptance requires the original design and timing graph, then
checks their source snapshot identity plus the UCM snapshot, unsupported
semantics, required dependency and assumption links, semantic duplicates,
conflicts, and the established validation pipeline before changing the caller
UCM. It never modifies, deletes, or repairs an existing constraint.
A successful explicit acceptance stores the original evidence, provenance,
assumptions, candidate warnings, and knowledge references in the canonical UCM
and its metadata; the accepted constraint is marked `CONFIRMED` while retaining
its actual inference source.

Candidates that reflect an explicit user/imported declaration are reported as
`ALREADY_PRESENT`, rather than being promoted as a new inferred intent.

## Knowledge boundary

A Step-26 `KnowledgeEngine` can supply deterministic explanatory references.
Knowledge may categorize or strengthen a structurally supported candidate but
never creates one by itself, supplies missing values, or enters UCM implicitly.
A same-scope/different-values knowledge result is retained as a conflict and
cannot be accepted.

## CLI

```bash
rca infer project.yaml
rca infer project.yaml --json
```

The CLI does not write an SDC or add a candidate to UCM. JSON is deterministic
and separates `structural_facts`, `candidates`, `ambiguities`, `conflicts`, and
`missing_information`. Candidate serialization always records
`acceptance_state: "NOT_ACCEPTED"`.

## Known intentional limits

Step 27 intentionally does not infer numeric timing values or signoff intent.
Generated/gated/mux clocks, ambiguous I/O associations, CDC relationships,
false paths, and multicycle paths require explicit information or the existing
formal/validation processes. Advisory candidates are excluded from coverage
until explicit acceptance has added an ordinary canonical UCM constraint.
