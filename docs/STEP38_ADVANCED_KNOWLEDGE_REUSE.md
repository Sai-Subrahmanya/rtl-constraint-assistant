# Step 38 — Trust-Preserving Knowledge Reuse

`KnowledgeEngine.index_release_package(PATH)` is a read-only extension of the
existing Step-26 knowledge index. It first invokes the existing Step-32 package
integrity verifier, then restores the retained canonical UCM snapshot only
when the package is intact. Any broken/missing/malformed package becomes a
diagnostic and produces no pattern.

Patterns receive the explicit `RELEASE_PACKAGE` origin and remain
`VALIDATED`, not `VERIFIED`: package integrity and a release record are not an
external proof, timing signoff, or guarantee that a reused intent applies to a
new design. Existing applicability checks still compare UCM semantic form,
objects, clocks and scenario scope. Suggestions remain advisory and their
existing explicit-acceptance flow retains provenance and requires confirmation.

No history database, package, UCM, review, or release record is changed by
indexing.
