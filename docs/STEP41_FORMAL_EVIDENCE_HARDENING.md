# Step 41 — Formal Safety, Provenance and Staleness

The existing SymbiYosys adapter still executes only explicit user-authored
`.sby` mappings, argv-only, with bounded timeout and no shell. Only `PASS` plus
a zero exit status is `VERIFIED`; missing tool/job/mapping, timeout, unknown,
ambiguous marker, nonzero PASS, or error stay unresolved/error. A mock backend
is test evidence only.

`verify_exceptions()` now binds each backend result to the current canonical
constraint content with `constraint_semantic_identity` and deterministic
`formal_evidence_identity`. `formal_result_is_current()` rejects a result when
its associated exception changed. The binding does not alter a verdict or
promote proof status. Formal result identity strips host-specific absolute
locations; SymbiYosys run identity uses proof content and portable name, not a
checkout root.

Counterexamples, status markers and observed tool metadata remain evidence;
none is converted into UCM intent, approval, release or EDA signoff.
