# Step 26 — Constraint Knowledge & Reuse Engine

## Purpose and boundary

Step 26 adds an **offline, vendor-neutral advisory lookup service** for known
constraint patterns. It is implemented in `rca.search.knowledge`, extending
the already-reserved `rca.search` package.

It does **not** add a constraint language, SDC parser, cache, database, QoR
model, artifact authority, scheduler, or external/LLM/vector service:

- The Universal Constraint Model (UCM) remains the only canonical constraint
  representation. A `KnowledgePattern` or `KnowledgeSuggestion` is not a UCM
  constraint and cannot be emitted as SDC.
- UCM `Constraint.semantic_key` is retained as a source-identity digest, while
  Step-9 `normalize_constraint`, `semantic_match_key`, and
  `has_unsupported_options` are used for matching. Therefore equivalent timing
  units and order-insensitive fields match semantically; raw SDC text is never
  compared or parsed by this feature.
- `ConstraintSet`, `Constraint`, existing `ProvenanceRecord`, and `Evidence`
  are used for source projection and explicit acceptance. An accepted item is
  a normal UCM constraint with an ordinary generated UCM ID.
- SQLite QoR history remains a read-only historical projection sidecar. It is
  never an authority or cache and cannot by itself make a constraint
  `VERIFIED`.

No knowledge input is executed. The feature performs no Tcl, shell, Python,
dynamic import, expression evaluation, network call, tool execution, or EDA
execution.

## Data model

The public typed API exported from `rca.search` is:

- `KnowledgePattern`: source item, optional copied canonical UCM template,
  explicit applicability requirements, source constraint/set identity,
  provenance/evidence, assumptions, warnings, and trust classification.
- `KnowledgeSuggestion`: deterministic search result. It has `relevance`,
  `rank`, `match_kind`, applicability result, complete provenance/evidence,
  candidate template, matching rationale, and `NOT_ACCEPTED` state.
- `AcceptanceResult`: an explicit attempt to materialise one suggestion in a
  caller-owned `ConstraintSet`.
- `KnowledgeEngine`: in-memory index and search API.

Trust status is deliberately separate from both UCM `Confidence` and ranking:
`VERIFIED`, `VALIDATED`, `OBSERVED`, `USER_PROVIDED`, `UNVERIFIED`, and
`UNKNOWN` are available. Ranking is deterministic relevance only, **not** a
probability, signoff statement, or correctness claim.

Built-in patterns cover primary/generated clocks, I/O budgets, clock
relationships, uncertainty, multicycle paths, and false paths. They are
vendor-neutral guidance with no concrete UCM template; they can never be
accepted. Project UCM projections preserve source provenance and are indexed
without mutating their `ConstraintSet`. History items are indexed only when a
stored snapshot locator resolves to a valid canonical UCM snapshot and, where
present, its stored SHA-256 matches. Missing or malformed history produces a
diagnostic rather than a guessed constraint.

A history row with a retained `validation_errors == 0` observation can be
classified `VALIDATED`; otherwise it is `OBSERVED`. Neither outcome is
`VERIFIED`, and historic existence alone never proves correctness.

## Applicability and matching

Applicability conditions are declarative data: required objects, clocks,
scenarios, constraint types, and evidence. Missing context/evidence returns
`UNKNOWN` or `INSUFFICIENT_EVIDENCE`; an incompatible object/scope returns
`INAPPLICABLE`. The engine does not infer an async relationship, timing budget,
period, exception, or object mapping.

Search order is stable: exact existing semantic normalization (100), same
normalized scope with different values (70, explicitly a disagreement), same
type (35), applicable canonical type guidance without concrete intent (25),
then bounded token relevance; item ID breaks ties. Unsupported or
unresolved semantic options remain `UNKNOWN` and never become an exact match.

`KnowledgeEngine.inference_references(report, cset)` may add only exact,
applicable `VALIDATED`/`VERIFIED` references to an `InferenceReport`'s optional
`knowledge_references` field. It does not change inference result status,
constraints, ambiguity/conflict records, safety checks, or UCM state.

## Explicit acceptance API

`accept_suggestion(suggestion, cset)` is the only bridge to UCM. It requires a
concrete template, `APPLICABLE` status, supported normalized semantics, and all
referenced source dependencies/assumptions to already exist in the target UCM.
It then:

1. rejects semantic duplicates without adding anything;
2. uses `ConstraintSet.add()` to allocate a normal deterministic UCM ID;
3. preserves source provenance/evidence and appends a deterministic
   `KNOWLEDGE-REUSE` `Evidence` record with item/suggestion identity, source
   status, trust, matching rationale, assumptions, warnings, and explicit
   acceptance state;
4. changes the copied item's lifecycle to `REQUIRES_CONFIRMATION` rather than
   silently promoting prior fixed/confirmed intent; and
5. retains same-scope disagreements as separate constraints, reports their
   IDs, never overwrites a fixed item, and asks the existing validation/conflict
   pipeline to adjudicate them before emission/EDA.

Search and CLI suggestion commands never call this API. There is intentionally
no CLI auto-accept command in Step 26; a project is never silently rewritten.

## Knowledge-file format and security

Only a regular, non-symlink UTF-8 `.json` file at most 1 MiB is accepted.
Duplicate JSON keys, non-finite constants, unsupported schemas, unknown fields,
malformed UCM templates, unresolved/unsupported semantic template options, and
more than 1,000 patterns are rejected. YAML, Tcl, Python, and shell files are
not accepted.

The outer document is exact-schema JSON:

```json
{
  "schema_version": 1,
  "patterns": [
    {
      "id": "board-input-delay-v1",
      "title": "Known board input delay",
      "description": "Approved interface budget for the named port and clock.",
      "constraint_template": {
        "id": "source-input-delay",
        "type": "set_input_delay",
        "target_objects": ["din"],
        "source_objects": [],
        "through_objects": [],
        "clock_refs": ["clk"],
        "values": {"clock": "clk", "delay": 0.000000002, "min_max": "max"},
        "source_kind": "USER"
      },
      "applicability": {
        "required_objects": ["din"],
        "required_clocks": ["clk"],
        "required_constraint_types": ["set_input_delay"],
        "rationale": "The documented board interface and clock must match."
      },
      "assumptions": ["board_budget_approved"],
      "warnings": ["Confirm package/board revision before use."]
    }
  ]
}
```

`constraint_template` uses the existing canonical UCM constraint shape. Its
optional provenance is retained, then a `USER_FILE` evidence record with the
file digest is appended. A data file is always classified `USER_PROVIDED` even
if its contents claim a more authoritative source.

## CLI

All commands are offline and advisory:

```bash
# Built-ins plus optional strict data files/history projections
rca knowledge list --json
rca knowledge search "false path" --json
rca knowledge show K-BUILTIN-FALSE-PATH --json

# In-memory project-UCM view; writes only output/knowledge_suggestions.json
rca knowledge suggest project.yaml --knowledge approved_patterns.json --json

# Explicitly read an existing local SQLite sidecar; it is never created or changed
rca knowledge list --history-output-dir output --json
```

`list`, `search`, and `show` do not write artifacts. `suggest [CONFIG]` writes
the separate advisory `knowledge_suggestions.json` through the existing
`ArtifactManager` when a config is supplied. This is not a `RunManifest` and
makes no EDA execution claim; creating a fake run manifest for lookup would
weaken existing manifest authority. No command changes project YAML, UCM input,
SDC, SQLite history, cache, execution ledger, or fixed intent.

## Verification coverage

`tests/unit/test_knowledge.py` covers model/trust data, unit semantic
normalization, deterministic ranking, explicit acceptance/duplicates/conflicts,
provenance preservation, strict file security, and read-only history
projections. `tests/golden/knowledge/builtin_patterns.json` fixes canonical
built-in data, and `tests/integration/test_knowledge_cli.py` covers the CLI and
its separate advisory artifact. Existing UCM, semantic equivalence, validation,
history, execution-ledger, MCMM, optimizer, and real-EDA suites remain
regression gates.
