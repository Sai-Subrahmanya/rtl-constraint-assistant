# Step 25 — Real EDA Flow Hardening & Reproducible Tool Environment

**Status:** implemented on the Step 25 branch; this document describes the
execution boundary and evidence contract, not a signoff claim.

## Scope and preserved authorities

Step 25 hardens the existing `yosys_opensta` boundary without changing the
Universal Constraint Model (UCM), SDC serialization, `QoRResult`, optimizer,
MCMM, SQLite history sidecar, cache identity, or Step 22/23 coordinator
semantics.

- UCM remains the canonical constraint truth; SDC is serialization only.
- `QoRResult` remains canonical QoR.
- The run artifact set and its `RunManifest` remain evidence authority.
- Filesystem manifest/hash validation remains cache authority.
- SQLite remains an advisory historical/query index and cannot create a cache
  hit or change a real-flow outcome.
- `mock` is deterministic and intentionally selected; it is labelled mock and
  is never a fallback for a real request.

No new optimizer, QoR, artifact, cache, database, scheduler, or commercial EDA
adapter authority is introduced.

## Typed readiness model

`src/rca/eda/preflight.py` supplies a vendor-neutral `CapabilityStatus`,
`CapabilityCheck`, and immutable `EDAPreflight`. Its vocabulary explicitly
includes:

- executable absent, found, and safely version-discovered;
- missing collateral, invalid configuration, ready environment, and ready
  output location;
- execution started/completed/failed/timed-out lifecycle vocabulary;
- missing/malformed output; and
- unsupported execution (including commercial execution).

Each detailed status also exposes one typed coarse operator classification:
`available`, `tool_missing`, `tool_unavailable`, `input_missing`,
`liberty_missing`, `config_invalid`, `backend_unsupported`,
`environment_invalid`, `permission_error`, or `precheck_failed`. This is
preflight metadata only; it does not create a global execution-state authority.

`preflight_yosys_opensta` checks the selected execution backend, non-empty top,
Yosys/OpenSTA executable and bounded version-probe result, RTL sources, include
directories, Liberty files, the generated SDC, and the planned run-output
locations. It observes only: it does not synthesize, run STA, or run formal.
A file that is executable but whose safe version probe did not complete is
reported as `executable_found` and is intentionally **not ready**. This fails
closed before real execution.

`preflight_symbiyosys` is similarly observational. SymbiYosys collateral is
required only when that formal backend is explicitly selected; otherwise its
unavailability is non-required diagnostic evidence.

## Real-flow execution contract

For `run_flow(..., backend="yosys_opensta")`, RCA writes the run-local SDC,
discovers versions with bounded argv-only probes, constructs deterministic
scripts/output names, computes the established cache identity, and then runs
preflight before executing Yosys or OpenSTA. A verified pre-existing cache hit
is retained evidence, not a new tool invocation or a mock fallback.

Each external command is executed with:

- a deterministic argument vector and working directory;
- `shell=False` and pinned `LC_ALL=C`, `LANG=C`, `TZ=UTC` diagnostic locale/time zone
  while preserving operator-supplied tool search/library environment;
- a configured `flow.tool_timeout_seconds` timeout (default 600 seconds);
- bounded stdout/stderr diagnostic tails;
- timeout process-group termination on POSIX, including descendants;
- return-code and terminal execution status evidence; and
- fresh-output and parser validation before QoR can be accepted.

The runtime environment map is not recorded in `CommandRecord`, diagnostics,
manifest, or environment fingerprint. Conventional credential-like command
options (`password`, `passphrase`, `secret`, `token`, or API-key forms) are
redacted from persisted argv and matching bounded output text. Command output
remains bounded; tool owners should still avoid causing secrets to be printed
by a tool through an unrelated channel.

Before a non-cache real invocation begins, RCA removes run-local stale netlist,
STA report, synthesis-stat, QoR, power, and log outputs. Backends also guard
their direct invocation paths. A stale file can therefore not turn a nonzero,
timed-out, missing-output, or malformed-output invocation into current success.

A real tool failure produces no successful `QoRResult`, power evidence, or cache
hit. Valid tool-produced timing that violates constraints remains the distinct
`TIMING_FAIL` QoR outcome; it is not a tool-execution failure.

## Manifest and provenance

Each run has **one** authoritative run-local manifest:

```text
<flow.output_dir>/runs/<run-id>/run_manifest.json
```

The existing `RunManifest` gained descriptive execution fields:
`execution_mode`, `execution_status`, `failure_classification`, and
`environment_fingerprint`. `execution_mode` is explicitly `MOCK` or `REAL`.
Its existing artifact/input/tool/extra evidence now
records real tool/version/backend identity, argv/cwd/timeout/return code and
bounded tails, preflight, source/Liberty/SDC/script/power inputs, current output
artifact hashes, QoR artifact, and configured power-report provenance.

The environment fingerprint is a deterministic SHA-256 over a schema tag,
RCA/Python/platform version, selected backend, tool vendor/version, Liberty
hashes, and execution configuration hash. It deliberately excludes environment
values, host name, timestamps, PIDs, random run paths, and secret-bearing data.
It is provenance only; it is not added as an arbitrary cache-key input. Tool,
input, script, and output evidence remain subject to the established cache
contract.

Legacy cache lookup can read older `manifest.json` files, but Step 25 writes no
second compatibility copy. New cache entries require a success or valid
`TIMING_FAIL` execution state plus required, hash-valid artifacts. A recorded
failed/blocked/timed-out status is never reusable.

## Operator diagnostics

```bash
rca doctor project.yaml
rca doctor project.yaml --json > doctor.json
```

`doctor` performs bounded version discovery and readiness checks only. JSON
reports RCA/Python, selected execution backend, configured project/SDC backend
context, Yosys/OpenSTA/SymbiYosys, RTL/include/Liberty/output checks, formal
readiness, and the explicit mock policy. Since doctor does not generate a run
SDC, it labels the expected SDC location non-required; real `run-sta` verifies
its generated SDC immediately before execution. An available version probe is
not a functional-flow, PDK-quality, or signoff claim.

`rca run-sta ... --backend yosys_opensta` prints retained preflight diagnostics
and exits non-zero if a prerequisite is unavailable. Users select
`--backend mock` deliberately for deterministic mock behavior.

## Controlled-fake and optional-real coverage

Default coverage contains no live-tool/Liberty dependency:

```bash
pytest tests/unit/test_eda.py tests/unit/test_eda_preflight.py \
       tests/unit/test_eda_subprocess_hardening.py \
       tests/integration/test_real_eda_boundary.py \
       tests/integration/test_pipeline_boundaries.py -q
```

It covers preflight classifications, invalid doctor JSON configuration, argv and
secret exclusion, POSIX descendant cleanup, mock separation, fake-real success,
nonzero/missing/malformed/timeout behavior, stale-output removal, power evidence,
manifest/provenance/fingerprint, cache reuse/invalidation, and failed-cache
rejection. Controlled fake programs test RCA’s subprocess contract only and are
not presented as Yosys/OpenSTA measurements.

Actual-real coverage is separately opt-in:

```bash
RCA_RUN_REAL_EDA=1 RCA_REAL_EDA_LIBERTY=/absolute/path/to/cells.lib \
  pytest tests/integration/test_optional_real_eda.py -m optional_real_eda -q
```

It skips only if opt-in, executable, or Liberty prerequisites are unavailable;
once available, an unexpected real flow failure fails the test. It proves only
that provisioned tool/collateral case.

## Docker review

The default `Dockerfile` target is an RCA/Python runtime. The separate
`open-source-yosys` target deliberately adds Yosys only. Neither target contains
OpenSTA, PDK/Liberty/activity collateral, or commercial software, and neither
downloads a tool at container runtime. Commercial tools are not bundled,
installed, emulated, or claimed. Synopsys/Cadence retain SDC rendering support
only; commercial execution is explicitly unsupported.

```bash
docker build -t rca .
docker build --target open-source-yosys -t rca:yosys .
docker run --rm -v "$PWD:/work" rca doctor /work/project.yaml --json
```

Use user-provisioned compatible executable and collateral paths for real EDA;
run `rca doctor` first. No Step 25 behavior claims signoff or PDK quality.

## Architecture decision record

No ADR is added. This step is a local hardening of the existing EDA boundary and
existing `RunManifest`; it makes no new architectural authority or decision.
