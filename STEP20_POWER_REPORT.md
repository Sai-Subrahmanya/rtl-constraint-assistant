# Step 20 — OpenROAD/OpenSTA Power-Report Ingestion

## Scope and honest boundary

Step 20 adds conservative ingestion of one real, explicitly configured power
report syntax to the existing report parser, QoR model, EDA flow, artifact
manifest, cache, MCMM, optimizer, and CLI paths. It does **not** implement a
power-analysis engine, generate switching activity, invoke OpenROAD/OpenSTA
power commands, or claim a physical/silicon measurement.

The supported syntax is based on the OpenROAD/OpenSTA `report_power`
group-summary documented in the OpenROAD Project tutorial:
https://github.com/The-OpenROAD-Project/micro2022tutorial

RCA calls an accepted value **tool-reported** or **report-derived** power. It
means a valid value existed in the configured tool report; its underlying
Liberty/activity assumptions remain the report producer's responsibility.

## Supported format

The only accepted format identifier is:

```yaml
format: openroad_report_power
```

RCA recognizes exactly one OpenROAD/OpenSTA group-summary table with this
column order and a final `Total` row:

```text
report_power
=========================================================================
Group                  Internal  Switching    Leakage      Total
                          Power      Power      Power      Power (Watts)
-------------------------------------------------------------------------
Sequential             4.49e-04   6.01e-05   3.13e-06   5.12e-04  38.6%
Combinational          4.08e-04   3.96e-04   9.84e-06   8.14e-04  61.4%
-------------------------------------------------------------------------
Total                  8.57e-04   4.56e-04   1.30e-05   1.33e-03 100.0%
```

The checked-in example at
`tests/golden/reports/openroad_report_power_representative.rpt` is a single,
deterministic **representative test fixture**. Its comments explicitly state
that it was not produced by a live tool execution in this repository.

## Field mapping and units

All accepted values are normalized to watts before entering the existing
`QoRResult`:

| Report Total-row column | Canonical existing QoR field |
|---|---|
| `Total` | `power` and `power_total` |
| `Internal` + `Switching` | `power_dynamic` |
| `Leakage` | `power_leakage` |

Dynamic power is computed only if **both** Internal and Switching are valid.
RCA never derives `dynamic = total - leakage`. A missing dynamic input leaves
`power_dynamic=None`; a missing Leakage cell leaves `power_leakage=None`.
Those omissions do not invalidate an otherwise valid explicit total.

Accepted explicit header units are `W`, `Watts`, `mW`, `uW`, `µW`, `nW`, and
`pW`. A bare table with no declared supported unit is not assumed to be watts.
A literal total of `0` is an available, valid numeric power value and is not a
missing-value sentinel.

## Conservative status policy

The canonical `QoRResult.power_status` remains exactly the historical,
wire-compatible `PowerStatus` vocabulary: `AVAILABLE`, `UNAVAILABLE`, and
`ESTIMATED`. The parser never assigns `ESTIMATED`.

Detailed ingestion classification is deliberately separate:
`rca.reports.power.PowerParseStatus` is retained as
`QoRResult.raw_reports["power"]["parsing_status"]`. Every non-available parser
classification maps to canonical QoR `power_status=UNAVAILABLE` and all
canonical power fields remain `None`.

| Parser classification | Meaning | Canonical QoR `power_status` / numeric `power` |
|---|---|---|
| `AVAILABLE` | Exactly one valid table, explicit supported unit, and usable total. | `AVAILABLE` / Total in W, including `0.0`. |
| `UNAVAILABLE` | No mapping was configured or the configured report file is absent/unreadable. | `UNAVAILABLE` / `None` |
| `UNKNOWN` | Recognized table but no unambiguous usable total, including missing total/table unit or multiple table/total candidates. | `UNAVAILABLE` / `None` |
| `MALFORMED` | Intended `report_power` report has malformed structure or numeric cells. | `UNAVAILABLE` / `None` |
| `INVALID` | Parsed value is negative or a complete component sum differs materially from Total. | `UNAVAILABLE` / `None` |
| `UNSUPPORTED` | File does not identify this format or declares an unsupported unit. | `UNAVAILABLE` / `None` |
| `ESTIMATED` | Canonical compatibility status only; not emitted by this parser or its parser classification. | Existing caller behavior. |

When all three components are present, Total is checked against
Internal + Switching + Leakage using a 2% relative / `1e-15 W` absolute printed
report rounding tolerance. Multiple candidate tables or total rows are always
ambiguous; RCA does not choose one by position or value coincidence.

## Configuration and scenario binding

The existing `flow:` configuration gains `power_reports`:

```yaml
flow:
  power_reports:
    - format: openroad_report_power
      path: reports/func_slow.power.rpt
      scenario_id: FUNC_SLOW
      # producer defaults to openroad_opensta
      # producer_version is optional when the report producer version is known
```

Paths are resolved by the existing `ProjectConfig.resolve_paths()` mechanism.
The generated JSON schema is updated at
`configs/schemas/project.schema.json`.

For a single scenario, `scenario_id` may be omitted. For `mcmm.enabled: true`,
every mapping must name one configured active scenario. Configuration rejects:

- an omitted/global MCMM mapping;
- an unknown or inactive scenario ID;
- duplicate scenario mappings;
- a default single-scenario mapping mixed with a labelled mapping;
- unsupported format/producer keys or malformed fields.

Missing reports are permitted as missing evidence rather than converted to a
configuration value: that scenario receives `UNAVAILABLE` and `power=None`.
There is no global MCMM report fallback.

## Provenance, manifests, and cache identity

The parser boundary is `rca.reports.power.PowerReportParseResult`; it is not a
second QoR or provenance model. The existing `QoRResult.raw_reports["power"]`
contains format and parser version, configured producer and optional producer
version, discovered flow tool version, original unit and normalized unit when
numeric power is accepted, scenario/mode/corner, source report path/SHA-256,
parser classification,
diagnostics, and raw Internal/Switching source cells.

`QoRResult.summary()`/`to_dict()` retain old fields and add:

- `power_total`
- `power_dynamic`
- `power_leakage`
- `power_provenance`

On a real flow, the configured report is copied unchanged into the run
artifact directory as `configured_power_report.rpt`. The existing
`RunManifest.artifacts` calls it `power_report`, and the existing
`artifact_hashes` records its SHA-256. The QoR provenance retains the original
configured path, while the manifest artifact remains run-relative and portable.

The existing run-flow cache key (version 3) contains the canonical selected
power-report input: path, content SHA-256, format, parser version, effective
scenario association, producer, producer version, presence, and file-existence
state. Changing report content, path, format, producer, scenario association,
or absence/presence changes the cache identity. The staged artifact hash also
prevents reuse when a previously recorded report artifact has been modified.

## Flow, MCMM, objectives, and CLI

For the existing real `yosys_opensta` flow, synthesis and STA remain unchanged.
After successful STA, the flow selects only the configured report for the
current scenario, parses it, merges its fields into the existing `QoRResult`,
and writes existing QoR/manifest artifacts. The mock backend remains explicitly
mock, ignores configured power-report inputs, and returns unavailable power.

Each MCMM scenario invokes this same flow selection with its own scenario ID,
mode, and corner. Per-scenario QoR retains its own power provenance. The
existing conservative aggregation sees any canonical unavailable/missing power
as unknown; it does not average the remaining scenarios or create a global
power number. Existing Pareto logic treats available report-derived lower power
as lower-is-better and keeps canonical unavailable (including all non-available
parser classifications) out of the power objective.

Existing commands are extended rather than replaced:

- `rca run-sta` prints tool-reported total and available dynamic/leakage
  components. For unusable evidence it prints canonical `UNAVAILABLE` together
  with the parser classification when it is more specific, plus report format,
  path, and SHA-256 provenance.
- real `rca optimize` routes evaluation through the existing flow so it can
  retain report-derived fields and cache/manifest evidence; its final output
  prints power status/value/provenance.
- `rca report` displays the most recent already-recorded QoR power/provenance;
  it does not trigger tool execution or parse a new report itself.

## Tests

`tests/unit/test_power_reports.py` uses the one tracked representative fixture
and temporary/in-memory variants to cover total/dynamic/leakage mapping,
W/mW/uW/µW/nW/pW conversion, zero, unavailable/missing/ambiguous/malformed/
invalid/unsupported cases, provenance, deterministic parsing, configuration
validation, manifest artifact hash, cache invalidation, real-flow plumbing,
MCMM scenario binding/incomplete global power, Pareto consumption, and mock
behavior. No test executes a real OpenROAD/OpenSTA power analysis.

Final regression results, run in the repository's editable Python 3.11.2
virtual environment (so commands are `python -m pytest ...` after activation):

| Gate | Result |
|---|---|
| Step 20 power ingestion: `tests/unit/test_power_reports.py` | **38 collected / 38 passed / 0 failed / 0 skipped / 0 errors** |
| Existing EDA/report regression: `tests/unit/test_eda.py` | **34 collected / 34 passed / 0 failed / 0 skipped / 0 errors** |
| Step 11 Pareto: `tests/unit/test_pareto.py` | **125 collected / 125 passed / 0 failed / 0 skipped / 0 errors** |
| Step 12 MCMM: `tests/unit/test_mcmm.py` | **70 collected / 70 passed / 0 failed / 0 skipped / 0 errors** |
| Step 13 validation: `tests/unit/test_validation_step13.py` | **40 collected / 40 passed / 0 failed / 0 skipped / 0 errors** |
| Step 14 SymbiYosys adapter: `tests/unit/test_symbiyosys.py` | **11 collected / 11 passed / 0 failed / 0 skipped / 0 errors** |
| Step 15 equivalence: `tests/unit/test_equivalence.py` | **67 collected / 67 passed / 0 failed / 0 skipped / 0 errors** |
| Full project: `python -m pytest -q` | **854 collected / 854 passed / 0 failed / 0 skipped / 0 errors** |

`python -m compileall -q src tests`, `git diff --check`, and Ruff checks on the
new parser/test files also passed. The power tests use fake orchestration
executables strictly to exercise RCA's real-flow plumbing; they are not a live
OpenROAD/OpenSTA power-tool test.

## Deliberate limitations

- Only the specified text group-summary grammar is supported.
- A report without an explicit accepted table-header unit is rejected as
  unknown, even when a tool installation convention might default to watts.
- RCA does not parse an arbitrary `report_units` context or infer a scenario
  identity from unstandardized report comments.
- No live power tool, VCD/SAIF generation, activity estimation, parasitic
  analysis, commercial parser, or physical measurement is included.
- The fixture demonstrates parsing syntax only and must never be described as
  project tool output or a measured design result.
