# Step 37 — Evidence-Driven Advanced Inference

Step 37 extends the existing Step-27 `InferenceEngine`; it does not make a
second inference path or UCM writer. Existing structural rules keep detecting
clock roles, resets, generated-clock/gating/mux intent, I/O association and
clock-domain relationships. Candidates retain source snapshots, rule evidence,
canonical semantic template identity, scenario links and knowledge references.

When structural facts have multiple plausible interpretations, candidate JSON
now also contains deterministic `hypotheses`. Each is explicitly
`UNCONFIRMED`; the candidate remains `AMBIGUOUS` and `REJECTED` until an
operator supplies an explicit decision. Hypotheses are not probability scores,
constraints, selected clock relationships, or invented numeric timing values.
`explain_inference_candidate()` shows them alongside missing information.

Inference still has no side effect on UCM. Only the existing explicit
application receipt path can attempt a candidate-to-UCM transition, and it
continues to validate staleness, conflicts, semantics and scenario scope.
