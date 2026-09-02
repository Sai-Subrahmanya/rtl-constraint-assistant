# Step 10 Report — Real EDA Closed-Loop Backend (WP-M/N)

This step implements the closed-loop EDA flow:
`RTL → Yosys synthesis → gate netlist → Liberty + generated SDC →
OpenSTA → parsed timing/QoR → run manifest/artifacts`, with MockEDA
kept for unit tests (clearly labeled) and deterministic hashing for
reproducibility/cache.

## 1. Files modified

Existing files extended in place (no duplicate/v2 modules):

- `src/rca/utils/enums.py` — added `RunStatus`, `PowerStatus`,
  `BackendKind` enums.
- `src/rca/eda/base.py` — rewrote `ToolBackend` with deterministic
  executable resolution (explicit > env var > PATH > project-local
  dirs), safe argv subprocess (`_safe_run`, never `shell=True`),
  `ToolInfo` with availability/host/platform/error, and
  `CommandRecord` capturing exact argv, cwd, rc, stdout/stderr tails,
  duration for the run manifest.
- `src/rca/eda/yosys/backend.py` — rewrote Yosys backend: env var
  `RCA_YOSYS`/project-local discovery, version capture via `-V`,
  proper Liberty-aware script (hierarchy → proc/opt/fsm/memory →
  dfflibmap → abc -liberty when Liberty present, techmap otherwise),
  structured `SynthResult` with netlist/log/script/stats/command/tool
  info, yosys-stat parsing (cell/FF/area), failure classification
  without raising (returns structured error).
- `src/rca/eda/opensta/backend.py` — rewrote OpenSTA backend: env var
  `RCA_OPENSTA` discovery, auto-detects `openroad` fallback,
  generates Tcl script that reads Liberty/netlist/SDC and writes
  setup/hold/WNS/TNS/check reports, runs via argv subprocess, classifies
  BLOCKED vs STA_FAILED vs TIMING_FAIL vs SUCCESS, refuses to run on
  PARTIAL/BLOCKED SDC unless exploratory flag set, clearly returns
  BLOCKED when Liberty is missing (never fabricates timing).
- `src/rca/eda/common/mock.py` — MockEDA clearly labels every QoR as
  `is_mock=True`, `backend="mock"`, with MOCK note; never confused
  with real OpenSTA.
- `src/rca/eda/flow.py` (new, see §2) — end-to-end `run_flow()`
  orchestrator including cache lookup, stale-artifact-aware manifest
  writing, status rollup, and structured return dict.
- `src/rca/eda/__init__.py` — exports `run_flow`, backends,
  `CommandRecord`, `ToolInfo`, `blocked_result`.
- `src/rca/qor/model.py` — expanded `QoRResult` with Step 10 §14
  metadata (run_id, candidate_id, backend, backend_version,
  flow_stage, scenario, is_mock), per-metric fields (critical_setup/
  critical_hold paths, area vs area_proxy, ff_count, comb_cell_count,
  power UNKNOWN default), `Feasibility` class separating BLOCKED
  (can't run) from TIMING_FAIL (bad timing), back-compat aliases for
  `area_total`/`power_total`/`buffer_count`.
- `src/rca/reports/timing.py` — strengthened OpenSTA report parser:
  extracts setup/hold slacks, WNS/TNS (treated as ns per OpenSTA
  default), critical paths with startpoint/endpoint/launch clock/
  capture clock/path group/slack, cell/area stat lines, and correctly
  handles multiple path sections without global-sort or fabricated
  metrics.
- `src/rca/cli/main.py` — replaced legacy inline `run-sta` body with a
  call to `run_flow()`, pretty-prints Status/Tool versions/Timing/QoR/
  Diagnostics with correct coloring and exit codes; added `rca doctor`
  command reporting Python/RCA/pyslang/yosys/opensta/Liberty/config
  status.
- `tests/unit/test_eda.py` — 18 new tests covering discovery, version
  capture, no-shell invocation, missing-tool blocking, missing-Liberty
  blocking, mock labeling, timing parsing, feasibility separation,
  area/area_proxy, power UNKNOWN, synth-stat parsing, manifest
  completeness, blocked-without-tools flow, cache-hit lookup,
  cache invalidation by Liberty/tool version.

## 2. New files (justified)

- `src/rca/eda/flow.py` — closed-loop orchestration. The existing
  `src/rca/eda/` package did not contain a flow orchestrator, and the
  repository rule allows a genuinely new file when no suitable
  location exists. It does not duplicate any backend module.
- `tests/unit/test_eda.py` — new EDA test file because no existing
  test file covered EDA at all (the repository rule permits extending
  existing test suites; creating an additional focused test module is
  consistent with how exception/equivalence tests are organized).

## 3. Yosys flow

- Discovers binary from `eda.yosys_executable`, env var `RCA_YOSYS`,
  PATH, or `tools/`, `.tools/`, `eda_tools/`, `bin/` under project
  root.
- Builds a deterministic Yosys script:
  1. `verilog_defaults -add -D<define>` and `-I<incdir>` as configured
  2. `read_verilog -sv` for each source
  3. `read_liberty -lib` per supplied `.lib`
  4. `hierarchy -check -top <top>`
  5. `proc; opt; fsm; opt; memory; opt`
  6. `techmap; opt; dfflibmap -liberty <lib>; abc -liberty <lib>; opt;
     clean` (mapped flow when a Liberty is present) **or**
     `techmap; opt; clean` (generic, for bring-up only)
  7. `stat -liberty <lib>` (mapped) or `stat -top <top>` (generic)
  8. `write_verilog -noattr <netlist>`
- Returns `SynthResult` with: netlist path, log path, script path,
  parsed stats (cell_count, ff_count, area/area_proxy, area_is_proxy),
  ToolInfo, CommandRecord, success flag, and error message (if any).
- Command invocation is via argument list (`[yosys_bin, "-s", script]`),
  `cwd` set to the work directory, timeout, stdout/stderr captured —
  never `shell=True`.
- Failures produce a structured `SynthResult.success=False`; flow
  orchestrator maps them to `RunStatus.SYNTHESIS_FAILED`.

## 4. OpenSTA flow

- Discovers binary from `eda.opensta_executable`, env var `RCA_OPENSTA`,
  PATH, project-local dirs, with automatic `openroad -sta` fallback.
- BLOCKs when:
  - STA executable unavailable (BLOCKED)
  - No Liberty supplied (BLOCKED, not silent mock timing)
  - Netlist or SDC missing (BLOCKED)
  - SDC generation status is PARTIAL/BLOCKED unless
    `allow_partial_sdc=True` (exploratory mode)
  - OpenSTA exits non-zero → STA_FAILED
- Writes a Tcl script (`read_liberty`, `read_verilog`, `link_design`,
  `read_sdc`, `report_checks -path_delay max/min …`, `report_wns`,
  `report_tns`, `check_setup`) and invokes via argv list with timeout.
- Parses setup/hold reports into a `QoRResult` (WNS/TNS/violations/
  critical paths), marks `power_status=UNAVAILABLE`, and classifies
  the run as SUCCESS / TIMING_FAIL / STA_FAILED / BLOCKED.
- Exact CommandRecord is recorded for the manifest.

## 5. QoR schema

`QoRResult` fields (see `src/rca/qor/model.py`):

- Run metadata: `run_id`, `candidate_id`, `backend`, `backend_version`,
  `flow_stage`, `scenario`, `is_mock`.
- Timing: `setup_wns`, `setup_tns`, `setup_violations`, `hold_wns`,
  `hold_tns`, `hold_violations`, `whs`, `ths`, `near_critical_count`,
  `path_count`, `critical_setup`, `critical_hold` (each with startpoint,
  endpoint, launch/capture clock, path group, slack, path_type).
- Area/cells: `area` (real mapped area when Liberty used),
  `area_proxy` (cell count when no Liberty), `cell_count`, `ff_count`,
  `comb_cell_count`, `buf_count`.
- Power: `power=None`, `power_status=UNAVAILABLE` (Step 10 §13 — no
  fabricated power numbers; future power integration sets this
  explicitly).
- Diagnostics/notes: `diagnostics`, `notes`, `raw_report_text`,
  `feasibility` (populated via `Feasibility.from_qor`).
- Back-compat aliases: `area_total`, `power_total`, `buffer_count`,
  `area_comb`, `area_seq`, `area_buffer`, `power_dynamic`,
  `power_leakage` retained for pareto/optimizer/tests.

`Feasibility` is deterministic:
- BLOCKED (missing tool/library/netlist/SDC/fatal error)
- TIMING_FAIL (setup_violations or hold_violations present)
- SUCCESS (setup and hold pass)

## 6. Run manifest schema

Every run writes `results/runs/<run-id>/run_manifest.json` containing:

- `candidate_id`, `timestamp`
- `rtl_hash` (per-source SHA-256), `sdc_hash`, `config_hash`
- `tool`, `tool_version` (composite `yosys:<ver>|opensta:<ver>`)
- `flow_stage`, `corner` (scenario)
- `library` (comma-separated list of liberty paths)
- `artifacts`: relative paths to sdc, netlist, synth script/log/stats,
  sta log, qor.json
- `extra.cache_key`: deterministic cache key used for run reuse
- `extra.commands.{yosys,opensta}`: CommandRecord (argv, cwd,
  timeout, returncode, stdout/stderr tail, duration)
- `extra.tool_info.{yosys,opensta}`: ToolInfo dicts (vendor, version,
  executable, host, platform, availability, capabilities, error)
- `extra.diagnostics`: list of diagnostic strings (missing tools,
  missing Liberty, partial SDC warnings, etc.)

## 7. Cache key

Cache key = `stable_hash({rtl, sdc, cfg, libs, yosys_bin, yosys_ver,
sta_bin, sta_ver})`.

- Cache HIT: prior run_dir with identical `extra.cache_key` is reused
  instead of rerunning synthesis/STA.
- Cache invalidated if any of: RTL hash, SDC hash, config hash,
  Liberty file hash, Yosys binary/path, Yosys version, OpenSTA binary/
  path, or OpenSTA version differ.
- No Python `hash()` is used; all digests use the project's
  `stable_hash`/`hash_file` (SHA-256 based).
- Stale-artifact protection: SDC is written fresh in the run_dir
  alongside netlist; hashes are computed from the exact files passed
  to tools, not from stale sources.

## 8. Tests added/modified

- New file `tests/unit/test_eda.py` with 18 tests covering:
  1. explicit-path tool discovery
  2. environment-variable discovery
  3. missing-tool reporting
  4. version capture
  5. safe subprocess (no shell=True) — argument injection string
     passed literally
  7. OpenSTA missing-Liberty → BLOCKED
  10. setup/hold/WNS/TNS parsing from a representative OpenSTA report
  11. setup/hold feasibility independent
  12. Yosys stat parsing (cell count + area)
  16. power UNAVAILABLE by default
  17. area vs area_proxy distinction
  18. manifest completeness via mock flow
  19. real flow is BLOCKED when tools unavailable (no silent mock)
  20/21/22. cache-hit retrieval, Liberty-hash invalidation,
  tool-version invalidation.
- All existing equivalence/exception/pareto tests continue to pass
  (334 tests in the non-pyslang core suite).
- The CLI `rca doctor` provides environment diagnostics; `rca run-sta`
  uses the new flow.

## 9. Exact test commands

```
$ python -m pytest tests/unit/test_eda.py -q
============================== 18 passed in 0.28s ==============================

$ python -m pytest tests/unit/test_exceptions.py tests/unit/test_equivalence.py \
      tests/unit/test_eda.py tests/unit/test_constraints.py \
      tests/unit/test_sdc_parser.py tests/unit/test_pareto.py \
      tests/unit/test_units.py tests/unit/test_validation.py \
      tests/unit/test_sdc_generation.py tests/unit/test_ucm_provenance.py -q
============================= 334 passed in 1.26s ==============================
```

- **collected (non-pyslang core)**: 334
- **passed**: 334
- **failed**: 0
- **skipped**: 0
- **errors**: 0
- **BLOCKED_BY_EXTERNAL_TOOL (test collection level)**: 0 (real-tool
  tests are not present because neither `yosys`, `sta`, nor `openroad`
  are installed in the sandbox; the EDA flow is covered structurally
  via the fake-tool and CLI tests, and run_flow returns structured
  BLOCKED when binaries can't be found).

Remaining failures in `test_parser.py`, `test_timing_model.py`,
`test_sdc_import.py`, `test_inference.py`, `test_connectivity.py`,
`test_expression_semantics.py`, and `test_determinism.py` are the
pre-existing `RuntimeError: pyslang is not installed` environment
issue — not Step-10 regressions.

## 10. Real-tool integration result

This sandbox does not have `yosys`, `sta`, or `openroad` installed,
and no compatible Liberty (.lib) file is shipped. In this environment:

- `rca run-sta examples/simple_counter/project.yaml` →
  `Status: BLOCKED` with diagnostics explaining exactly which tools
  are missing, how to configure them (env vars `RCA_YOSYS` /
  `RCA_OPENSTA` or `eda.yosys_executable`/`eda.opensta_executable` in
  project.yaml), and that Liberty must be supplied via `flow.liberty`.
- The CLI does NOT fall back to MockEDA unless the user passes
  `--backend mock` explicitly; Mock results are labeled `MOCK` in the
  CLI and in every QoRResult.
- No shell is invoked; no command injection is possible via design
  name, paths, or SDC text; argv arrays only.

When Yosys and OpenSTA (plus a matching Liberty for the standard-cell
set Yosys maps to) are available, the same `run_flow()` will:
synthesize → write netlist → run STA → populate timing/area/cell
metrics → write the run manifest. The `doctor` command will report
the discovered versions and paths.

## 11. Known environment-dependent limitations

- Real synthesis/STA results are only produced when both Yosys and
  OpenSTA are installed and a compatible Liberty is configured;
  otherwise the flow reports BLOCKED with explicit reasons.
- Power remains `UNAVAILABLE`; no credible open-source power flow is
  wired in yet. This is deliberate — zero/placeholder power numbers
  would poison future optimization.
- SDF/SPEF/MCMM corners are not exercised yet (post-route/parasitics
  belong to later steps). Only a single scenario/corner is run per
  invocation in this step.
- `yosys`'s SV parser is not the authoritative RTL frontend (Step-2/3
  remains); the Yosys call here is strictly for synthesis-to-gates.

---

# Step 10 — Correction Pass (20 items)

This section documents corrections applied per user 20-item list. All changes
extend existing files in place; no new framework/module was introduced.

## Files modified (correction pass)

- `src/rca/utils/hashing.py` — added `hash_text(text) -> str` (SHA-256 hex of
  UTF-8 text) for script-hashing.
- `src/rca/artifacts/manager.py` — `RunManifest` now carries three additional
  fields persisted through `to_dict()`/`from_dict()`:
  - `artifact_hashes: dict[str,str]` — per-artifact hashes.
  - `tool_identity: dict` — structured {vendor, tool, executable, version}.
  - `input_hashes: dict` — RTL/include/lib/SDC input hashes captured at run.
- `src/rca/eda/yosys/backend.py` — split script generation into
  `_build_script()` for deterministic script construction (also threads
  `parameters` via `hierarchy -P<k>=<v>`), added static
  `script_semantic_key(sources, liberty, defines, include_dirs, parameters,
  source_hashes, lib_hashes)` producing a canonical tuple (sorted defines,
  sorted include_dirs, sorted stringified parameters, sorted lib hashes,
  liberty-mode flag); records `script_hash` into synth stats; Liberty-mode
  uses `stat -liberty <lib>` (previously used `stat -top` for both modes).
- `src/rca/eda/opensta/backend.py` — split Tcl generation into `_build_script()`
  returning `(tcl_text, tcl_path, report_paths)`; added static
  `script_semantic_key(netlist, sdc, liberty, top, corner, netlist_hash,
  sdc_hash, lib_hashes)` returning a canonical tuple; accepts an optional
  `prebuilt_script=` kwarg so `run_flow()` can build the script once before
  the cache lookup and reuse it (avoiding run-dir-dependent script text);
  generated Tcl uses basenames for netlist/SDC/report redirect targets so
  script text is independent of `run_dir`/`run_id` (script is run with
  `cwd=work_dir`); `check_setup` is documented as setup/constraint-integrity
  only — **does NOT validate hold**. Hold is validated explicitly with
  `report_checks -path_delay min`, `report_wns -min`, `report_tns -min`.
  That policy tag is embedded in the canonical script fingerprint and
  recorded as a QoR note (`sta_script_hash=<h>; hold validated via explicit
  -path_delay min reports; check_setup is setup/constraint-integrity only`).
- `src/rca/eda/yosys/backend.py` — `write_verilog` now uses `netlist.name`
  (basename) so script text is independent of `work_dir`.
- `src/rca/eda/flow.py` — full rewrite in place (no new module), corrected so
  that one canonical experiment cache key is computed ONCE before cache lookup
  and used identically for lookup, manifest storage, and the returned
  `cache_key` (no post-synthesis key mutation):
  - Canonical `_extract_cfg(cfg, **overrides)` pulls top, defines (list|dict),
    include_dirs, parameters, backend, stage, liberty, safe_mode, corner,
    scenario from ProjectConfig (with caller overrides taking precedence).
  - `_coerce_defines`, `_coerce_parameters` normalize to canonical dicts with
    all values stringified for deterministic hashing.
  - `_hash_dir_if_exists(d)` recursively hashes include-directory contents via
    the project's `hash_directory()`, so include-content changes (not just path
    changes) invalidate cache.
  - Path helpers `_rel(run_dir, p)`, `_artifacts_to_rel(run_dir, artifacts)`,
    `_resolve_rel(run_dir, p)` enforce **run-relative / portable** artifact
    paths in the manifest (absolute paths only kept for tool executables in
    `tool_identity`, which is metadata).
  - `_hash_artifacts(run_dir, artifacts)` hashes `_HASHED_ARTIFACTS`
    (`sdc, netlist, synth_script, synth_stats, sta_tcl, sta_checks,
    sta_setup_rpt, sta_hold_rpt, qor`) with SHA-256 and records size-only for
    `_LOG_ARTIFACTS` (`synth_log`, `sta_log`) to avoid hashing large,
    version-varying console output while still recording enough info to detect
    gross tampering.
  - Cache key v2 uses the project's `stable_hash()` over a canonical dict
    (see §Final cache-key definition below). Both the Yosys synthesis script
    and the OpenSTA Tcl are built **before** the cache lookup using basenames
    for run-local paths (so script text is independent of `run_dir`/`run_id`);
    the pre-built STA Tcl is passed to `OpenSTABackend.run_sta()` via
    `prebuilt_script=` so it is not regenerated with absolute paths after
    synthesis. The netlist content hash (`netlist_hash`) is an OUTPUT — it is
    recorded in `artifact_hashes` and `extra.netlist_hash` for integrity but
    is **never** mixed into the experiment cache key.
  - `_find_cached_run(am, cache_key, run_dir_parent)` now performs integrity:
    1. iterates existing run manifests;
    2. skips non-JSON / malformed / non-matching cache_key;
    3. **resolves manifest artifact paths relative to the run_dir** (not CWD);
    4. verifies all listed artifacts exist on disk;
    5. verifies hashes for `_HASHED_ARTIFACTS` match `artifact_hashes` in the
       manifest (logs `CACHE_INVALID` with reason, returns None → MISS);
    6. requires a completed-run entry to contain `sdc`, `netlist`, and `qor`
       (BLOCKED/FAILED manifests naturally lack one or more of these and are
       skipped without being treated as hits; they do not trigger
       CACHE_INVALID because they are not advertised as completed).
    7. does NOT auto-delete the stale run.
  - The CACHE_HIT return includes `cache_key` and `status=CACHE_HIT` symmetric
    with fresh-run returns.
  - Mock path writes a netlist stub and a portable manifest with hashes so
    integrity checks also pass for mock cache hits.
  - BLOCKED and SYNTHESIS_FAILED paths also write portable manifests
    capturing `tool_identity`, `input_hashes`, `cache_key` (so diagnosis from
    a partial run is deterministic and replayable).
  - `parameters` are threaded to `YosysBackend.synthesize()` via `hierarchy
    -P<k>=<v>` so elaboration-time parameters are part of synthesis identity.

## Final cache-key definition (v2)

`cache_key = stable_hash(cache_key_data)` where `cache_key_data` is a
canonical JSON-serializable dict with the following keys:

- `version`: `2` (bumps invalidate pre-correction runs).
- `rtl`: `{<resolved-path-str>: <sha256>}` — hashes of every source file
  (sorted by path).
- `includes`: `{<resolved-dir-str>: <recursive-dir-hash>}` — content hash of
  each include directory, not just path (empty string if missing).
- `sdc`: `<sha256>` of generated SDC text.
- `libs`: `{<resolved-lib-path-str>: <sha256>}` — hashes of every Liberty file
  used for synthesis + STA (sorted).
- `cfg`: `{top, defines(sorted k→v), include_dirs(sorted), parameters(sorted
  k→string(v)), backend, stage, safe_mode, corner, scenario,
  allow_partial_sdc, sdc_generation_status}` — canonical configuration.
- `tool`: `{yosys_bin, yosys_ver, sta_bin, sta_ver}` — executable path AND
  probed version for both tools (path is included so swapped symlinks do not
  silently alias; version captures real binary identity).
- `synth_script_hash`: `<sha256>` of the canonical Yosys script text.
- `synth_semantic`: canonical tuple from `YosysBackend.script_semantic_key()`
  (sorted-defines/includes/parameters/liberty-mode fingerprint).
- `sta_script_hash`: `<sha256>` of the canonical STA Tcl text (built with
  basename-only references to netlist/SDC/report files, so script text is
  run-dir independent).
- `sta_semantic_pre`: canonical tuple from
  `OpenSTABackend.script_semantic_key()` (netlist-name, sdc-name, libs with
  hashes, top, corner, + command fingerprint including the explicit
  hold-min-report policy).

The netlist hash is deliberately **NOT** part of this key: netlist is an
**output** of the experiment; its content is verified on lookup via
`artifact_hashes.netlist` (integrity), not used for experiment identity.

NOT used: Python built-in `hash()`, `repr()`, timestamps, run_id, run_dir
path, or any output/content hashes (netlist, QoR, reports, logs).

## Artifact-integrity policy (cache hit)

On lookup, if any of the following is true, the run is logged as
`CACHE_INVALID:<reason>` and treated as a MISS:

- required artifact (`sdc`, `qor`, any artifact listed in the manifest)
  missing on disk;
- hash in manifest differs from the file's current SHA-256 (for
  `_HASHED_ARTIFACTS`);
- size mismatch for log-only artifacts;
- manifest cache_key does not match the requested key.

Stale runs are NOT auto-deleted.

## Manifest path convention

- All artifact references in `RunManifest.artifacts` are **run_dir-relative
  POSIX strings** (e.g. `generated.sdc`, `top_synth.v`, `synth.ys`, `sta.tcl`,
  `sta_checks.rpt`, `qor.json`).
- Lookup always resolves paths relative to the manifest's run directory
  (`runs/<run_id>/`), not CWD.
- Absolute paths are retained only inside `tool_identity` (executable
  metadata), not as primary artifact references.

## Hash policy

- Content-hashed (SHA-256): SDC, netlist, synth script, synth stats JSON,
  STA Tcl, STA checks report, STA setup report, STA hold report, QoR JSON.
- Size-only (fast integrity, no content guarantee): synth log, STA log (these
  are noisy/version-dependent and potentially large).
- Inputs: RTL files (SHA-256), Liberty files (SHA-256), include directories
  (recursive content hash via `hash_directory`).
- Identity hash for scripts: SHA-256 of the **canonical generated script
  text** (no run-id, no temp-paths, sorted defines/params/includes).

## Hold-validation policy

- `check_setup` in OpenSTA reports setup/constraint integrity only — RCA
  does NOT treat it as evidence of hold closure.
- RCA computes hold WNS/TNS from **explicit min-mode reports**:
  `report_checks -path_delay min`, `report_wns -min`, `report_tns -min`.
- The generated STA Tcl always emits those reports, and the semantic key
  contains a command fingerprint that includes `report_checks(max/min)`,
  `report_wns/tns(min/max)` so any regression that drops hold reports will
  change the cache key.

## Real vs Mock

- `backend=mock` (or MockEDA) produces clearly-labeled results with
  `is_mock=True`, `backend="mock"`, a MOCK note, and a stub netlist so
  cache/integrity round-trips work for mock runs too.
- `backend=yosys_opensta` NEVER silently falls back to MockEDA. If Yosys or
  OpenSTA are unavailable on the requested path/PATH/env-var, the run
  returns `BLOCKED` (with diagnostics, tool identity, and a portable manifest).
- Power is always `None` / `PowerStatus.UNAVAILABLE` — not fabricated.
- Yosys failures on SV that RCA parsed are classified as
  `SYNTHESIS_FAILED` (tool/version/rc/log/diagnostic captured), not
  "parser failure".

## Regression tests added (in existing `tests/unit/test_eda.py` only)

34 tests total in test_eda.py (up from 18). Key tests:

- `test_20_cache_hit_on_complete_valid` — complete, hash-matching run → HIT.
- `test_20b_missing_netlist_cache_miss` — missing netlist file → MISS.
- `test_20c_modified_sdc_cache_miss` — SDC tampered → MISS.
- `test_20d_hash_mismatch_qor_cache_miss` — QoR hash mismatch → MISS.
- `test_defines_change_invalidates_cache` — defines WIDTH=8 vs 16 → different
  cache keys.
- `test_include_dir_change_invalidates_cache` — include dir content differs
  → different cache keys.
- `test_parameter_change_invalidates_cache` — elaboration parameter W=8 vs 16
  → different cache keys.
- `test_synth_script_identity_changes` — synth script text differs between
  liberty vs no-liberty flows; `hash_text` differs.
- `test_manifest_artifact_paths_are_relative` — mock manifest paths are
  portable; `_resolve_rel` against run_dir finds them.
- `test_relative_paths_resolve_correctly` — `_resolve_rel` handles relative
  and absolute inputs.
- `test_hold_policy_explicit_in_sta_script` — generated Tcl contains
  `report_checks -path_delay min`, `report_wns -min`, `report_tns -min`,
  `check_setup`; script semantic key fingerprints hold-min reports.
- `test_mock_is_clearly_labeled` — mock returns `is_mock=True`, status MOCK.
- `test_yosys_failure_is_synthesis_failed` — Yosys that passes version probe
  but fails during synthesis → SYNTHESIS_FAILED (not BLOCKED, not parser
  error).
- `test_power_unknown_when_real` — QoR defaults: `power=None`,
  `power_status=UNAVAILABLE`.
- **`test_cache_hit_then_invalidation_then_restore_e2e`** — end-to-end with
  functional fake yosys/opensta executables: (a) first run SUCCESS →
  canonical cache_key stored; (b) second identical run → CACHE_HIT with the
  same key; (c) tamper netlist → experiment key UNCHANGED but integrity
  fails → MISS (fresh execution); (d) restore netlist → CACHE_HIT again.
  This is the primary regression proving key identity is separated from
  output integrity.
- `test_input_change_changes_key_but_output_tamper_does_not` — SDC change
  changes the key; netlist (output) tamper leaves the key unchanged.

## Test results (correction pass, final)

- `python -m pytest tests/unit/test_eda.py -q`: **34 collected, 34 passed,
  0 failed, 0 errors, 0 skipped.**
- `python -m pytest -q` (full suite): **571 collected, 419 passed, 145 failed,
  7 errors, 0 skipped.** Every failure/error is pre-existing and caused by
  `pyslang not installed` (parser, inference, timing-model, sdc-import,
  connectivity, expression-semantics, determinism, golden timing); none are
  regressions from this correction pass. The EDA, CLI, qor, artifacts,
  hashing, manifest, and constraint-model test modules all pass.

## Environment limitations (unchanged)

- `pyslang` is not installed → parser/inference/timing-model/sdc-import/
  connectivity/expression-semantics/determinism/golden tests fail as before.
- `yosys`, `sta`, `openroad` are not on PATH in this sandbox; real-tool
  integration tests are covered by subprocess-safe invocation tests with
  fake executables and structural assertions on generated scripts.
- No sample Liberty cell library is shipped; Liberty-required flows return
  BLOCKED with a clear diagnostic when no Liberty is provided.
