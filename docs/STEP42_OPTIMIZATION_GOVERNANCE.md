# Step 42 — Optimization Governance

RCA’s existing optimizer, MCMM evaluator, Pareto frontier, candidate execution
ledger, artifact/manifest cache and SQLite QoR query store remain the
optimization architecture. Optimization evaluates canonical candidate UCM
snapshots under preserved scenario identity; it does not apply inferred
candidates, approve a review, create a release, or equate QoR improvement with
constraint validity.

Candidate and MCMM identities bind canonical UCM semantics, scenario context,
backend/tool/cache inputs and retained execution evidence. Cache hits require
existing manifest/artifact integrity. Mock evidence is explicitly marked and
is excluded from real-evidence best-QoR queries unless the operator opts in.

Human review/release use their existing policy gates; optimizer output is
advisory evidence for those gates, never a bypass.
