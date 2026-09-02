# Step 5 — SDC/Tcl Importer

Three-stage pipeline:

```
SDC text / file
   │
   ▼
[A] TclLexer      →  token stream per command (WORD, QWORD, BWORD, CMD_SUBST, COMMENT, SEMI, NEWLINE)
   │
   ▼
[B] SdcParser     →  SdcCommand list (name + options + positional + source span + original text)
   │
   ▼
[C] SdcImporter   →  UCM ConstraintSet (Constraint objects, EXISTING_SDC provenance, ImportMetadata)
```

## Supported SDC commands

Semantically normalized into UCM:

| Command | Notes |
|---|---|
| `create_clock` | `-name`, `-period`, `-waveform`, `-add`, `-comment` |
| `create_generated_clock` | `-name`, `-source`, `-master_clock`, `-divide_by`, `-multiply_by`, `-duty_cycle`, `-invert`, `-edges`, `-edge_shift`, `-combinational`, `-add` |
| `set_input_delay` / `set_output_delay` | value, `-clock`, `-clock_fall`, `-min`, `-max`, `-rise`, `-fall`, `-add_delay` |
| `set_clock_uncertainty` | value, `-from`/`-to`, `-setup`/`-hold`, `-min`/`-max`, `-rise`/`-fall` |
| `set_clock_latency` | value, `-source` (early/late), `-min`/`-max`, `-rise`/`-fall` |
| `set_clock_transition` | value, `-min`/`-max`, `-rise`/`-fall`, `-setup`/`-hold` |
| `set_clock_groups` | `-asynchronous`, `-logically_exclusive`, `-physically_exclusive`, multiple `-group {…}` |
| `set_false_path` | `-from`, `-to`, multiple `-through`, `-setup`/`-hold`, `-rise`/`-fall`, `-reset_path` |
| `set_multicycle_path` | N, `-from`, `-to`, `-through`, `-setup`/`-hold`, `-start`/`-end`, `-rise`/`-fall` |
| `set_min_delay` / `set_max_delay` | value, `-from`, `-to`, `-through`, qualifiers |
| `set_propagated_clock` | targets (or all clocks) |

Recognized (preserved as opaque passthrough constraints with diagnostics):

`set_load`, `set_driving_cell`, `set_input_transition`, `set_max_transition`,
`set_max_capacitance`, `set_max_fanout`, `set_case_analysis`, `set_disable_timing`,
`set_operating_conditions`, `set_wire_load_model`, `set_wire_load_mode`,
`set_clock_gating_check`, `set_data_check`, `set_ideal_network`,
`set_ideal_latency`, `set_ideal_transition`, `set_resistance`,
`set_timing_derate`, `group_path`, `set_sense`.

Unknown commands are recorded with a WARNING diagnostic and stored as
opaque passthrough constraints so no source text is silently lost.

## Supported Tcl subset

The importer is **NOT** a Tcl interpreter. It handles the lexical
constructs that appear in real SDC:

- quoted strings `"..."` with standard backslash escapes
- brace groups `{...}` with balanced nesting (verbatim)
- command substitution `[...]` (inner text preserved; only the safe
  collection subset is interpreted)
- line continuation `\` followed by newline (folded to a space)
- comments `# ...` (only at command-start)
- semicolon and newline as command separators
- arbitrary whitespace

Command substitution (`[...]`) is interpreted ONLY for these
safe collection helpers:

- `get_ports`, `get_pins`, `get_cells`, `get_nets`, `get_clocks`
- `all_inputs`, `all_outputs`, `all_clocks`, `all_registers`
- `list` (treated as literal brace-equivalent)

Any other command inside `[...]` — including nested substitutions —
becomes an UNRESOLVED TargetCollection with the original expression
preserved. The original `[expr]` text is retained in `expression`/`raw`.

## Target collections

Every target is represented by a `TargetCollection` with:

- `collection_kind` — PORT/PIN/CELL/NET/CLOCK/REGISTER/ALL_*/LITERAL/EXPR
- `expression`     — original selector text
- `pattern`        — primary name/pattern argument (if simple)
- `arguments`      — additional positional args (e.g. brace-list members)
- `filters`        — recognized option filters (e.g. `-hierarchical`, `-include_generated_clocks`)
- `resolved_objects` — names resolved against a Design/TimingGraph when available
- `resolution_status` — RESOLVED/PATTERN/UNRESOLVED

Wildcards (`*`, `?`, `[...]`) are resolved against the design's port/net/cell/register/clock
indexes via `fnmatch` when a `Design` is supplied; unresolved patterns are preserved.

## Import status model

Each imported command records an `ImportStatus`:

- **COMPLETE** — all recognized options parsed; targets resolved.
- **PARTIAL** — some options were unrecognized or semantic fidelity is
  limited (recorded in `unsupported_options`); constraint is still emitted.
- **UNRESOLVED** — target could not be resolved but original text is preserved.
- **ERROR** — could not parse or normalize; constraint may not have been created.

Diagnostics have severities INFO/WARNING/ERROR/SECURITY.

## Security model

The importer is a parser only; it never executes Tcl code:

- No `eval`, `exec`, `source`, `open`, `puts`, `file`, `glob`, `socket`,
  `load`, `package`, `after`, `catch`, `proc`, `rename`, `uplevel`,
  `interp`, or other side-effecting Tcl commands are ever interpreted.
- Top-level forbidden commands become SECURITY diagnostics with no
  constraint emitted.
- Forbidden commands inside `[...]` are recognized as EXPR/UNRESOLVED
  TargetCollections with `unresolved_reason` stating they are never executed.
- External files (`source other.sdc`) are not followed.
- No shell/system access is reachable from the parser.
- `-filter { timing_clock == "clk" }` Tcl expressions are preserved as
  opaque strings but not evaluated.

## Provenance policy

Every imported `Constraint` carries:

- `source_kind = EXISTING_SDC`
- `provenance.import_meta` with source file, source line, original
  command text, source format (`sdc`), and import timestamp.
- `status = FIXED`, `opt_status = FIXED` for imported constraints
  (imported SDC is authoritative).
- `confidence = HIGH` for well-formed commands; lower for partial.

## Information-loss policy

When an option is syntactically recognized but not semantically modeled,
the importer:

1. Records the option name in `unsupported_options` for that import.
2. Emits a WARNING diagnostic.
3. Marks the import status PARTIAL (not ERROR).
4. Still emits a constraint with the semantically understood fields.

When a target cannot be resolved (e.g. `[get_pins U1/A]` with no design
loaded), the pattern is preserved as a literal target name and the
import is marked PARTIAL/UNRESOLVED rather than dropping the constraint.

## Determinism

- Lexing and parsing produce identical token/command sequences for
  identical input (no timestamps, PIDs, or object addresses used).
- Imported constraints use stable ordering (sorted target names,
  sorted option lists).
- `SdcImporter` accepts `run_ts` and `run_id` so timestamps/run IDs
  can be pinned for canonical-snapshot reproducibility.
- Importing the same SDC twice produces byte-identical canonical JSON
  (with fixed run_ts/run_id), verified in `tests/unit/test_sdc_import.py`.

## CLI integration

`rca import <file.sdc>` prints a summary table:

```
SDC IMPORT
----------
Commands: 37
Fully resolved:       31
Partially resolved:    4
Unresolved:            2
Errors:                0
```

Use `--verbose` to list per-command diagnostics. Use `--design <top.sv>`
to enable design-aware target resolution.

## Known limitations

- Pin (`get_pins`) resolution requires a hierarchical pin index that
  the current structural model does not build for top modules; pin
  wildcards will resolve as patterns without design binding.
- Hierarchical (`-hierarchical`) and `-of_objects` collection filters are
  parsed but not yet resolved; collection is marked PARTIAL.
- `-filter {Tcl expr}` strings are preserved but not interpreted.
- Multi-scenario SDC (operating-condition-specific sets with
  `set_operating_conditions`) is not decomposed into scenario IDs yet.
- Tcl variables (`$foo`) are kept as literal text and not substituted.
- SDC generation (textual round-trip) is the next step and not part of
  this importer.
