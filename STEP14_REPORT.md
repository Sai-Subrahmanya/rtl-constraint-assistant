# Step 14 — SymbiYosys Formal Exception Adapter

**Status:** Implemented on the merged Step-12/Step-13 main baseline.

## Objective

Implement the planned concrete **SymbiYosys** adapter for formal verification
of `set_false_path` and `set_multicycle_path` exceptions.  This work extends
the existing Step-8 `FormalBackend` / `verify_exceptions()` abstraction and
integrates its outcomes with the Step-13 validation engine.  It does not
replace the UCM, validation model, MCMM model, SDC renderer, EDA backends, or
optimizer.

## Requirements implemented

1. **Concrete backend.** `SymbiYosysFormalBackend` invokes an explicit `sby`
   job with an argument list (`shell=False`) and a configured timeout.
2. **No invented proof.** RCA does not derive a temporal assertion from an SDC
   selector.  Users map one UCM exception ID to one design-specific, authored
   `.sby` proof job and optional task.  No mapping, missing job/tool, timeout,
   `UNKNOWN`, or no status marker is `UNRESOLVED` — never `VERIFIED`.
3. **Strict proof verdict.** Only a SymbiYosys `PASS` marker **and** zero exit
   status return `VERIFIED`. `FAIL` returns `INVALID` and preserves bounded,
   trace-like counterexample artifact paths. A conflicting marker/outcome or
   tool error returns `ERROR`.
4. **Provenance and reproducibility.** Results retain job/task, `.sby` SHA-256,
   stable run identity, output directory, exact argv, tool version, bounded
   stdout/stderr tails, status markers, and any counterexample artifacts.
   Scenario IDs from the UCM path spec are retained in this evidence.
5. **Configuration and CLI integration.** New optional top-level `formal:`
   config selects `conservative` (default) or `symbiyosys`, the executable,
   work directory, timeout, and proof mappings. `rca validate`, `rca report`,
   and `rca coverage` build this backend and pass it through the existing
   validation pipeline.
6. **Validation integration.** `run_validation(..., formal_backend=...)` is
   additive and remains backward compatible. A formal counterexample emits the
   blocking `EXCEPTION_FORMAL_INVALID` finding; a backend/configuration error
   emits blocking `EXCEPTION_VERIFICATION_ERROR`; unresolved evidence remains
   the pre-existing non-blocking `EXCEPTION_UNVERIFIED` finding.

## Architecture decisions

- **User-authored `.sby` collateral remains authoritative.** An SDC exception
  is not enough information to safely manufacture a formal property. RCA
  transports selector/scenario context as provenance only; the proof’s RTL
  assumptions, assertions, and MCMM setup remain user-owned.
- **One existing formal and validation architecture.** The new adapter
  subclasses `FormalBackend`; `verify_exceptions()` remains the sole formal
  orchestration path; all outcomes are reported through the existing
  `ValidationIssue` / `ValidationReport` data model.
- **No vendor coupling in core models.** UCM and SDC remain vendor-neutral.
  SymbiYosys-specific command/status handling is isolated in
  `rca.exceptions.symbiyosys`.
- **Fail closed.** `sby -f` is used only on a deterministic child directory
  verified to be below the configured `formal.work_dir`; it never modifies the
  user-authored `.sby` source. Ambiguous success cannot become a proof.

## Files added

- `src/rca/exceptions/symbiyosys.py` — concrete backend, strict status parser,
  provenance capture, configuration factory.
- `tests/unit/test_symbiyosys.py` — 11 deterministic Step-14 scenarios using a
  local fake `sby` executable.
- `STEP14_REPORT.md` — this milestone report.
- `docs/decisions/ADR-003-symbiyosys-formal-adapter.md` — formal-property and
  backend-isolation decision record.

## Files modified

- `src/rca/config/model.py`
- `src/rca/config/schema.py`
- `configs/schemas/project.schema.json`
- `src/rca/exceptions/__init__.py`
- `src/rca/exceptions/formal_backend.py` (lint-only unused-import cleanup)
- `src/rca/exceptions/verifier.py`
- `src/rca/validation/engine.py`
- `src/rca/validation/exceptions.py`
- `src/rca/utils/enums.py`
- `src/rca/cli/main.py`
- `README.md`
- `DOCUMENTATION.md`
- `TEST_PLAN.md`
- `docs/references.md`

## Step-14 tests

`tests/unit/test_symbiyosys.py` covers:

1. PASS is the only `VERIFIED` outcome.
2. FAIL becomes `INVALID` and preserves trace paths.
3. UNKNOWN and missing status marker remain `UNRESOLVED`.
4. Missing mapping, job, or executable remain `UNRESOLVED`.
5. Timeout and a PASS/non-zero-exit discrepancy cannot verify.
6. A false-path/multicycle mapping mismatch is explicit `ERROR`.
7. Multicycle proof identity includes its cycle count.
8. Stable run identity, scenario provenance, and UCM immutability.
9. Existing validation blocks a formal counterexample and retains provenance.
10. YAML config/path resolution and factory construction.
11. Duplicate proof mappings are rejected deterministically.

## Deliberate limitations

- RCA does **not** generate formal assertions, derive clock assumptions, or
  prove arbitrary timing semantics from an SDC selector. The mapped `.sby`
  collateral must do that and is reviewed/owned by the user.
- RCA does not bundle SymbiYosys, Yosys, SMT solvers, or a PDK. A missing `sby`
  installation is reported as `UNRESOLVED`, not as a test pass or a proof.
- A single UCM exception maps to one proof job. For MCMM-specific proof
  assumptions, users encode the mode/corner or select a suitable task in their
  `.sby` collateral; RCA preserves `scenario_ids` in the recorded proof input
  rather than inventing an MCMM property.
- Formal execution is intentionally opt-in (`formal.backend: symbiyosys`). The
  default conservative backend preserves all existing Step-8/13 behavior.

## Test results

Executed with the project’s pinned editable environment (Python 3.11.2 and
`pyslang` installed):

| Gate | Command | Result |
|---|---|---|
| Step 11 regression | `python -m pytest tests/unit/test_pareto.py -q` | **125 collected / 125 passed** |
| Step 12 regression | `python -m pytest tests/unit/test_mcmm.py -q` | **70 collected / 70 passed** |
| Step 13 regression | `python -m pytest tests/unit/test_validation_step13.py -q` | **40 collected / 40 passed** |
| Step 14 | `python -m pytest tests/unit/test_symbiyosys.py -q` | **11 collected / 11 passed** |
| Full suite | `python -m pytest -q` | **811 collected / 811 passed / 0 failed / 0 skipped / 0 errors** |

The focused compatibility gate also passed:
`test_symbiyosys.py + test_exceptions.py + test_validation.py +
test_validation_step13.py` ⇒ **175 collected / 175 passed**.

Ruff passed for the Step-14 implementation and integration modules; mypy passed
for the four type-sensitive new/integrated source modules (`symbiyosys`, config,
validation engine, validation exception layer).
