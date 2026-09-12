# Step 15 Report — Semantic Comparison Audit and Hardening

## 1. Scope and baseline

Step 15 is an **audit-and-harden** milestone authorized after repository
inspection found no separate tracked Step-15 work package. The canonical
semantic-comparison implementation is Step 9 / WP-L (`STEP9_REPORT.md`). This
milestone audits that accepted implementation rather than replacing or
renaming it.

Baseline audited: Step-14 commit
`8e71aaca4cab03b0d897a388c1430391b611b73e` on the existing development
branch. The comparator remains UCM-first; SDC is imported into UCM and is not
compared as raw text.

## 2. Audit result matrix

| Requirement | Existing implementation / evidence | Audit result |
|---|---|---|
| UCM rather than text semantics | `compare()` consumes `ConstraintSet`; `compare_sdc_text()` uses the existing `SdcImporter`. `test_sdc_A`–`test_sdc_H`. | **Implemented and verified.** |
| Units, defaults, formatting and unordered ordering | `normalize.py`; `test_02`–`test_07`, `test_sdc_B`. | **Implemented and verified.** |
| Meaningful ordering remains meaningful | Ordered waveforms, generated-clock edges, and `-through` stages in `normalize.py`; `test_09`, `test_15`, `test_19`, adversarial ordering tests. | **Implemented and verified.** |
| Object/reference, clock, timing, I/O, group and exception differences | Type-specific semantic fields and field differences; `test_08`–`test_18`, latency/transition/min-max tests, golden and adversarial tests. | **Implemented and verified.** |
| Complete MCMM applicability | `ScenarioMatrix` defines empty `scenario_ids` as all active scenarios. Step-9 comparison used raw IDs only. New active-scope projection in `semantic_compare.py`; `test_15_mcmm_empty_scope_equals_explicit_complete_active_scope_read_only`. | **Defect found and fixed.** |
| Scenario identity / definition differences | Active matrices are now compared using the existing `Scenario.semantic_key()`; deterministic `scenario_differences` are included in the result. `test_15_mcmm_different_active_context_is_not_falsely_equivalent`; `test_15_mcmm_same_id_different_definition_is_not_equivalent`. | **Defect found and fixed.** |
| Unknown, partial, unresolved input | `has_unsupported_options()` and importer statuses; `test_20`, `test_21`, `test_sdc_E`, `test_sdc_F`, and `test_15_mcmm_unknown_constraint_scenario_remains_unknown`. | **Implemented and hardened.** |
| Provenance retained but non-semantic | `PairResult` source/provenance fields; `test_23`, `test_sdc_G`, CLI output in `test_15_cli_unsupported_sdc_is_unknown_not_equivalent`. | **Defect found and fixed.** |
| Deterministic dict/machine output | Stable hashes and ordered output; `test_26`, `test_27_cross_process_determinism_subprocess`, `test_report_to_dict_is_jsonable`; Step-15 manual repeated scenario-context digest check. | **Implemented and verified.** |
| Human and JSON CLI output | `rca compare` now invokes `compare_sdc_text`; `test_15_cli_unsupported_sdc_is_unknown_not_equivalent` plus manual CLI evidence below. | **Defect found and fixed.** |
| Read-only / fixed user intent | Comparison-only `model_copy()` scope projection; `test_15_mcmm_empty_scope_equals_explicit_complete_active_scope_read_only`. | **Implemented and verified.** |
| Formal and backend independence | The equivalence package has no formal-backend dependency and compares UCM only; `test_sdc_roundtrip_generate_import_compare`. | **Implemented and verified.** |
| No false equivalence / arbitrary pairing | UNKNOWN policy and deterministic multiset pairing; `test_20`, `test_21`, `test_pairing_min_max_does_not_falsely_pair`, `test_pairing_ambiguous_candidates_not_force_paired`. | **Implemented and verified.** |

## 3. Actual defects found and fixes made

### 3.1 CLI dropped unresolved timing intent

**Defect.** `rca compare` used the legacy `SDCParser`, while the public
`compare_sdc_text()` API used the hardened `SdcImporter`. The legacy parser
silently omitted a recognized-but-unmodeled command such as `set_load`. Two
identical files containing only that command therefore produced
`EQUIVALENT` with zero constraints, violating the rule that unknown material
intent cannot become equivalence.

**Fix.** The CLI reads the supplied files and delegates to
`compare_sdc_text()`. This reuses the existing importer, preserves its
structured diagnostics and opaque metadata, and reports `UNKNOWN` in this
case. It does not add a parser or an SDC semantic model.

**Regression evidence.** `test_sdc_E_unsupported_causes_unknown` was
strengthened from accepting an equivalent result to requiring `UNKNOWN`.
`test_15_cli_unsupported_sdc_is_unknown_not_equivalent` exercises the actual
Typer command and both JSON and human-readable output paths.

### 3.2 MCMM scope was compared as raw syntax instead of active applicability

**Defect.** The Step-9 comparator treated `scenario_ids=[]` as a literal
empty tuple. It therefore (a) reported a false difference between an empty
scope and an explicit list of all active scenarios, and (b) reported a false
equivalence for identical global constraints when the two UCMs had different
active scenario matrices.

**Fix.** When both UCMs provide active MCMM definitions, comparison creates
read-only projection copies whose scenario IDs are their effective active
scope. It compares active scenario definitions using the established
`Scenario.semantic_key()` and emits deterministic structured
`scenario_differences`. An invalid constraint scenario ID becomes `UNKNOWN`.
A one-sided active matrix also remains `UNKNOWN`, rather than being guessed.

### 3.3 Rich markup hid semantic categories in CLI output

**Defect.** Human output formatted a category as `[create_clock]`, which Rich
interpreted as markup and displayed as blank. This prevented the command from
reliably showing the semantic category required to understand a difference.

**Fix.** The command now escapes category, identifier, value, scenario, and
note text before Rich rendering. It displays literal categories such as
`[create_clock]` and remains safe for user-controlled identifiers.

### 3.4 Comparison provenance equality used a removed field

**Defect.** `_provenance_equal()` checked a `source_file` attribute directly
on `ProvenanceRecord`, but imported source paths live in
`ProvenanceRecord.import_meta`. Two constraints imported from distinct files
could consequently be labelled `provenance-equal` merely because their source
kind matched. The machine result retained only source kinds and the human
output did not display available provenance.

**Fix.** `PairResult` now carries deterministic source provenance summaries
(source kind, creator, rule ID, and import file/line/format) and the actual
`provenance_equal` verdict. Source timestamps and mutable evidence are
intentionally excluded from this comparison report to retain deterministic
output; full provenance remains on the source UCM. Human output shows those
summaries for difference/UNKNOWN findings.

**Regression evidence.** `test_23_provenance_difference_semantic_equal` and
`test_sdc_G_provenance_does_not_affect_equality` now verify exact provenance
status and import locations. The CLI regression also verifies its provenance
section.

### 3.5 Opaque `set_load` was displayed as `set_case_analysis`

**Defect.** The opaque-import fallback constructed every unsupported command
as `ConstraintType.SET_CASE_ANALYSIS`. Consequently, identical unsupported
`set_load` commands conservatively produced `UNKNOWN`, but both CLI formats
misidentified their category as `set_case_analysis`.

**Fix.** The importer retains `ConstraintType.SET_LOAD` for the recognized
`set_load` command while preserving its opaque metadata and unsupported-import
status. It remains outside the published semantic comparison rules, so the
result remains `UNKNOWN`; no timing equivalence rule was added.

**Regression evidence.** `test_sdc_E_unsupported_causes_unknown` asserts the
API reports `set_load` and an unmodeled-category note. The CLI regression
asserts `[set_load]` in human output, `constraint_type: "set_load"` in JSON,
the `UNKNOWN` status/count, and retained `UNSUPPORTED_COMMAND` diagnostics.

## 4. API and architecture impact

No existing comparison, UCM, SDC, MCMM, validation, or formal API was removed
or replaced. `compare()`, `compare_sdc_text()`, normalization functions, and
`rca compare` retain their signatures.

`ComparisonResult.to_dict()` gains backward-compatible reporting fields:

- `scenario_differences`: deterministic structured active-MCMM context
  findings (`DIFFERENT`, `ONLY_IN_LEFT`, `ONLY_IN_RIGHT`, or `UNKNOWN`);
- `counts.scenario_differences`: the corresponding summary count;
- `a_provenance`, `b_provenance`, and the actual `provenance_equal` verdict
  on every paired constraint, retaining deterministic origin details.

The additions are reporting records, not a second scenario model. They reuse
`ConstraintSet.scenarios`, `Scenario.semantic_key()`, and the existing
Scenario/MCMM applicability policy.

Formal verification remains independent: no formal result affects comparison,
and comparison imports no formal backend or generates proof properties.

## 5. CLI evidence

The following was exercised against temporary SDC inputs using the established
interface:

```console
$ rca compare examples/simple_counter/project.yaml --a a.sdc --b b.sdc
Status: DIFFERENT  (level=SEMANTIC_DIFFERENT)
Top semantic differences:
  - [create_clock] CLK0001 → CLK0001
      period: A=1e-08  B=8e-09
        numeric timing value 'period' differs after unit normalization.
```

Both human and `--json` modes were also exercised with identical files
containing unsupported `set_load 0.5 [get_ports out]` intent. Human output
reported `Status: UNKNOWN`, `Unknown: 1`, and `[set_load]`. JSON emitted
`overall_status: "UNKNOWN"`, one unknown constraint with
`constraint_type: "set_load"`, and the importer `UNSUPPORTED_COMMAND`
diagnostics for both inputs. Neither mode claimed equivalence.

## 6. Deliberate limitations retained

These are intentional conservative outcomes, not unaddressed defects:

- Constraint types without published semantic rules (including `set_load`,
  `set_case_analysis`, `set_disable_timing`, and design-rule constraints)
  remain `UNKNOWN`.
- Unresolved/expressive target collections and partial/unresolved imports
  remain `UNKNOWN`.
- A comparison with active MCMM definitions on only one side remains
  `UNKNOWN`; no implicit scenario mapping is invented.
- Clock aliasing and vendor-specific semantic quirks are not inferred.
- Formal proof outcomes are not semantic-equivalence evidence.

## 7. Tests and checks

New focused regressions in `tests/unit/test_equivalence.py`:

1. `test_15_mcmm_empty_scope_equals_explicit_complete_active_scope_read_only`
2. `test_15_mcmm_different_active_context_is_not_falsely_equivalent`
3. `test_15_mcmm_same_id_different_definition_is_not_equivalent`
4. `test_15_mcmm_unknown_constraint_scenario_remains_unknown`
5. `test_15_cli_unsupported_sdc_is_unknown_not_equivalent`

The existing unsupported-import test was strengthened. The full
semantic-comparison suite now contains 67 tests.

Final regression results, run with the project editable environment on Python
3.11.2:

| Gate | Command | Result |
|---|---|---|
| Step 11 | `python -m pytest tests/unit/test_pareto.py -q` | **125 collected / 125 passed / 0 failed / 0 skipped / 0 errors** |
| Step 12 | `python -m pytest tests/unit/test_mcmm.py -q` | **70 collected / 70 passed / 0 failed / 0 skipped / 0 errors** |
| Step 13 | `python -m pytest tests/unit/test_validation_step13.py -q` | **40 collected / 40 passed / 0 failed / 0 skipped / 0 errors** |
| Step 14 | `python -m pytest tests/unit/test_symbiyosys.py -q` | **11 collected / 11 passed / 0 failed / 0 skipped / 0 errors** |
| Step 15 semantic comparison | `python -m pytest tests/unit/test_equivalence.py -q` | **67 collected / 67 passed / 0 failed / 0 skipped / 0 errors** |
| Full project | `python -m pytest -q` | **816 collected / 816 passed / 0 failed / 0 skipped / 0 errors** |

`compileall` and `git diff --check` also passed. The audit did not change the
formal, validation, optimizer, UCM, or SDC backend architectures.
