# Step 12 — MCMM (Multi-Mode / Multi-Corner) Support

**Status:** Implemented end-to-end on the canonical Step-11 baseline.
**Scope:** Introduce scenario-aware MCMM evaluation, feasibility, objective
aggregation, caching, Pareto and reporting while preserving the exact
single-scenario behaviour of Step 11.

> Honest limitation: MCMM evaluation is validated against a deterministic
> **mock** backend and (where the environment allows) the real per-scenario
> `run_flow` orchestration. No claim of real multi-corner signoff EDA validation
> is made — the SDC is emitted per scenario, but the mock is clearly labelled
> `is_mock=True`. Real tools (Yosys/OpenSTA) can be wired via the
> `MCMMEvaluator` with a vendor-specific `evaluate_scenario` callable; none of
> that vendor logic lives in the core.

---

## 1. Architecture

A new `src/rca/mcmm/` package was added. It is *not* a second scenario model or a
parallel system — it composes the existing canonical `Scenario` (from
`rca.constraint_model`) and the existing QoR / Pareto machinery.

```
src/rca/mcmm/
    model.py     ScenarioQoR, ObjectiveAggregate, MCMMResult (+ status vocab)
    matrix.py    ScenarioMatrix, build_scenario_matrix
    aggregate.py global_feasibility, aggregate_objectives, global_margin,
                 finalize_limiting, mcmm_is_dominating, mcmm_pareto_front,
                 mcmm_scalar_score, mcmm_select_final, mcmm_explanation_for
    cache.py     scenario_cache_key, mcmm_run_cache_key, scenario_semantic_key
    evaluate.py  MCMMEvaluator (+ mock_mcmm_evaluator factory)
    __init__.py  public API
```

Integration points (no parallel systems):

- `rca.constraint_model.scenarios` — canonical `Scenario` pydantic model.
- `rca.constraint_model.constraint` — `Constraint.scenario_ids` added; included
  in `semantic_key()` and `stable_hash_cset()`.
- `rca.constraint_model.constraint_set` — `ConstraintSet.scenarios`, `add_scenario`,
  `get_scenario`, clone preserves scenarios.
- `rca.optimizer.*` — `Candidate` carries a `mcmm` result; `Optimizer._evaluate`
  branches to the MCMM evaluator; `_classify` skips MCMM candidates; selection uses
  MCMM Pareto.
- `rca.sdc.generation.renderer` — scenario-aware constraint filtering via
  `_scenario_applies()`; generic + opensta backends propagate `scenario`.
- `rca.cli.main` — MCMM-aware `analyze`/`infer`/`generate`/`validate`/`coverage`/
  `run-sta`/`optimize`/`report`.

## 2. Scenario model

A scenario is `(id, mode, corner, libraries, parasitics, sdc_set_id,
environment, active, analysis_count, parent_scenario_id, name, semantic_key,
summary)`. No mode/corner names are hard-coded: the matrix is driven by config
`scenarios` and the UCM's `Scenario` definitions. `build_scenario_matrix(cfg, cset)`
merges the two (UCM definitions win for identity; config supplies active-set).

- Empty `scenario_ids` on a constraint ⇒ applies to **all** active scenarios.
- Non-empty `scenario_ids` ⇒ applies **only** to the listed scenarios.
- Single-scenario or `mcmm.enabled=false` ⇒ legacy behaviour preserved exactly.

## 3. Configuration

`ProjectConfig` gains:

```yaml
scenarios:
  - id: FUNC_SLOW
    mode: functional
    corner: slow
    libraries: []
    parasitics: rc_slow
    environment: {temp: 125, voltage: 0.90}
    active: true
mcmm:
  enabled: true
  active_scenario_ids: [FUNC_SLOW, FUNC_FAST]
```

`MCMMConfig` (`enabled`, `active_scenario_ids`) defaults to disabled. When
disabled or only one scenario is active, the optimizer uses the legacy path.

## 4. Per-candidate evaluation

`MCMMEvaluator(cand, work_dir) -> MCMMResult` evaluates the candidate across
**every** active scenario (one `ScenarioQoR` each). Each record retains candidate
id, scenario id, mode, corner, per-scenario QoR, per-scenario feasibility, cache
key, cache status, run id, backend/tool, diagnostics and provenance. The result is
**never collapsed** into a single number.

## 5. Global feasibility

`global_feasibility()` requires **every** active scenario to be feasible.
Precedence: invalid ⇒ blocked ⇒ infeasible ⇒ feasible. The status vocabulary is
`feasible | infeasible | blocked | invalid`, with the reason for each failing
scenario retained on the per-scenario record. `limiting_scenarios` records the
failing / binding scenario ids.

**Missing results are never silently skipped.** A required/active scenario with
no recorded `ScenarioQoR` (or which is referenced but has no scenario definition)
is synthesised as `blocked` with the deterministic reason `missing_scenario_result`
and retained in `limiting_scenarios`; a candidate is therefore globally infeasible
whenever any required scenario is missing (or failed/blocked). A healthy scenario
can never rescue a missing/failing one.

## 6. Objective aggregation

`aggregate_objectives()` is conservative/binding:

- MAXIMIZE objectives (`setup_wns`, `hold_wns`, `setup_tns`, `hold_tns`) bind on
  the **minimum** across known scenarios.
- MINIMIZE objectives (`area`, `power`) bind on the **maximum**.
- `constraint_quality` (maximize) binds on the minimum.
- **UNKNOWN stays UNKNOWN**: if any required scenario lacks the metric, the
  aggregate value is `None`/unknown — never averaged, never fabricated.
- The **limiting scenario(s)** for each objective are retained.
- **Area:** `real + real` and `proxy + proxy` are comparable (binding per
  direction); `real + proxy` is INCOMPARABLE; if **any** required scenario has
  UNKNOWN area the global area is **UNKNOWN** (never aggregated over only the
  known scenarios, never fabricated to zero/placeholder). The scenario IDs
  responsible for the unknown are retained.

## 7. Area semantics

Area keeps its real/proxy identity:

- real vs real ⇒ comparable (lower better).
- proxy vs proxy ⇒ comparable.
- real vs proxy ⇒ **INCOMPARABLE** — no numeric conversion is ever invented;
  the aggregate is marked `incomparable`.

## 8. Power semantics

- UNKNOWN power stays UNKNOWN (never converted to 0).
- The scenario(s) responsible for missing power are retained on the aggregate
  (`limiting`).

## 9. Margin policy

Per scenario, the exact Step-11 math is preserved:

```
setup_headroom = max(0, baseline_setup_wns - required_setup)
hold_headroom  = max(0, baseline_hold_wns  - required_hold)
setup_consumed = max(0, baseline_setup_wns - candidate_setup_wns)
hold_consumed  = max(0, baseline_hold_wns  - candidate_hold_wns)
setup_util     = consumed / setup_headroom
hold_util      = consumed / hold_headroom
margin_utilization = max(setup_util, hold_util)
```

Both baseline headrooms must be known and positive; there is **no**
one-dimensional fallback. Margin is computed **per scenario**, then aggregated to
a binding global signal (worst utilisation, worst headroom) with its limiting
scenario. `margin_utilization` is **diagnostic only**: it is excluded from
`OBJECTIVE_SPECS` / the Pareto scalar and is a secondary final-selection signal.

**The MCMM baseline is evaluated per scenario.** `MCMMEvaluator` derives a
per-scenario baseline `(setup_wns, hold_wns)` map (from the baseline candidate's
own per-scenario QoR when the candidate IS the baseline, otherwise a single
baseline evaluation is cached) and feeds it to `global_margin()` so the exact
Step-11 math runs for every candidate against a real per-scenario baseline. The
baseline map is retained in MCMM provenance. This is wired through the
**production** `Optimizer` path (not just the helper): the baseline candidate is
evaluated per scenario first, and every candidate's per-scenario margin is
populated against that baseline, with a deterministic global limiting scenario.

## 10. Pareto policy

`mcmm_is_dominating()` / `mcmm_pareto_front()` represent the **complete scenario
set**. A candidate cannot appear superior merely by improving one scenario while
degrading another. Compatibility is maintained with:

- UNKNOWN metrics (block dominance conservatively),
- real/proxy area INCOMPARABILITY (block dominance),
- hard / per-scenario feasibility (only globally-feasible candidates enter).
Deterministic ordering by candidate id keeps the front reproducible.

## 11. Cache policy

`scenario_cache_key()` / `mcmm_run_cache_key()` compose `stable_hash_cset()` with
scenario identity (id, mode, corner, libraries, parasitics, environment) and the
backend / tool identity + version. A single canonical `CACHE_VERSION = 3`
constant is used in both functions and in the docs/tests so the versioning policy
is consistent. `scenario_semantic_key()` distinguishes mode/corner so
`functional/slow` can never reuse a `functional/fast` entry unless the scenario
semantics are identical. `stable_hash_cset()` is preserved and includes sorted
`scenario_ids`.

## 12. Candidate generation

One UCM-level mutation = one candidate; that candidate is then evaluated across
**all** active scenarios. Mutation count is **not** multiplied by the scenario
count, and candidate ids remain deterministic. Scenario definitions survive
candidate cloning (see `rca.optimizer.search._clone`).

## 13. CLI changes

- `analyze`/`infer`/`report` print the active scenario matrix when MCMM is enabled
  and `scenario_count > 1`.
- `generate` accepts `--scenario <id>` to emit scenario-specific SDC.
- `validate`/`coverage` print the active matrix.
- `run-sta` branches on MCMM: with `mock` it runs the mock MCMM evaluator on a
  baseline candidate and writes `mcmm_report.json`; with a real backend it runs
  `run_flow` once per active scenario (passing `scenario`/`corner`).
- `optimize` uses the `MCMMEvaluator` for `scenario_count > 1`, then prints the
  matrix + per-scenario QoR + global limiting scenarios.

## 14. Reporting

`mcmm_explanation_for()` and the CLI `_print_mcmm_result()` expose: active scenario
matrix, per-scenario QoR, per-scenario feasibility, global feasibility, limiting
scenarios, cache hits/misses, EDA runs, Pareto membership, final candidate,
provenance and diagnostics. This answers the question *"which mode/corner is
limiting this candidate?"*.

## 15. Provenance

Scenario identity (id, mode, corner, libraries, parasitics, environment) is
attached to every per-scenario value / decision (ScenarioQoR.provenance) and to
the aggregate (MCMMResult.provenance), reusing the existing AssumptionLedger /
provenance architecture.

## 16. Backend

Vendor logic is injected via the `evaluate_scenario` callable of `MCMMEvaluator`;
no vendor-specific MCMM behaviour lives in the core. `mock_mcmm_evaluator()`
provides a deterministic, clearly-labelled mock for tests/offline dev.

## 17. Tests

`tests/unit/test_mcmm.py` — 45 parameterised assertions covering (Step 12 §17):

1. scenario matrix creation
2. single-scenario backward compatibility
3. multiple modes
4. multiple corners
5. scenario-specific constraints
6. all-scenario constraints
7. per-candidate evaluation of every scenario
8. one-scenario-infeasible ⇒ global infeasible
9. blocked / invalid ⇒ global blocked / invalid
10. per-scenario diagnostics
11. limiting-scenario selection
12. UNKNOWN metrics
13. real/proxy area incomparability
14. UNKNOWN power
15. scenario-aware cache identity
16. cache isolation (same cset different scenario; identical scenario same key)
17. deterministic evaluation
18. fixed-constraint immutability
19. one-mutation-per-candidate
20. Pareto (complete scenario set)
21. final selection (global timing; margin tie-break)
22. reporting completeness
23. provenance
24. enable/disable MCMM
25. invalid/empty scenario configuration
26. optimizer backward compatibility (MCMM disabled)
27. scenario-specific SDC
28. repeated determinism
29. same-scenario cache hit
30. different-scenario cache separation (+ direction / conservative-aggregation
    and per-scenario/global margin math)

## 18. Example

`examples/mcmm_counter/` — a deterministic MCMM demonstrator (two active
scenarios, `FUNC_SLOW`/`FUNC_FAST`) using the mock backend. Run:

```bash
python -m rca.cli run-sta examples/mcmm_counter/project.yaml --backend mock
python -m rca.cli optimize examples/mcmm_counter/project.yaml --backend mock
python -m rca.cli report examples/mcmm_counter/project.yaml
```

## 19. Test results

- Step-11 regression gate: `python -m pytest tests/unit/test_pareto.py -q` ⇒
  **125 passed**.
- New MCMM tests: `python -m pytest tests/unit/test_mcmm.py -q` ⇒ **70 passed**.
- Full suite: `python -m pytest -q` ⇒ **760 passed** (0 failed), including the
  cross-process determinism tests once `pyslang` is installed. (Without
  `pyslang`, the two `SlangAdapter`-based determinism / parser tests report an
  honest environment limitation.)

## 20. Limitations

- `report` command previously crashed on a coverage key mismatch
  (`input_timing_coverage_pct` vs `input_timing_path_coverage_pct`) in
  `src/rca/explanation/generator.py` **before** this step; that pre-existing bug
  was fixed so `report` works for both legacy and MCMM configs.
- No real EDA multi-corner signoff is claimed; only mock and orchestration are
  validated in this environment (`pyslang`/Yosys/OpenSTA availability is the
  caller's responsibility).
- `ScenarioSpec.constraints` is **explicitly rejected** when non-empty
  (deterministic `ValueError` with a clear message). It is a reserved,
  currently-unsupported configuration block and is never silently ignored.
  Scenario-specific constraints must be expressed via `Constraint.scenario_ids`.
- A **pre-existing** `report` command crash (coverage key mismatch in
  `src/rca/explanation/generator.py`) was fixed so the MCMM-aware `report`
  path is reachable; this is unrelated to MCMM semantics.

## 21. Reproducibility

- All MCMM logic is deterministic (seeded mock, sorted scenario/objective order,
  candidate-id tie-break).
- Cache identity is W/M-stable and scenario-aware; `stable_hash_cset()` is intact.
- No generated artifacts (`output/`, `examples/*/output/`) are tracked.
