# Step 5 — Hardened SDC/TCL Importer and Semantic Normalization

This step implements Work Package H: `existing SDC → SDC/Tcl parsing →
semantic normalization → UCM`.  The importer is a three-stage pipeline
that separates lexical analysis, command/option parsing, and UCM
normalization, preserving source provenance at every step.  It is a
parser only — never a Tcl interpreter — and enforces a strict security
boundary around command substitution.

## Files changed

New package `src/rca/sdc_importer/`:

| File | Purpose |
|---|---|
| `__init__.py` | Public API re-exports. |
| `lexer.py` | Stage A: Tcl lexer with proper handling of quotes, braces, command substitutions, line continuations, comments, and unmatched-group error recovery. |
| `parser.py` | Stage B: option/positional parser producing structured `SdcCommand` records with source span and original text. |
| `collections.py` | `TargetCollection` model, safe nested-substitution parsing, and `DesignResolver` for design-aware wildcard/object resolution (ports, nets, cells, registers, clocks, all_inputs/outputs/clocks/registers). |
| `normalizer.py` | Stage C: `SdcImporter` that emits UCM `Constraint`s with EXISTING_SDC provenance, per-command import status (COMPLETE/PARTIAL/UNRESOLVED/ERROR), unsupported-option tracking, and security diagnostics. |
| `README_SDC_IMPORTER.md` | Full documentation (supported commands, supported Tcl subset, security model, determinism guarantees, known limitations). |

Updated existing files:

- `src/rca/utils/enums.py` — added `ConstraintType.SET_CLOCK_TRANSITION`,
  new enums: `CollectionKind`, `ResolutionStatus`, `ImportStatus`,
  `DiagnosticSeverity`, `ClockGroupsRelationship`.
- `src/rca/cli/main.py` — new `rca import <file.sdc>` command
  that prints a summary (complete / partial / unresolved / error) and
  optional verbose per-command detail.
- `src/rca/sdc/parser.py` — **left untouched**; the legacy parser is
  still used by some internal tests so it keeps working. The new
  importer is in `sdc_importer/` so it does not disturb existing
  behavior.
- `tests/unit/test_sdc_import.py` — **55 new tests** covering all 36
  acceptance cases plus additional adversarial/security/determinism
  tests.

## Supported SDC commands

Semantically normalized into UCM constraints:

| Command | Options preserved |
|---|---|
| `create_clock` | `-name`, `-period`, `-waveform`, `-add`, `-comment`, target collection |
| `create_generated_clock` | `-name`, `-source`, `-master_clock`, `-divide_by`, `-multiply_by`, `-duty_cycle`, `-invert`, `-edges`, `-edge_shift`, `-combinational`, `-add`, target |
| `set_input_delay` / `set_output_delay` | value, `-clock`, `-clock_fall`, `-min`/`-max`, `-rise`/`-fall`, `-add_delay`, targets (separate constraints per (min/max, rise/fall) combination) |
| `set_clock_uncertainty` | value, `-from`/`-to`, `-setup`/`-hold`, `-min`/`-max`, `-rise`/`-fall`, targets |
| `set_clock_latency` | value, `-source`/`-early`/`-late`, `-min`/`-max`, `-rise`/`-fall`, `-clock`, targets |
| `set_clock_transition` | value, `-min`/`-max`, `-rise`/`-fall`, `-setup`/`-hold`, targets |
| `set_clock_groups` | `-asynchronous`/`-logically_exclusive`/`-physically_exclusive`, multiple `-group {…}` (preserves group structure as a single constraint) |
| `set_false_path` | `-from`, `-to`, multiple `-through` groups (ordered stages), `-setup`/`-hold`, `-rise`/`-fall`, `-reset_path` |
| `set_multicycle_path` | N, `-from`, `-to`, `-through`, `-setup`/`-hold`, `-start`/`-end`, `-rise`/`-fall` |
| `set_min_delay` / `set_max_delay` | value, `-from`, `-to`, `-through`, qualifiers |
| `set_propagated_clock` | targets (or all clocks) |

Recognized but stored as opaque passthrough constraints with a WARNING:
`set_load`, `set_driving_cell`, `set_input_transition`, `set_max_transition`,
`set_max_capacitance`, `set_max_fanout`, `set_case_analysis`, `set_disable_timing`,
`set_operating_conditions`, `set_wire_load_model`, `set_wire_load_mode`,
`set_clock_gating_check`, `set_data_check`, `set_ideal_network`,
`set_ideal_latency`, `set_ideal_transition`, `set_resistance`,
`set_timing_derate`, `group_path`, `set_sense`.

Unknown commands are recorded with an `UNKNOWN_COMMAND` warning and
stored as opaque passthroughs so no source text is silently lost.

## Supported Tcl subset

The lexer handles every common SDC lexical construct:

- Quoted strings `"..."` with standard backslash escapes (`\n`, `\t`, hex/octal/unicode, `\"`, `\[`, etc.).
- Brace groups `{...}` with balanced nesting (verbatim content).
- Command substitutions `[...]` with balanced nesting; inner text is
  preserved verbatim and interpreted only for the safe collection subset.
- Line continuations `\`-newline (with surrounding horizontal whitespace) folded into a single space.
- Comments `# ...` at command start.
- Semicolons and newlines as command separators.
- Arbitrary whitespace.
- Variable references `$foo` / `${foo}` kept as literal text (not substituted).

Command substitution is interpreted ONLY for these safe collection helpers:

- `get_ports`, `get_pins`, `get_cells`, `get_nets`, `get_clocks`
- `all_inputs`, `all_outputs`, `all_clocks`, `all_registers`
- `list` (equivalent to brace-literal)

Any other command inside `[...]` (including nested substitutions, `-filter`
Tcl expressions, and all disallowed commands) becomes an UNRESOLVED
`TargetCollection` with the original raw expression preserved.

## Target collection model

Every target is represented as a `TargetCollection` with:

- `collection_kind` — PORT/PIN/CELL/NET/CLOCK/REGISTER/ALL_*/LITERAL/EXPR
- `expression` — original selector text
- `pattern` — primary name/pattern argument
- `arguments` — additional positional args (e.g. brace-list members)
- `filters` — recognized option filters
- `resolved_objects` — names resolved against a Design/TimingGraph when available
- `resolution_status` — RESOLVED/PATTERN/UNRESOLVED
- `raw` — original token text.

Wildcards (`*`, `?`, `[...]`) are resolved against design indexes via
`fnmatch`.  Unresolved patterns are preserved as literal names rather
than dropping the constraint.

## Import-status model

Each imported command records an `ImportStatus`:

- **COMPLETE** — all recognized options parsed; targets resolved.
- **PARTIAL** — some options unrecognized (listed in `unsupported_options`) or target pattern preserved literally.
- **UNRESOLVED** — target could not be resolved; original expression retained.
- **ERROR** — failed to parse or missing required field; constraint may not have been created.

Diagnostics have severities INFO/WARNING/ERROR/SECURITY.

## UCM fields/values added/changed

- `ConstraintType.SET_CLOCK_TRANSITION` added (previously missing).
- I/O delay, clock uncertainty, latency, and transition constraints now
  carry structured `values`: `min_max`, `edge`, `setup_hold`,
  `add_delay`, `clock_fall`, `source`, `early`, `late`, etc. — one
  constraint per (min/max, rise/fall) combination so separate
  min/max/rise/fall values are never collapsed.
- Clock groups preserve the full `groups` list-of-lists in
  `values["groups"]` (a single constraint per `set_clock_groups`).
- False-path / multicycle / min_delay / max_delay use a structured
  `PathSelector` with ordered `through_set` stages so multiple
  `-through` groups are preserved (not flattened).
- Every imported constraint carries `provenance.import_meta` with
  `source_file`, `source_line`, `original_command`, `source_format`,
  `normalized_command_name`, and `import_timestamp`.

## Tests added

`tests/unit/test_sdc_import.py` — 55 tests covering:

1. Lexer (simple words, quotes, braces, command substitution,
   nested substitutions, line continuation, comments, semicolons,
   blank lines).
2. Parser/semantic: create_clock with waveform and `-add`;
   create_generated_clock (basic, divide/source); input/output delay
   (basic, min/max separation, rise/fall, add_delay); clock
   uncertainty/latency/transition; all three clock-group
   relationships; false path / multicycle / min_delay / max_delay;
   multi-through stages preserving grouping.
3. Wildcards, `get_ports`/`get_pins`/`get_clocks`, `all_inputs`/`all_outputs`.
4. Multiline continuation, brace grouping, quoted names, comments,
   unsupported commands, malformed commands, unresolved targets.
5. Nested/unsupported Tcl substitutions become UNRESOLVED; no execution.
6. Security tests proving `exec` / `source` / other forbidden commands
   are never executed (sentinel file not created; SECURITY diagnostics emitted).
7. Adversarial inputs (unmatched braces/brackets recover and continue parsing;
   unknown switches preserved as PARTIAL; top-level forbidden commands blocked).
8. Source metadata preserved (`source_file`, `source_line`, original text).
9. Multi-constraint semantics (min+max / rise+fall on the same port stay distinct).
10. Round-trip through canonical snapshot preserves provenance.
11. Cross-process determinism (fixed `run_ts`/`run_id` produces byte-identical canonical JSON).
12. Design-aware resolution against a real parsed Verilog design.

## Test command and results

```
$ python -m pytest tests/ -q
============================= 286 passed in 3.0s ==============================
```

- 286 passed (231 prior to Step 5 + 55 new importer tests)
- 0 failed
- 0 errors
- 0 skipped

Breakdown of new tests: 55 in `tests/unit/test_sdc_import.py`.

## Examples of unsupported constructs

- `-filter { timing_clock == "clk" }` Tcl expressions: preserved as
  filter option text, not interpreted; import marked PARTIAL.
- Hierarchical `-hierarchical` / `-of_objects` collection filters:
  parsed but not yet resolved; PARTIAL.
- Pin (`get_pins`) resolution against a pin hierarchy requires a
  hierarchical pin index that the current structural model doesn't
  build; pin patterns are preserved as literal names until then.
- Tcl variables (`$foo`) are kept as literal text.
- Tcl control flow (`if`, `foreach`, `proc`, `expr`) is never executed.
- Multi-scenario SDC (operating-condition-specific sets) is not yet
  decomposed into scenario IDs.
- Commands like `set_load`, `set_driving_cell`, `set_operating_conditions`
  are recognized but only passthrough-preserved (no UCM modeling yet).

## Security guarantees

The importer is a parser, not a Tcl interpreter:

1. **No Tcl execution.** The only commands interpreted inside `[...]`
   are the safe collection helpers (`get_ports`, `get_pins`,
   `get_cells`, `get_nets`, `get_clocks`, `all_inputs`, `all_outputs`,
   `all_clocks`, `all_registers`, `list`). Everything else becomes an
   UNRESOLVED `TargetCollection` with `unresolved_reason` explaining why.
2. **Disallowed commands blocked.** `exec`, `source`, `eval`, `open`,
   `close`, `read`, `write`, `puts`, `file`, `glob`, `socket`, `pid`,
   `load`, `package`, `unknown`, `after`, `catch`, `proc`, `rename`,
   `uplevel`, `upvar`, `namespace`, `interp`, `apply`, `coroutine`,
   `yield` produce `SECURITY` diagnostics at the top level and are
   rejected inside `[...]` (UNRESOLVED with reason `disallowed Tcl
   command '<name>' (never executed)`).
3. **No external file sourcing.** `source` is in the forbidden set; no
   follow-file I/O is performed.
4. **No shell/system calls.** No `os.system`, `subprocess`, or similar
   is reachable from the parser.
5. **No code evaluation.** There is no `eval()`/`exec()` of input text
   anywhere in the pipeline; collection resolution is pure pattern
   matching over pre-computed object indexes.
6. **Adversarial-input recovery.** Unmatched braces/brackets/quotes
   record a LEX_ERROR and stop at the next newline so subsequent
   commands continue to parse; a malformed command cannot crash the
   importer.

## Determinism

- The lexer and parser produce identical results for identical input
  (no hashing of object addresses, PIDs, timestamps, or dict iteration
  order dependencies).
- Imported constraints are emitted in deterministic order (sorted
  target/clock names, sorted option lists).
- `SdcImporter` accepts `run_ts` and `run_id` so canonical snapshots
  can be pinned; `cross_process_determinism` test runs the importer in
  two independent Python subprocesses and asserts byte-identical
  canonical JSON.
- Evidence IDs / provenance continue to use the stable SHA-256 scheme
  from Step 4; the importer adds no non-deterministic state.

## Known limitations

- Pin (`get_pins`) resolution is currently pattern-only because the
  structural graph does not build a flat hierarchical pin index for
  top modules.
- Hierarchical `-hierarchical` and `-of_objects` collection filters are
  parsed but not resolved; collections are marked PARTIAL.
- `-filter` Tcl expressions are preserved as opaque strings.
- Tcl variable substitution (`$var`, `${var}`) is kept as literal
  text.
- Multi-scenario / operating-condition SDC is not decomposed into UCM
  scenario IDs yet.
- SDC generation (textual round-trip from UCM back to SDC) is a
  separate upcoming step and not part of this importer.

Step 5 is complete. I have NOT proceeded to SDC generation redesign,
advanced validation, optimizer changes, OpenSTA/MCMM/dashboard changes,
or commercial backend updates.
