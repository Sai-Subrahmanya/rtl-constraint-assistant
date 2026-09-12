# Step 39 — Hierarchy, Domain, Scenario and Evidence-Aware Validation

Step 39 is delivered by the existing layered validator rather than a parallel
checker. Its deterministic pipeline performs design/object reference checks,
semantic and conflict checks, exception safety, scenario registration,
completeness, backend capability and graph-aware coverage. The timing graph
supplies structural domain edges and coverage; leaf/hierarchical naming
comparison is used only where documented, never to invent an object.

Findings preserve typed evidence, source provenance, stable issue identity and
single-scenario identity. Multi-scenario findings are not falsely assigned to
one scenario. Unknown/inactive scenario identifiers and uncovered graph scope
remain explicit. Formal-backed exception checking reports `UNRESOLVED`,
`INVALID`, or backend failure rather than declaring timing exceptions safe.

Coverage is evidence about known graph scope—not EDA signoff. A coverage
percentage is not silently treated as complete when graph evidence is absent or
unknown.
