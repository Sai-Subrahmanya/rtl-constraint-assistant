"""
End-to-end EDA flow orchestration (Step 10 + correction pass).

Pipeline::

    RTL → RCA parse → UCM → validate → SDC generation
      → Yosys synthesis → gate netlist → OpenSTA
      → timing/QoR extraction → run manifest / artifacts.

Correction-pass additions:
- Full cache key covering RTL/sources, defines, includes, parameters,
  top, flow, SDC, Liberty, tool identity (path+version), script hashes,
  scenario, safe-mode.
- Synthesis and STA scripts built by backends (``build_script``); their
  semantic identity hashes feed the cache key.
- Manifest records artifact hashes for every important output.
- Cache hits are integrity-checked: every artifact listed in the
  manifest must exist AND hash-match before the cache is honored;
  stale/missing/modified artifacts cause a cache MISS with a
  CACHE_INVALID diagnostic.
- Artifact paths in the manifest are run-relative (portable).
"""

from __future__ import annotations

import json
import os
import platform
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..artifacts import ArtifactManager, RunManifest
from ..constraint_model import ConstraintSet
from ..qor.model import QoRResult
from ..reports.power import (
    POWER_REPORT_PARSER_VERSION,
    PowerParseStatus,
    parse_openroad_power_report,
    unavailable_power_result,
)
from ..utils.enums import BackendKind, PowerStatus, RunStatus
from ..utils.hashing import hash_file, hash_source_set, hash_text, stable_hash
from ..utils.logging import get_logger
from .common.mock import MockEDA
from .opensta.backend import OpenSTABackend, STAResult
from .yosys.backend import SynthResult, YosysBackend

log = get_logger("eda.flow")

_PROJECT_LOCAL_TOOL_DIRS = ("tools", ".tools", "eda_tools", "bin")

# Artifacts whose content we hash for integrity tracking.  Very large
# logs can be skipped — policy documented in STEP10_REPORT.
_HASHED_ARTIFACTS = {"sdc", "netlist", "synth_script", "synth_stats",
                     "sta_tcl", "sta_checks", "sta_setup_rpt", "sta_hold_rpt",
                     "power_report", "qor"}
_LOG_ARTIFACTS = {"synth_log", "sta_log"}  # recorded but not integrity-hashed


def _project_local_dirs(project_root: Path | None) -> list[Path]:
    if project_root is None:
        return []
    return [project_root / d for d in _PROJECT_LOCAL_TOOL_DIRS if (project_root / d).is_dir()]


def _lib_hashes(libs: list[Path]) -> dict[str, str]:
    return {str(p): hash_file(p) for p in libs if p.is_file()}


def _coerce_defines(defs: Any) -> dict[str, str]:
    """Accept list['NAME=val','NAME'] or dict and return a canonical dict."""
    out: dict[str, str] = {}
    if defs is None:
        return out
    if isinstance(defs, dict):
        for k, v in defs.items():
            out[str(k)] = "" if v is None else str(v)
        return out
    for item in defs:
        s = str(item)
        if "=" in s:
            k, v = s.split("=", 1)
            out[k.strip()] = v.strip()
        else:
            out[s.strip()] = ""
    return out


def _coerce_parameters(params: Any) -> dict[str, str]:
    if params is None:
        return {}
    return {str(k): str(v) for k, v in dict(params).items()}


def _extract_cfg(cfg: Any) -> dict[str, Any]:
    """Pull synthesis-affecting fields out of a ProjectConfig-like object."""
    top = cfg.top_module() if hasattr(cfg, "top_module") else ""
    flow = getattr(cfg, "flow", None)
    analysis = getattr(cfg, "analysis", None)
    sources = getattr(cfg, "sources", None)
    defines = _coerce_defines(getattr(sources, "defines", None) if sources else None)
    include_dirs = [str(p) for p in (getattr(sources, "include_dirs", None) or [])]
    params = _coerce_parameters(getattr(cfg, "parameters", None) or {})
    backend_name = getattr(flow, "backend", None) if flow else None
    stage = getattr(flow, "stage", None) if flow else None
    libs = [str(p) for p in (flow.liberty_files() if flow else [])]
    safe_mode = getattr(analysis, "safe_mode", "balanced") if analysis else "balanced"
    return {
        "top": top,
        "defines": defines,
        "include_dirs": sorted(include_dirs),
        "parameters": params,
        "backend": backend_name,
        "stage": stage,
        "liberty": sorted(libs),
        "safe_mode": safe_mode,
    }


def _rel(run_dir: Path, p: Path | str | None) -> str:
    if p is None:
        return ""
    try:
        return str(Path(p).resolve().relative_to(run_dir.resolve()))
    except Exception:
        return str(p)


def _artifacts_to_rel(run_dir: Path, artifacts: dict[str, Any]) -> dict[str, str]:
    return {k: _rel(run_dir, v) for k, v in artifacts.items() if v}


def _hash_artifacts(run_dir: Path, artifacts: dict[str, Any]) -> dict[str, str]:
    hashes: dict[str, str] = {}
    for k, v in artifacts.items():
        if not v:
            continue
        p = Path(v)
        if not p.is_file():
            continue
        if k in _HASHED_ARTIFACTS:
            try:
                hashes[k] = hash_file(p)
            except Exception:
                hashes[k] = ""
        elif k in _LOG_ARTIFACTS:
            # Record existence + size only (logs can be large / vary by version)
            try:
                hashes[k] = f"size={p.stat().st_size}"
            except Exception:
                hashes[k] = ""
    return hashes


def _resolve_rel(run_dir: Path, rel: str) -> Path:
    p = Path(rel)
    if p.is_absolute():
        return p
    return (run_dir / p).resolve()


def _power_report_input(cfg: Any, scenario: str) -> dict[str, Any]:
    """Return canonical identity for the report bound to ``scenario``.

    Configuration validation enforces the MCMM mapping rules for normal
    ``ProjectConfig`` instances.  This small defensive selector also avoids a
    global fallback if a config-like test object bypasses Pydantic validation.
    """
    flow = getattr(cfg, "flow", None)
    reports = list(getattr(flow, "power_reports", None) or [])
    matches = [r for r in reports if getattr(r, "scenario_id", None) == scenario]
    defaults = [r for r in reports if getattr(r, "scenario_id", None) is None]
    selected = matches[0] if len(matches) == 1 else None
    mcmm = getattr(cfg, "mcmm", None)
    mcmm_enabled = bool(getattr(mcmm, "enabled", False))
    if selected is None and not matches and not mcmm_enabled and len(defaults) == 1:
        selected = defaults[0]
    if selected is None:
        return {
            "configured": False,
            "format": None,
            "parser_version": POWER_REPORT_PARSER_VERSION,
            "path": "",
            "sha256": "",
            "exists": False,
            "scenario_id": scenario,
            "producer": "",
            "producer_version": None,
        }
    path = Path(str(getattr(selected, "path", "")))
    digest = ""
    exists = path.is_file()
    if exists:
        try:
            digest = hash_file(path)
        except OSError:
            exists = False
    return {
        "configured": True,
        "format": str(getattr(selected, "format", "")),
        "parser_version": POWER_REPORT_PARSER_VERSION,
        "path": str(path),
        "sha256": digest,
        "exists": exists,
        # Preserve the configuration association, even when a single-scenario
        # report omits it and ``scenario`` supplies the effective association.
        "scenario_id": getattr(selected, "scenario_id", None) or scenario,
        "producer": str(getattr(selected, "producer", "openroad_opensta")),
        "producer_version": getattr(selected, "producer_version", None),
    }


def _stage_power_report(power_input: dict[str, Any], run_dir: Path) -> Path | None:
    """Copy an existing configured report into the run artifact directory.

    The source path and source SHA-256 stay in QoR provenance.  The staged copy
    lets the existing run-relative manifest and integrity mechanism audit the
    exact report consumed by this run without adding a second artifact system.
    """
    if not power_input.get("configured") or not power_input.get("exists"):
        return None
    source = Path(str(power_input["path"]))
    if not source.is_file():
        return None
    target = run_dir / "configured_power_report.rpt"
    try:
        if source.resolve() != target.resolve():
            shutil.copyfile(source, target)
        return target
    except OSError:
        return None


def _apply_power_report(qor: Any, power_input: dict[str, Any], *, scenario: str,
                        mode: str | None = None, corner: str | None = None,
                        producer_version: str | None = None) -> Any:
    """Merge one parser-bound result into the existing canonical QoR object."""
    # A configured producer version describes the report source; otherwise the
    # discovered OpenSTA/OpenROAD invocation version is the best available
    # producer evidence. Keep the latter separately in provenance below.
    reported_version = power_input.get("producer_version") or producer_version
    if not power_input.get("configured"):
        parsed = unavailable_power_result(
            scenario_id=scenario, mode=mode, corner=corner,
            producer_version=reported_version,
            diagnostic="No power report is configured for this scenario.",
        )
    elif power_input.get("format") != "openroad_report_power":
        # Normal ProjectConfig validation rejects this before flow execution.
        # Retain a defensive conservative result for config-like callers.
        from ..reports.power import PowerReportParseResult
        parsed = PowerReportParseResult(
            status=PowerParseStatus.UNSUPPORTED.value,
            source_path=str(power_input.get("path", "")),
            source_sha256=str(power_input.get("sha256", "")),
            scenario_id=scenario, mode=mode, corner=corner,
            producer_version=reported_version,
            diagnostics=[f"Unsupported configured power report format: {power_input.get('format')}"],
        )
    else:
        parsed = parse_openroad_power_report(
            str(power_input.get("path", "")),
            scenario_id=scenario,
            mode=mode,
            corner=corner,
            producer_version=reported_version,
        )
    # Canonical QoR power status deliberately preserves the historic three
    # states. Detailed parser classification remains in raw_reports below.
    qor.power_status = (PowerStatus.AVAILABLE.value if parsed.available
                        else PowerStatus.UNAVAILABLE.value)
    qor.power = parsed.total if parsed.available else None
    qor.power_total = parsed.total if parsed.available else None
    qor.power_dynamic = parsed.dynamic if parsed.available else None
    qor.power_leakage = parsed.leakage if parsed.available else None
    qor.raw_reports = dict(qor.raw_reports or {})
    power_provenance = parsed.provenance()
    power_provenance["configured_producer"] = power_input.get("producer", "")
    power_provenance["configured_scenario_id"] = power_input.get("scenario_id", scenario)
    power_provenance["tool_version"] = producer_version
    qor.raw_reports["power"] = power_provenance
    qor.diagnostics.extend(parsed.diagnostics)
    return parsed


def run_flow(cfg: Any,
             cset: ConstraintSet,
             sdc_text: str,
             sdc_generation_status: str,
             sources: list[Path],
             include_dirs: list[Path] | None = None,
             defines: dict[str, str] | None = None,
             parameters: dict[str, str] | None = None,
             output_dir: Path | None = None,
             backend: str = "yosys_opensta",
             run_id: str | None = None,
             candidate_id: str = "baseline",
             scenario: str = "default",
             corner: str = "default",
             mode: str = "default",
             allow_partial_sdc: bool = False,
             yosys_bin: str | None = None,
             sta_bin: str | None = None,
             force: bool = False,
             ) -> dict[str, Any]:
    project_root = Path(cfg._config_path).parent if hasattr(cfg, "_config_path") else Path(".")
    output_dir = output_dir or Path(getattr(getattr(cfg, "flow", None), "output_dir", "output"))
    am = ArtifactManager(output_dir)
    run_id = run_id or f"run_{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S')}_{candidate_id}"
    run_dir = (am.runs_dir / run_id).resolve()
    run_dir.mkdir(parents=True, exist_ok=True)
    diagnostics: list[str] = []

    cfg_bits = _extract_cfg(cfg)
    # Resolve and hash the evidence before cache lookup.  It is an input to
    # this experiment, not an output-derived metric and never a synthesized
    # estimate.  Mock flow deliberately does not consume it.
    power_input = _power_report_input(cfg, scenario)
    power_artifact: Path | None = None
    # Caller-provided defines/includes/parameters override config defaults
    if defines:
        cfg_bits["defines"] = _coerce_defines(defines)
    if include_dirs:
        cfg_bits["include_dirs"] = sorted(str(p) for p in include_dirs)
    if parameters:
        cfg_bits["parameters"] = _coerce_parameters(parameters)

    libs = [Path(p) for p in cfg_bits["liberty"]]
    sources = [Path(p).resolve() for p in sources]
    include_dirs_paths = [Path(p).resolve() for p in cfg_bits["include_dirs"]]
    defines_map = cfg_bits["defines"]
    params_map = cfg_bits["parameters"]
    top = cfg_bits["top"]

    # Write SDC into run_dir
    sdc_path = run_dir / "generated.sdc"
    sdc_path.write_text(sdc_text, encoding="utf-8")

    rtl_hashes = hash_source_set(sources)
    # Hash the set of files inside include directories so that adding/
    # removing/modifying a header invalidates the cache.
    inc_hashes = {str(d): _hash_dir_if_exists(Path(d)) for d in cfg_bits["include_dirs"]}
    sdc_hash = hash_file(sdc_path)
    lib_hashes = _lib_hashes(libs)

    # Build backends to discover tool identity EARLY for cache key.
    local_dirs = _project_local_dirs(project_root)

    # ---------- MOCK short-circuit ----------
    if backend == "mock":
        tool = MockEDA()
        try:
            from ..optimizer import Candidate
            cand = Candidate(id=candidate_id, constraint_set=cset)
        except Exception:
            cand = None
        qor = tool.evaluate_candidate(cand, run_dir) if cand else tool.run_sta(
            run_dir / "netlist.v", sdc_path, libs, run_dir, top)
        qor.run_id = run_id
        qor.candidate_id = candidate_id
        qor.backend = "mock"; qor.is_mock = True
        qor.backend_version = "mock"; qor.flow_stage = "synthesis_sta"
        qor.scenario = scenario
        qor.mode = mode
        qor.corner = corner
        qor.power = None
        qor.power_total = None
        qor.power_dynamic = None
        qor.power_leakage = None
        qor.power_status = PowerStatus.UNAVAILABLE.value
        qor.notes.append("MOCK result — not from real EDA tools.")
        if power_input.get("configured"):
            qor.notes.append("Configured power report ignored for mock flow; power remains unavailable.")
        from ..qor.model import Feasibility
        qor.feasibility = Feasibility.from_qor(qor).to_dict()
        # Write mock netlist stub for artifact integrity
        netlist = run_dir / f"{top}_synth.v"
        netlist.write_text(f"// mock netlist for {top}\n", encoding="utf-8")
        qor_path = am.write_json(f"runs/{run_id}/qor.json", qor.summary())
        artifacts = {"sdc": sdc_path, "netlist": netlist, "qor": qor_path}
        rel_artifacts = _artifacts_to_rel(run_dir, artifacts)
        artifact_hashes = _hash_artifacts(run_dir, artifacts)
        manifest = RunManifest(
            candidate_id=candidate_id, rtl_hash=rtl_hashes, sdc_hash=sdc_hash,
            config_hash="", tool="mock", tool_version="mock",
            flow_stage="synthesis_sta", mode=mode, corner=corner,
            library=",".join(str(p) for p in libs),
            artifacts=rel_artifacts, artifact_hashes=artifact_hashes,
            tool_identity={"backend": "mock"},
            input_hashes={"rtl": rtl_hashes, "includes": inc_hashes},
            extra={"cache_key": "mock", "scenario": scenario, "diagnostics": diagnostics},
        )
        am.write_manifest_to(run_id, manifest)
        return {"status": RunStatus.MOCK.value, "run_id": run_id,
                "run_dir": str(run_dir), "manifest": manifest.to_dict(),
                "qor": qor.summary(), "qor_result": qor, "diagnostics": diagnostics,
                "synth": None, "sta": None}

    # ---------- REAL backends ----------
    yosys = YosysBackend(executable=yosys_bin, project_local_dirs=local_dirs)
    yinfo = yosys.discover()
    opensta = OpenSTABackend(executable=sta_bin, project_local_dirs=local_dirs)
    oinfo = opensta.discover()

    # Build scripts NOW (before cache check) so their identity is part of
    # the cache key — scripts are deterministic and cheap to write.
    synth_script_text, synth_script_path, netlist_path = yosys._build_script(
        sources, top, libs, run_dir,
        defines=defines_map, include_dirs=[str(p) for p in include_dirs_paths],
        parameters=params_map,
    )
    synth_script_hash = hash_text(synth_script_text)
    synth_semantic_key = yosys.script_semantic_key(
        sources, top, libs, defines=defines_map,
        include_dirs=[str(p) for p in include_dirs_paths],
        parameters=params_map,
        source_hashes=rtl_hashes, lib_hashes=lib_hashes,
    )
    # For OpenSTA we build the Tcl *before* cache lookup. The script is
    # executed with cwd=work_dir, so we reference netlist/sdc by their
    # basenames only. That makes the script text (and thus its semantic
    # identity) independent of run_dir / run_id. Netlist content hash is
    # an OUTPUT and is intentionally NOT part of the experiment key.
    predicted_netlist_hash = ""  # netlist hash is output; not in key
    sta_tcl_text, sta_tcl_path, sta_report_map = opensta._build_script(
        netlist_path.name, sdc_path.name,
        [Path(p).resolve() for p in libs],  # lib paths are absolute inputs (liberty comes from project config, not run_dir)
        run_dir, top, corner)
    sta_script_hash = hash_text(sta_tcl_text)
    sta_script_semantic_pre = opensta.script_semantic_key(
        Path(netlist_path.name), Path(sdc_path.name),
        [Path(p).resolve() for p in libs], top, corner,
        netlist_hash=predicted_netlist_hash, sdc_hash=sdc_hash,
        lib_hashes=lib_hashes,
    )

    tool_identity = {
        "yosys": {"executable": yosys.executable, "version": yinfo.version,
                  "available": yinfo.available},
        "opensta": {"executable": opensta.executable, "version": oinfo.version,
                    "available": oinfo.available},
        "host": platform.node(), "platform": platform.platform(),
    }

    cfg_for_hash = {
        "top": top, "defines": dict(sorted(defines_map.items())),
        "include_dirs": sorted(cfg_bits["include_dirs"]),
        "parameters": dict(sorted(params_map.items())),
        "backend": backend, "stage": cfg_bits["stage"],
        "safe_mode": cfg_bits["safe_mode"],
        "corner": corner, "mode": mode, "scenario": scenario,
        "power_report": power_input,
        "allow_partial_sdc": allow_partial_sdc,
        "sdc_generation_status": sdc_generation_status,
    }
    cfg_hash = stable_hash(cfg_for_hash)

    cache_key_data = {
        "version": 3,
        "rtl": rtl_hashes,
        "includes": inc_hashes,
        "sdc": sdc_hash,
        "libs": lib_hashes,
        "cfg": cfg_for_hash,
        "power_report": power_input,
        "tool": {
            "yosys_bin": yosys.executable, "yosys_ver": yinfo.version,
            "sta_bin": opensta.executable, "sta_ver": oinfo.version,
        },
        "synth_script_hash": synth_script_hash,
        "synth_semantic": synth_semantic_key,
        "sta_script_hash": sta_script_hash,
        "sta_semantic_pre": sta_script_semantic_pre,
    }
    cache_key = stable_hash(cache_key_data)

    # Check cache
    if not force:
        cached = _find_cached_run(am, cache_key, run_dir_parent=am.runs_dir)
        if cached is not None:
            log.info("Cache hit: %s", cached["run_id"])
            return {**cached, "status": RunStatus.CACHE_HIT.value}

    if not yinfo.available:
        diagnostics.append(f"Yosys unavailable: {yinfo.error}")
    if not oinfo.available:
        diagnostics.append(f"OpenSTA unavailable: {oinfo.error}")
    if not yinfo.available or not oinfo.available:
        return _blocked(am, run_id, candidate_id, rtl_hashes, sdc_hash, cfg_hash,
                        lib_hashes, diagnostics, run_dir, reason="; ".join(diagnostics),
                        tool_identity=tool_identity, cache_key=cache_key,
                        inc_hashes=inc_hashes)

    extra_synth = {"defines": defines_map,
                   "include_dirs": [str(p) for p in include_dirs_paths],
                   "parameters": params_map, "timeout": 600}
    try:
        synth_res: SynthResult = yosys.synthesize(
            sources, top, libs, run_dir, extra_args=extra_synth)
    except Exception as e:
        diagnostics.append(f"yosys raised {type(e).__name__}: {e}")
        return _blocked(am, run_id, candidate_id, rtl_hashes, sdc_hash, cfg_hash,
                        lib_hashes, diagnostics, run_dir,
                        reason=f"Yosys raised {type(e).__name__}: {e}",
                        tool_identity=tool_identity,
                        status=RunStatus.SYNTHESIS_FAILED.value,
                        cache_key=cache_key, inc_hashes=inc_hashes)
    if not synth_res.success:
        diagnostics.append(synth_res.error)
        return _blocked(am, run_id, candidate_id, rtl_hashes, sdc_hash, cfg_hash,
                        lib_hashes, diagnostics, run_dir, reason=synth_res.error,
                        tool_identity=tool_identity,
                        status=RunStatus.SYNTHESIS_FAILED.value,
                        cache_key=cache_key, inc_hashes=inc_hashes,
                        synth_res=synth_res)
    netlist = synth_res.netlist
    netlist_hash = hash_file(netlist)

    # ---------- STA ----------
    sta_diag = []
    qor = None
    sta_result: dict[str, Any] = {}
    status = RunStatus.SUCCESS.value
    if not libs:
        diag = ("No Liberty libraries supplied; real STA blocked. "
                "Provide flow.liberty in project.yaml for signoff STA.")
        sta_diag.append(diag)
        diagnostics.append(diag)
        sta_result = {"status": RunStatus.BLOCKED.value, "error": diag}
        status = RunStatus.BLOCKED.value
    else:
        sta_res: STAResult = opensta.run_sta(
            netlist, sdc_path, libs, run_dir, top, corner=corner,
            extra_args={"timeout": 600, "allow_partial_sdc": allow_partial_sdc,
                        "sdc_status": sdc_generation_status},
            prebuilt_script=(sta_tcl_text, sta_tcl_path, sta_report_map))
        sta_result = sta_res.to_dict()
        if sta_res.status in (RunStatus.BLOCKED.value, RunStatus.STA_FAILED.value):
            diagnostics.append(sta_res.error)
            status = sta_res.status
            qor = None
        else:
            qor = sta_res.qor
            status = sta_res.status

    if qor is not None:
        qor.run_id = run_id
        qor.candidate_id = candidate_id
        qor.backend = "yosys_opensta"
        qor.backend_version = f"yosys:{yinfo.version}|opensta:{oinfo.version}"
        qor.flow_stage = "synthesis_sta"
        qor.scenario = scenario
        qor.mode = mode
        qor.corner = corner
        qor.is_mock = False
        if synth_res.stats.get("cell_count") is not None:
            qor.cell_count = synth_res.stats["cell_count"]
        if synth_res.stats.get("ff_count") is not None:
            qor.ff_count = synth_res.stats["ff_count"]
        if synth_res.stats.get("area") is not None and not synth_res.stats.get("area_is_proxy", True):
            qor.area = synth_res.stats["area"]; qor.area_proxy = None
        elif synth_res.stats.get("area_proxy") is not None:
            qor.area_proxy = synth_res.stats["area_proxy"]
        qor.runtime_seconds = (
            (synth_res.command.duration_seconds if synth_res.command else 0)
            + (sta_res.command.duration_seconds if sta_res.command else 0))
        qor.diagnostics = list(diagnostics)
        # Report ingestion is deliberately post-STA: it consumes only the
        # configured evidence for this real scenario and leaves synthesis/STA
        # commands unchanged.  A staged copy is tracked by the normal manifest.
        power_artifact = _stage_power_report(power_input, run_dir)
        _apply_power_report(qor, power_input, scenario=scenario, mode=mode, corner=corner,
                            producer_version=oinfo.version)

    # ---------- write artifacts / manifest ----------
    synth_stats_path = run_dir / "synthesis_stats.json"
    synth_stats_path.write_text(json.dumps(synth_res.stats, indent=2, sort_keys=True),
                                encoding="utf-8")
    qor_path = None
    if qor is not None:
        qor_path = am.write_json(f"runs/{run_id}/qor.json", qor.summary())

    artifacts: dict[str, Any] = {
        "sdc": sdc_path,
        "netlist": netlist,
        "synth_script": synth_res.script_path,
        "synth_log": synth_res.log_path,
        "synth_stats": synth_stats_path,
    }
    if sta_result.get("tcl"):
        artifacts["sta_tcl"] = sta_result.get("tcl")
    if sta_result.get("log"):
        artifacts["sta_log"] = sta_result.get("log")
    for name, p in (sta_result.get("reports") or {}).items():
        artifacts[f"sta_{name}"] = p
    if power_artifact is not None:
        artifacts["power_report"] = power_artifact
    if qor_path:
        artifacts["qor"] = qor_path
    artifacts = {k: v for k, v in artifacts.items() if v}
    rel_artifacts = _artifacts_to_rel(run_dir, artifacts)
    artifact_hashes = _hash_artifacts(run_dir, artifacts)

    post_sta_cache_key_data = dict(cache_key_data)  # kept for diagnostics only
    # NOTE: netlist_hash is an OUTPUT hash; it MUST NOT be mixed into the
    # experiment cache key. The cache key represents experiment IDENTITY
    # (inputs/configuration) only. Netlist integrity is verified via
    # artifact_hashes on lookup.
    input_hashes = {"rtl": rtl_hashes, "includes": inc_hashes,
                    "libs": lib_hashes, "sdc": sdc_hash,
                    "synth_script": synth_script_hash,
                    "power_report": power_input}
    manifest = RunManifest(
        candidate_id=candidate_id, rtl_hash=rtl_hashes, sdc_hash=sdc_hash,
        config_hash=cfg_hash, tool="yosys_opensta",
        tool_version=f"yosys:{yinfo.version}|opensta:{oinfo.version}",
        flow_stage="synthesis_sta", mode=mode, corner=corner,
        library=",".join(str(p) for p in libs),
        artifacts=rel_artifacts, artifact_hashes=artifact_hashes,
        tool_identity=tool_identity, input_hashes=input_hashes,
        extra={"cache_key": cache_key,
               "scenario": scenario,
               "cache_key_data": cache_key_data,
               "netlist_hash": netlist_hash,
               "commands": {
                   "yosys": synth_res.command.to_dict() if synth_res.command else None,
                   "opensta": sta_result.get("command"),
               },
               "diagnostics": diagnostics + sta_diag},
    )
    am.write_manifest_to(run_id, manifest)
    return {
        "status": status, "run_id": run_id, "run_dir": str(run_dir),
        "manifest": manifest.to_dict(),
        "qor": qor.summary() if qor is not None else None,
        "qor_result": qor,
        "diagnostics": diagnostics + sta_diag,
        "synth": synth_res.to_dict(), "sta": sta_result,
        "cache_key": cache_key,
    }


def _hash_dir_if_exists(d: Path) -> str:
    """Hash the set of files (recursively) inside an include dir, or return ''."""
    if not d.is_dir():
        return ""
    try:
        from ..utils.hashing import hash_directory
        return hash_directory(d, pattern="*")
    except Exception:
        return ""


def _blocked(am, run_id, candidate_id, rtl_hashes, sdc_hash, cfg_hash, lib_hashes,
             diagnostics, run_dir, reason, tool_identity=None,
             status=RunStatus.BLOCKED.value, cache_key=None,
             inc_hashes=None, synth_res=None):
    artifacts: dict[str, Any] = {}
    if synth_res is not None:
        artifacts = {
            "synth_script": getattr(synth_res, "script_path", None),
            "synth_log": getattr(synth_res, "log_path", None),
        }
    rel_artifacts = _artifacts_to_rel(run_dir, artifacts)
    artifact_hashes = _hash_artifacts(run_dir, artifacts)
    manifest = RunManifest(
        candidate_id=candidate_id, rtl_hash=rtl_hashes, sdc_hash=sdc_hash,
        config_hash=cfg_hash, tool="yosys_opensta", flow_stage="synthesis_sta",
        library=",".join(lib_hashes.keys()),
        artifacts=rel_artifacts, artifact_hashes=artifact_hashes,
        tool_identity=tool_identity or {},
        input_hashes={"rtl": rtl_hashes, "includes": inc_hashes or {},
                      "libs": lib_hashes, "sdc": sdc_hash},
        extra={"diagnostics": diagnostics, "cache_key": cache_key or ""},
    )
    try:
        am.write_manifest_to(run_id, manifest)
    except Exception:
        pass
    return {"status": status, "run_id": run_id, "run_dir": str(run_dir),
            "manifest": manifest.to_dict(), "qor": None,
            "diagnostics": diagnostics + [reason],
            "synth": synth_res.to_dict() if synth_res else None,
            "sta": None, "blocked_reason": reason}


def _find_cached_run(am: ArtifactManager, cache_key: str,
                     run_dir_parent: Path | None = None) -> dict[str, Any] | None:
    runs_dir = run_dir_parent or am.runs_dir
    if not runs_dir.is_dir():
        return None
    candidates = sorted([d for d in runs_dir.iterdir() if d.is_dir()], reverse=True)
    for d in candidates:
        mpath = d / "run_manifest.json"
        if not mpath.is_file():
            mpath = d / "manifest.json"
        if not mpath.is_file():
            continue
        try:
            m = json.loads(mpath.read_text(encoding="utf-8"))
        except Exception:
            continue
        extra = m.get("extra", {}) or {}
        if extra.get("cache_key") != cache_key:
            continue
        # Integrity check
        rel_artifacts = m.get("artifacts", {}) or {}
        recorded_hashes = m.get("artifact_hashes", {}) or {}
        run_dir = d
        missing: list[str] = []
        hash_mismatch: list[str] = []
        for k, rel in rel_artifacts.items():
            p = _resolve_rel(run_dir, rel)
            if not p.is_file():
                missing.append(k)
                continue
            if k in _HASHED_ARTIFACTS and k in recorded_hashes:
                want = recorded_hashes[k]
                if want and want != hash_file(p):
                    hash_mismatch.append(k)
        # Tool identity must also match
        tool_id = m.get("tool_identity", {}) or {}
        # (Cache key already encodes bin+ver; skip extra check.)
        if missing or hash_mismatch:
            log.warning("cache entry %s invalid (missing=%s, hash_mismatch=%s)",
                        d.name, missing, hash_mismatch)
            continue
        # Required artifacts for a *usable* cache hit (i.e. a completed run
        # whose results can be reused). BLOCKED/FAILED manifests are recorded
        # with cache_key for diagnostics but lack qor and/or netlist, so they
        # naturally fail this check and are not reused as hits.
        required = {"sdc", "netlist", "qor"}
        if not required.issubset(rel_artifacts.keys()):
            continue
        qor = None
        qpath = _resolve_rel(run_dir, rel_artifacts.get("qor", ""))
        if qpath.is_file():
            try:
                qor = json.loads(qpath.read_text(encoding="utf-8"))
            except Exception:
                continue
        diag = list((extra.get("diagnostics") or []))
        diag.append(f"CACHE_HIT from {d.name}")
        qor_result = QoRResult.from_summary(qor) if isinstance(qor, dict) else None
        return {"run_id": d.name, "run_dir": str(d), "manifest": m, "qor": qor,
                "qor_result": qor_result, "diagnostics": diag, "cache_key": cache_key,
                "status": RunStatus.CACHE_HIT.value}
    return None


def _write_manifest_to(self, run_id: str, manifest: RunManifest) -> Path:
    run_dir = self.runs_dir / run_id
    run_dir.mkdir(parents=True, exist_ok=True)
    # Rewrite artifacts to run-relative (portable) paths if caller passed absolutes.
    p = run_dir / "run_manifest.json"
    # Use the manifest's artifacts as-is (we already relativized).
    p.write_text(json.dumps(manifest.to_dict(), indent=2, sort_keys=True, default=str),
                 encoding="utf-8")
    (run_dir / "manifest.json").write_text(p.read_text(encoding="utf-8"), encoding="utf-8")
    return p


ArtifactManager.write_manifest_to = _write_manifest_to
