"""
RCA Command-Line Interface (Manual §59).

Provides: rca init, analyze, infer, generate, validate, compare, coverage,
lineage, review, explain, run-sta, optimize, inspect, report, dashboard.
"""

from __future__ import annotations

import json
import re
import sys
import webbrowser
from pathlib import Path
from typing import Annotated, Any

import typer
import uvicorn
from rich.console import Console
from rich.markup import escape
from rich.panel import Panel
from rich.table import Table

from .. import __version__
from ..artifacts import ArtifactManager, RunManifest
from ..config.model import ProjectConfig, default_config, load_config, write_config
from ..constraint_model import ConstraintSet, SnapshotFormatError
from ..design_model import Design
from ..eda import (
    MockEDA,
    OpenSTABackend,
    YosysBackend,
    index_deferred_history_evidence,
    preflight_symbiyosys,
    preflight_yosys_opensta,
    run_flow,
)
from ..equivalence import compare_sdc_text
from ..exceptions import SymbiYosysFormalBackend, formal_backend_from_config
from ..explanation import (
    design_report,
    explain_constraint,
    explain_constraint_lineage,
    explain_constraint_readiness,
    explain_constraint_review,
)
from ..inference import (
    ApplicationStatus,
    ConstraintApplication,
    InferenceEngine,
    IntentDecision,
    IntentDecisionKind,
    apply_intent_decision,
)
from ..lineage import build_constraint_lineage
from ..mcmm import (
    MCMMResult,
    build_scenario_matrix,
    mock_mcmm_evaluator,
)
from ..optimizer import Optimizer
from ..parser import SlangAdapter
from ..provenance import AssumptionLedger
from ..qor.repository import QoRRepositoryError, SQLiteQoRRepository
from ..readiness import assess_constraint_readiness
from ..review import (
    ConstraintReview,
    ReviewActor,
    ReviewDecisionError,
    ReviewDecisionKind,
    ReviewPolicy,
    assess_constraint_review,
    create_constraint_review,
    decide_review,
    supersede_review,
)
from ..sdc import SDCParser, get_backend
from ..sdc_importer import SdcImporter
from ..search import KnowledgeEngine, KnowledgeError, load_knowledge_file
from ..source.manifest import resolve_include_dirs, resolve_sources
from ..timing_model import TimingGraph
from ..utils import configure_logging, get_logger
from ..utils.enums import SafeMode
from ..utils.hashing import hash_file, stable_hash
from ..validation import validate as run_validation
from ..web import create_app

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="RTL Constraint Assistant - RTL-aware timing constraint intelligence and optimization",
)
console = Console()
log = get_logger("cli")

# ``knowledge`` is an advisory-only namespace. It deliberately has no command
# that writes a project configuration or accepts a result implicitly.
knowledge_app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="Offline typed constraint-pattern lookup and reuse advice (Step 26).",
)
app.add_typer(knowledge_app, name="knowledge")


# ---- Helpers ----------------------------------------------------------------

def _load(path: str | Path) -> ProjectConfig:
    cfg = load_config(Path(path))
    return cfg


def _do_parse(cfg: ProjectConfig):
    sources = resolve_sources(cfg)
    if not sources:
        console.print("[red]No source files found.[/red]")
        raise typer.Exit(code=2)
    defines: dict[str, str] = {}
    for d in cfg.sources.defines:
        if "=" in d:
            k, v = d.split("=", 1); defines[k.strip()] = v.strip()
        else:
            defines[d.strip()] = ""
    adapter = SlangAdapter()
    params = {k: (str(v) if not isinstance(v, (int, float, bool, str)) else v)
              for k, v in cfg.parameters.items()}
    design = adapter.parse(
        files=sources,
        include_dirs=resolve_include_dirs(cfg),
        defines=defines,
        top=cfg.top_module(),
        parameters=params,
    )
    return design, adapter.diagnostics.to_list()


def _do_timing(cfg: ProjectConfig, design: Design):
    user_clocks = []
    for uc in cfg.constraints.user.clocks:
        info = {"name": uc.name, "fixed": uc.fixed}
        if uc.period:
            from ..utils.units import parse_time_string
            info["period_seconds"] = parse_time_string(uc.period)
        user_clocks.append(info)
    user_rels = []
    for r in cfg.constraints.user.relationships:
        user_rels.append(r.model_dump())
    return TimingGraph.build(design, user_clocks=user_clocks, user_relationships=user_rels)


def _do_inference(cfg: ProjectConfig, design: Design, tg: TimingGraph, ledger: AssumptionLedger):
    engine = InferenceEngine()
    cset = ConstraintSet(name=cfg.project.name)
    report = engine.run(design, tg, cfg, cset, ledger)
    return cset, report


def _load_canonical_ucm(path: str | None, cfg: ProjectConfig) -> ConstraintSet:
    """Load an optional existing canonical UCM snapshot without repairing it."""
    if path is None:
        return ConstraintSet(name=cfg.project.name)
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("canonical UCM snapshot must be a JSON object")
        return ConstraintSet.from_snapshot_dict(payload, unknown_field_policy="error")
    except (OSError, TypeError, ValueError, json.JSONDecodeError, SnapshotFormatError) as exc:
        console.print(f"[red]Cannot load canonical UCM snapshot: {exc}[/red]")
        raise typer.Exit(code=2) from exc


def _load_review_record(path: str) -> ConstraintReview:
    """Read an explicitly supplied review record without persisting anything."""
    try:
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        if not isinstance(payload, dict):
            raise TypeError("review record must be a JSON object")
        # CLI assessment JSON nests the immutable review record; accepting it
        # lets callers inspect a prior explicit decision without a database.
        if isinstance(payload.get("review"), dict):
            payload = payload["review"]
        record = ConstraintReview.from_dict(payload)
        if not record.id or not record.reviewed_snapshot.ucm_content_identity:
            raise ValueError("review record is missing its exact reviewed UCM identity")
        return record
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        console.print(f"[red]Cannot load review record: {exc}[/red]")
        raise typer.Exit(code=2) from exc


def _load_review_policy(path: str | None, allow_warnings: bool) -> ReviewPolicy:
    """Load an explicit typed policy; defaults remain visible and conservative."""
    try:
        data: Any = {}
        if path:
            data = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise TypeError("review policy must be a JSON object")
        policy = ReviewPolicy.from_dict(data)
        if allow_warnings:
            policy = ReviewPolicy(
                require_readiness=policy.require_readiness,
                require_readiness_ready=policy.require_readiness_ready,
                require_validation_evidence=policy.require_validation_evidence,
                require_coverage_complete=policy.require_coverage_complete,
                require_all_active_scenarios=policy.require_all_active_scenarios,
                allow_approval_with_warnings=True,
                require_formal_for_constraint_types=policy.require_formal_for_constraint_types,
                allowed_unresolved_evidence_categories=policy.allowed_unresolved_evidence_categories,
            )
        return policy
    except (OSError, TypeError, ValueError, json.JSONDecodeError) as exc:
        console.print(f"[red]Cannot load review policy: {exc}[/red]")
        raise typer.Exit(code=2) from exc


def _write_canonical_ucm(cset: ConstraintSet, output: str | None, cfg: ProjectConfig) -> Path:
    """Persist an explicitly applied UCM using its existing canonical format."""
    if output is not None:
        path = Path(output)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(cset.to_canonical_json(), encoding="utf-8")
        return path
    return _am(cfg).write_json_atomic("applied_constraint_model.json", cset.to_snapshot_dict())


def _am(cfg: ProjectConfig) -> ArtifactManager:
    out = Path(cfg.flow.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    return ArtifactManager(out)


def _formal_backend(cfg: ProjectConfig):
    """Construct the opt-in formal backend from config without changing UCM state."""
    return formal_backend_from_config(cfg.formal)


# ---- MCMM helpers (Step 12 §13, §14) --------------------------------

def _mcmm_matrix(cfg: ProjectConfig, cset: ConstraintSet):
    """Build the active scenario matrix from config + UCM (MCMM-aware)."""
    return build_scenario_matrix(cfg, cset)


def _print_scenario_matrix(console, matrix) -> None:
    from rich.table import Table
    s = matrix.summary()
    t = Table(title="Active scenario matrix (MCMM)")
    t.add_column("ID"); t.add_column("Mode"); t.add_column("Corner")
    t.add_column("Libraries"); t.add_column("Parasitics")
    for sc in s.get("active_scenarios", []):
        libs = ", ".join(sc.get("libraries", [])) or "-"
        t.add_row(sc["id"], sc["mode"], sc["corner"], libs,
                  sc.get("parasitics") or "-")
    console.print(t)
    console.print(f"[dim]MCMM {'enabled' if s.get('enabled') else 'disabled'} "
                  f"| {s.get('scenario_count')} active "
                  f"| single-scenario={s.get('single_scenario')}[/]")


def _maybe_print_matrix(cfg: ProjectConfig, cset: ConstraintSet, console) -> bool:
    """Print the active scenario matrix when MCMM is enabled; return enabled."""
    matrix = _mcmm_matrix(cfg, cset)
    if matrix.is_enabled and matrix.scenario_count > 1:
        _print_scenario_matrix(console, matrix)
        return True
    return False


def _mcmm_per_scenario_sdc(cset: ConstraintSet, backend, design_name: str,
                           matrix, scenario_id: str) -> str:
    """Render the SDC restricted to a single MCMM scenario."""
    from ..utils.enums import SafeMode
    res = backend.generate(cset, design_name=design_name,
                           mode=SafeMode.BALANCED, with_provenance=True,
                           scenario=scenario_id)
    return res.text


def _parallel_task_flow_id(work_dir: Path, candidate_id: str, scenario_id: str) -> str | None:
    """Return a task-specific physical run ID only for an optimizer worker.

    ``Optimizer`` preassigns task directory names containing a per-invocation
    token, ordinal, and candidate ID before submission. The scenario component
    completes the physical-flow locator while leaving the existing input-only
    cache key untouched. Baseline/direct serial callbacks do not use this path.
    """
    task_component = Path(work_dir).name
    if not task_component.startswith("task_"):
        return None
    scenario_component = re.sub(r"[^A-Za-z0-9_.-]+", "-", scenario_id).strip(".-") or "scenario"
    candidate_component = re.sub(r"[^A-Za-z0-9_.-]+", "-", candidate_id).strip(".-") or "candidate"
    # The candidate component is intentionally repeated as an independently
    # visible guard even though it is already embedded in the task component.
    return f"run_{task_component}_{candidate_component}_{scenario_component}"


def _persist_optimizer_execution_artifacts(am: ArtifactManager, cfg: ProjectConfig,
                                           result, *, candidates_path: Path,
                                           pareto_path: Path, final_sdc_path: Path | None) -> dict[str, str]:
    """Write authoritative optimizer observability artifacts before SQLite advice.

    The ledger is a deterministic projection of the existing optimizer result,
    not an alternate cache, QoR model, or database record. A small RunManifest
    binds it to the established root optimizer artifacts without entering the
    physical-flow cache namespace under ``runs/``.
    """
    if result.execution_ledger is None:
        raise RuntimeError("optimizer returned without an execution ledger")
    ledger_path = am.write_json_atomic(
        result.execution_ledger.artifact_path,
        result.execution_ledger.to_dict(),
    )
    state_path = am.write_json_atomic("optimizer_state.json", result.to_dict())
    artifacts: dict[str, Path] = {
        "optimizer_state": state_path,
        "execution_ledger": ledger_path,
        "candidates": candidates_path,
        "pareto_frontier": pareto_path,
    }
    if final_sdc_path is not None and final_sdc_path.is_file():
        artifacts["final_sdc"] = final_sdc_path
    relative_artifacts = {name: path.name for name, path in artifacts.items() if path.is_file()}
    artifact_hashes = {name: hash_file(path) for name, path in artifacts.items() if path.is_file()}
    baseline = result.baseline
    baseline_hash = ""
    if baseline is not None:
        baseline_hash = baseline.constraint_model_hash
        if not baseline_hash and baseline.constraint_set is not None:
            from ..constraint_model import stable_hash_cset
            baseline_hash = stable_hash_cset(baseline.constraint_set)
    manifest = RunManifest(
        candidate_id=result.final.id if result.final else "",
        config_hash=stable_hash(cfg.model_dump(mode="json")),
        tool="rca_optimizer",
        tool_version=__version__,
        flow_stage="optimization",
        artifacts=relative_artifacts,
        artifact_hashes=artifact_hashes,
        input_hashes={"baseline_constraint_set": baseline_hash},
        extra={
            "kind": "optimizer_execution",
            "invocation_id": result.execution_ledger.invocation_id,
            "execution_stop_reason": result.execution_stop_reason.value
            if result.execution_stop_reason else None,
            "legacy_stop_reason": result.stop_reason.value if result.stop_reason else None,
            "workers": result.execution_ledger.workers,
            "ledger_schema_version": result.execution_ledger.schema_version,
        },
    )
    manifest_path = am.write_json_atomic("optimizer_execution_manifest.json", manifest.to_dict())
    return {
        "ledger": str(ledger_path),
        "state": str(state_path),
        "manifest": str(manifest_path),
    }


def _read_optimizer_execution_ledger(output_dir: str | Path) -> dict[str, Any] | None:
    """Read the authoritative execution ledger without consulting SQLite."""
    path = Path(output_dir) / "optimizer_execution_ledger.json"
    if not path.is_file():
        return None
    try:
        loaded = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise ValueError(f"Cannot read optimizer execution ledger at {path}: {exc}") from exc
    if not isinstance(loaded, dict):
        raise TypeError(f"Optimizer execution ledger at {path} is not a JSON object.")
    # Keep the persisted path deterministic and relative while exposing the
    # resolved inspection location for this CLI invocation.
    loaded["artifact_path"] = str(path)
    return loaded


def _print_optimizer_execution_ledger(data: dict[str, Any]) -> None:
    """Render a concise execution ledger summary for the existing history CLI."""
    counts = data.get("counts") or {}
    console.print(Panel("[cyan]Optimizer execution ledger[/cyan] — artifacts remain authoritative"))
    console.print(f"  Invocation: {data.get('invocation_id', '-')}")
    console.print(f"  Workers: {data.get('workers', '-')}  Stop: {data.get('stop_reason', '-')}")
    console.print(f"  Ledger: {data.get('artifact_path', '-')}")
    console.print(
        "  Planned: {planned}  Admitted: {admitted}  Submitted: {submitted}  "
        "Completed: {completed}  Failed: {failed}  Skipped: {skipped}  "
        "Cancelled: {cancelled}  Blocked: {blocked}".format(
            planned=counts.get("planned_total", 0),
            admitted=counts.get("admitted_total", 0),
            submitted=counts.get("submitted_total", 0),
            completed=counts.get("completed", 0),
            failed=counts.get("failed", 0),
            skipped=counts.get("skipped", 0),
            cancelled=counts.get("cancelled", 0),
            blocked=counts.get("blocked", 0),
        )
    )
    console.print(
        f"  EDA runs: {data.get('total_eda_runs', 0)}  "
        f"Final: {data.get('final_candidate_id', '-') or '-'}  "
        f"Pareto: {', '.join(data.get('pareto_candidate_ids') or []) or '-'}"
    )


def _print_power_summary(console, q: dict, *, indent: str = "  ") -> None:
    """Render report-derived power without implying a live/silicon measurement."""
    status = q.get("power_status", "UNAVAILABLE")
    total = q.get("power_total", q.get("power"))
    if status == "AVAILABLE" and total is not None:
        console.print(f"{indent}Tool-reported power: {total:.6g} W")
        dynamic = q.get("power_dynamic")
        leakage = q.get("power_leakage")
        if dynamic is not None or leakage is not None:
            pieces = []
            if dynamic is not None:
                pieces.append(f"dynamic={dynamic:.6g} W")
            if leakage is not None:
                pieces.append(f"leakage={leakage:.6g} W")
            console.print(f"{indent}  " + "  ".join(pieces))
    else:
        # Canonical QoR remains UNAVAILABLE for every unusable report. Retain
        # the parser-bound detail so users can distinguish absent evidence from
        # unknown, malformed, invalid, or unsupported configured evidence.
        provenance = q.get("power_provenance") or {}
        parse_status = provenance.get("parsing_status")
        suffix = (f" (report parser: {parse_status})"
                  if parse_status and parse_status != status else "")
        console.print(f"{indent}Power: {status}{suffix}")
    provenance = q.get("power_provenance") or {}
    if provenance:
        report_path = provenance.get("report_path") or "-"
        digest = provenance.get("sha256") or "-"
        fmt = provenance.get("format") or "-"
        console.print(f"{indent}Power provenance: format={fmt} path={report_path} sha256={digest}")


def _latest_qor_summary(cfg: ProjectConfig) -> dict | None:
    """Read the most recently modified completed QoR artifact, if any.

    ``rca report`` remains a reporting command: it never runs a tool or parses
    a new report on its own.  It only presents report-derived QoR already
    recorded by ``run-sta``/the flow.
    """
    runs_dir = Path(cfg.flow.output_dir) / "runs"
    if not runs_dir.is_dir():
        return None
    paths = sorted(runs_dir.glob("*/qor.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    for path in paths:
        try:
            candidate = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(candidate, dict):
                return candidate
        except (OSError, json.JSONDecodeError):
            continue
    return None


def _print_mcmm_result(console, m: MCMMResult, matrix) -> None:
    """Print an MCMMResult with full per-scenario auditability (Step 12 §14)."""
    from rich.table import Table
    _print_scenario_matrix(console, matrix)
    console.print(Panel(f"[cyan]MCMM evaluation[/cyan] — candidate {m.candidate_id}"))
    status_color = {"feasible": "green", "infeasible": "yellow",
                    "blocked": "red", "invalid": "red"}.get(
        m.global_status, "white")
    console.print(f"  Global status: [{status_color}]{m.global_status}[/]")
    console.print(f"  Limiting scenarios: {', '.join(m.limiting_scenarios) or '-'}")
    console.print(f"  EDA runs: {m.eda_runs}  Cache hits: {m.cache_hits}  "
                  f"Cache misses: {m.cache_misses}")
    t = Table(title="Per-scenario QoR")
    for col in ("Scenario", "Mode", "Corner", "Status", "Setup WNS (ns)",
                "Hold WNS (ns)", "Margin util", "Cache", "Run id"):
        t.add_column(col)
    for sid in m.active_scenario_ids:
        sq = m.scenario_results.get(sid)
        if sq is None:
            continue
        s_wns = f"{sq.qor.setup_wns*1e9:.3f}" if sq.qor and sq.qor.setup_wns is not None else "-"
        h_wns = f"{sq.qor.hold_wns*1e9:.3f}" if sq.qor and sq.qor.hold_wns is not None else "-"
        util = f"{sq.margin_utilization:.2f}" if sq.margin_utilization is not None else "-"
        t.add_row(sid, sq.mode, sq.corner, sq.status, s_wns, h_wns, util,
                  sq.cache_status, sq.run_id or "-")
    console.print(t)
    for name, agg in m.objectives.items():
        limiting = ", ".join(agg.limiting) or "-"
        val = f"{agg.value:.6g}" if agg.value is not None else "UNKNOWN"
        console.print(f"  Objective {name}: {val}  "
                      f"(unknown={agg.unknown}, limiting={limiting})")
    if m.provenance:
        console.print(f"  Provenance: {json.dumps(m.provenance, default=str)[:200]}")
    if m.diagnostics:
        console.print("  Diagnostics:")
        for d in m.diagnostics:
            console.print(f"    - {d}")


def _record_standalone_mcmm_history(cfg: ProjectConfig, cset: ConstraintSet,
                                    mcmm_result: MCMMResult, candidate_id: str = "baseline") -> tuple[str | None, str | None]:
    """Index a completed ``run-sta`` MCMM aggregate without changing its artifacts.

    The repository needs a session-scoped candidate key even for a baseline
    MCMM command.  This creates a one-candidate historical session after the
    established MCMM artifact is written; it does not run an optimizer or
    modify the MCMM/flow/cache models.
    """
    try:
        from ..optimizer import Candidate, OptimizationResult

        candidate = Candidate(id=candidate_id, constraint_set=cset, mcmm=mcmm_result)
        candidate.hard_feasible = bool(mcmm_result.feasible)
        candidate.blocked = bool(mcmm_result.blocked)
        candidate.global_status = mcmm_result.global_status
        candidate.infeasible_reason = mcmm_result.global_reason
        candidate.cache_key = mcmm_result.cache_key
        candidate.cache_status = mcmm_result.cache_status
        candidate.run_id = ";".join(mcmm_result.run_ids)
        candidate.margin_headroom_ns = mcmm_result.margin_headroom_ns
        candidate.margin_utilization = mcmm_result.margin_utilization
        session = OptimizationResult(baseline=candidate, final=candidate, all_candidates=[candidate])
        repo = SQLiteQoRRepository.for_output_dir(cfg.flow.output_dir)
        session_id = repo.record_optimizer_session(
            session, project_name=cfg.project.name, output_dir=cfg.flow.output_dir,
        )
        return session_id, None
    except Exception as exc:  # noqa: BLE001 - boundary captures are intentional.
        return None, f"QOR_DATABASE_PERSISTENCE_WARNING: {type(exc).__name__}: {exc}"


# ---- Commands ---------------------------------------------------------------

@app.command()
def init(path: str = typer.Argument(".", help="Project directory"),
         name: str = typer.Option("new_project", help="Project name"),
         top: str | None = typer.Option(None, help="Top module name")):
    """Initialize a new RCA project in the given directory."""
    root = Path(path).resolve()
    root.mkdir(parents=True, exist_ok=True)
    cfg_path = root / "rca.project.yaml"
    if cfg_path.is_file():
        console.print(f"[yellow]{cfg_path} already exists; not overwriting.[/yellow]")
        raise typer.Exit(code=1)
    cfg = default_config(name=name, top=top)
    write_config(cfg, cfg_path)
    # Create a basic RTL example
    rtl_dir = root / "rtl"; rtl_dir.mkdir(exist_ok=True)
    example = rtl_dir / f"{top or 'top'}.sv"
    if not example.exists():
        example.write_text(_DEFAULT_RTL.format(top=top or "top"), encoding="utf-8")
    console.print(f"[green]Initialized RCA project at {root}[/green]")
    console.print(f"  Config:  {cfg_path}")
    console.print(f"  RTL:     {example}")
    console.print(f"  Next:    rca analyze {cfg_path}")


@app.command()
def analyze(config: str = typer.Argument(..., help="Path to project YAML"),
            json_out: bool = typer.Option(False, "--json", help="Output JSON only")):
    """Parse & elaborate RTL; report structural findings and missing info."""
    configure_logging(level="WARNING" if json_out else "INFO")
    cfg = _load(config)
    design, diag = _do_parse(cfg)
    tg = _do_timing(cfg, design)
    missing = tg.missing_information()
    am = _am(cfg)
    am.write_json("design_model.json", design.snapshot())
    am.write_json("timing_graph.json", tg.summary())
    summary = design.summary()
    if json_out:
        out = {"design": summary, "timing": tg.summary(),
               "diagnostics": diag, "missing_information": missing}
        sys.stdout.write(json.dumps(out, indent=2, default=str))
        return
    console.print(Panel(f"[bold cyan]RCA Analysis[/bold cyan] — {cfg.project.name}"))
    t = Table(title="Design"); t.add_column("Metric"); t.add_column("Value")
    for k, v in summary.items():
        if isinstance(v, list):
            v = ", ".join(map(str, v[:20])) or "-"
        t.add_row(k, str(v))
    console.print(t)
    if missing:
        mt = Table(title="Missing information (required to generate complete SDC)")
        mt.add_column("Severity"); mt.add_column("Category"); mt.add_column("Message")
        for m in missing:
            mt.add_row(m.get("severity", "?"), m.get("category", "?"), m.get("message", ""))
        console.print(mt)
    _maybe_print_matrix(cfg, ConstraintSet(name=cfg.project.name), console)
    console.print(f"\n[dim]Artifacts written to {am.output_dir}/[/dim]")


@app.command()
def infer(config: str = typer.Argument(..., help="Path to project YAML"),
          ucm: str | None = typer.Option(None, "--ucm", help="Existing canonical UCM JSON snapshot"),
          json_out: bool = typer.Option(False, "--json", help="Output deterministic advisory JSON only")):
    """Report non-mutating, evidence-backed constraint candidates.

    Unlike legacy materialization used by generate/validate compatibility
    paths, this command never adds an inferred candidate to a UCM. A caller
    must explicitly use the candidate API to accept a valid proposal.
    """
    configure_logging(level="WARNING" if json_out else "INFO")
    cfg = _load(config)
    design, _ = _do_parse(cfg)
    tg = _do_timing(cfg, design)
    baseline = _load_canonical_ucm(ucm, cfg)
    report = InferenceEngine().infer_candidates(
        design, tg, cfg, baseline, AssumptionLedger(), knowledge=KnowledgeEngine(),
    )
    if json_out:
        sys.stdout.write(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
        return

    console.print(Panel(f"[cyan]Inference report (advisory; UCM unchanged)[/cyan] — {cfg.project.name}"))
    facts = report.structural_facts
    if facts:
        ft = Table(title=f"Structural facts ({len(facts)})")
        ft.add_column("Category"); ft.add_column("Object"); ft.add_column("Observation")
        for fact in facts:
            ft.add_row(fact["category"], fact["object"], fact["statement"])
        console.print(ft)
    candidates = report.candidates
    ct = Table(title=f"Advisory candidates ({len(candidates)}; not applied)")
    for col in ("ID", "Kind", "Status", "Decision", "Objects", "Why"):
        ct.add_column(col)
    for candidate in candidates:
        ct.add_row(candidate.id, candidate.kind, candidate.status.value, candidate.decision.value,
                   ", ".join(candidate.source_objects[:3]) or "-", candidate.rationale)
    console.print(ct)

    required = report.required_information()
    if required:
        mt = Table(title="Missing information / confirmation required", show_lines=False)
        mt.add_column("ID"); mt.add_column("Category"); mt.add_column("Object")
        mt.add_column("Message"); mt.add_column("Blocking")
        for i, missing in enumerate(required, 1):
            mt.add_row(missing.get("id", f"REQ-{i:03d}"), missing.get("category", ""),
                       missing.get("object", ""), missing.get("message", ""),
                       "YES" if missing.get("blocking") else "no")
        console.print(mt)
    rejected = [candidate for candidate in candidates
                if candidate.status.value in {"AMBIGUOUS", "UNSUPPORTED", "CONFLICTING", "REJECTED"}]
    if rejected:
        console.print(f"\n[yellow]Ambiguous, unsupported, or rejected advisory items: {len(rejected)}[/yellow]")
    if report.warnings:
        console.print(f"\n[yellow]Warnings: {len(report.warnings)}[/yellow]")
        for warning in report.warnings[:20]:
            console.print(f"  - {warning.get('message', warning)}")
    if report.conflicts:
        console.print(f"\n[magenta]Conflicts retained (nothing overwritten): {len(report.conflicts)}[/magenta]")
        for conflict in report.conflicts[:10]:
            console.print(f"  - {conflict.get('message', conflict)}")
    _maybe_print_matrix(cfg, baseline, console)


@app.command()
def apply(
    config: str = typer.Argument(..., help="Path to project YAML"),
    candidate: str = typer.Option(..., "--candidate", help="Reviewed advisory candidate ID to resolve"),
    decision: str = typer.Option(..., "--decision", help="Explicit ACCEPT|REJECT|DEFER|CONFIRM|ALREADY_SATISFIED"),
    ucm: str | None = typer.Option(None, "--ucm", help="Existing canonical UCM JSON snapshot"),
    scenario_ids: Annotated[list[str] | None, typer.Option("--scenario", help="Explicit candidate scenario scope (repeatable)")] = None,
    output: str | None = typer.Option(None, "--output", help="Canonical UCM snapshot written only after application"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Validate isolated application without mutating or writing UCM"),
    json_out: bool = typer.Option(False, "--json", help="Output deterministic application receipt JSON only"),
):
    """Explicitly resolve one advisory candidate into canonical UCM intent.

    The command recomputes the reviewed candidate against the supplied
    canonical UCM snapshot, requires an explicit decision, and writes a
    canonical UCM snapshot only after the existing validation pipeline passes.
    It never applies all candidates or emits SDC.
    """
    configure_logging(level="WARNING" if json_out else "INFO")
    cfg = _load(config)
    design, _ = _do_parse(cfg)
    tg = _do_timing(cfg, design)
    cset = _load_canonical_ucm(ucm, cfg)
    report = InferenceEngine().infer_candidates(
        design, tg, cfg, cset, AssumptionLedger(), knowledge=KnowledgeEngine(),
    )
    selected = next((item for item in report.candidates if item.id == candidate), None)
    if selected is None:
        payload = {
            "candidate_id": candidate,
            "status": "INVALID",
            "ucm_mutated": False,
            "blocking_reasons": ["Candidate ID was not found in fresh deterministic inference output."],
        }
        if json_out:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            console.print("[red]Candidate ID was not found in fresh deterministic inference output.[/red]")
        raise typer.Exit(code=2)
    try:
        decision_kind = IntentDecisionKind(decision.strip().upper())
    except ValueError:
        payload = {
            "candidate_id": candidate,
            "status": "INVALID",
            "ucm_mutated": False,
            "blocking_reasons": [f"Unsupported explicit decision: {decision!r}"],
        }
        if json_out:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            console.print(f"[red]Unsupported explicit decision: {decision!r}[/red]")
        raise typer.Exit(code=2)
    application = ConstraintApplication(
        candidate=selected,
        decision=IntentDecision(candidate_id=candidate, kind=decision_kind),
        scenario_ids=tuple(scenario_ids or ()),
        dry_run=dry_run,
    )
    outcome = apply_intent_decision(application, cset, design, tg, cfg)
    output_path: Path | None = None
    if outcome.ucm_mutated:
        output_path = _write_canonical_ucm(cset, output, cfg)
    payload = outcome.to_dict()
    if output_path is not None:
        payload["canonical_ucm_output"] = str(output_path)
    if json_out:
        typer.echo(json.dumps(payload, indent=2, sort_keys=True, default=str))
        return
    color = {
        ApplicationStatus.APPLIED: "green",
        ApplicationStatus.ALREADY_PRESENT: "green",
        ApplicationStatus.DEFERRED: "yellow",
        ApplicationStatus.REJECTED: "yellow",
    }.get(outcome.status, "red")
    console.print(Panel(
        f"[bold]{outcome.status.value}[/bold] — candidate {outcome.candidate_id}\n"
        f"UCM mutated: {outcome.ucm_mutated}",
        title="Controlled constraint application", border_style=color,
    ))
    if outcome.applied_constraint_ids:
        console.print("Applied UCM constraints: " + ", ".join(outcome.applied_constraint_ids))
    if outcome.already_present_ids:
        console.print("Already present UCM constraints: " + ", ".join(outcome.already_present_ids))
    for reason in outcome.blocking_reasons:
        console.print(f"[yellow]Blocked: {reason}[/yellow]")
    for warning in outcome.warnings:
        console.print(f"[dim]{warning}[/dim]")
    if output_path is not None:
        console.print(f"[green]Canonical UCM snapshot written to {output_path}[/green]")


@app.command()
def generate(config: str = typer.Argument(..., help="Path to project YAML"),
             backend: str = typer.Option("generic", help="SDC backend: generic|opensta|synopsys|cadence"),
             output: str | None = typer.Option(None, help="Output SDC path (default: output/design.sdc)"),
             safe_mode: str = typer.Option("balanced", help="strict|balanced|aggressive"),
             provenance_comments: bool = typer.Option(True, "--provenance/--no-provenance"),
             scenario: str | None = typer.Option(None, "--scenario",
                                                 help="MCMM scenario id; restrict SDC to one scenario")):
    """Generate SDC from inferred + user-specified constraints (Step 6)."""
    configure_logging(level="INFO")
    cfg = _load(config)
    design, _ = _do_parse(cfg)
    tg = _do_timing(cfg, design)
    ledger = AssumptionLedger()
    cset, _inf_report = _do_inference(cfg, design, tg, ledger)
    sdc_backend = get_backend(backend)
    try:
        mode = SafeMode(safe_mode)
    except ValueError:
        mode = SafeMode.BALANCED
    result = sdc_backend.generate(cset, design_name=cfg.project.name, mode=mode,
                                   with_provenance=provenance_comments,
                                   scenario=scenario)
    am = _am(cfg)
    suffix = f".{scenario}" if scenario else ""
    out_path = Path(output) if output else am.path(f"design{'.' + backend if backend else ''}{suffix}.sdc")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.text, encoding="utf-8")
    am.write_json("constraint_model.json", cset.snapshot())
    am.write_json("assumptions.json", ledger.to_list())
    if scenario:
        _maybe_print_matrix(cfg, cset, console)
        console.print(f"[dim]Scenario-specific SDC for {scenario} "
                      f"(emitted {len(result.emitted_constraint_ids)}/"
                      f"{len(cset)} constraints).[/]")

    # Step 6 §29 summary block
    console.print("SDC GENERATION")
    console.print("--------------")
    console.print(f"Backend:    {result.backend}")
    console.print(f"Safe mode:  {result.safe_mode}")
    console.print(f"Design:     {cfg.project.name}")
    console.print(f"Constraints: {len(cset)}")
    console.print(f"Emitted:    {len(result.emitted_constraint_ids)}")
    console.print(f"Blocked:    {len(result.skipped_constraint_ids)}")
    status_str = result.status if isinstance(result.status, str) else result.status.value
    status_color = {"COMPLETE": "green", "PARTIAL": "yellow",
                    "BLOCKED": "red", "ERROR": "red"}.get(status_str, "white")
    console.print(f"Status:     [{status_color}]{status_str}[/]")

    # List blocked constraints with reasons
    errors_by_id: dict[str, list[str]] = {}
    for d in result.diagnostics:
        if d.severity in ("ERROR", "FATAL"):
            errors_by_id.setdefault(d.constraint_id or "", []).append(d.message)
    if result.skipped_constraint_ids:
        console.print("\n[yellow]Blocked constraints:[/yellow]")
        for cid in result.skipped_constraint_ids:
            reasons = "; ".join(errors_by_id.get(cid, ["unspecified"]))
            console.print(f"  - {cid}: {reasons}")

    color = "green" if status_str == "COMPLETE" else "yellow"
    console.print(Panel(f"[{color}]Generated {out_path}[/{color}]"))
    # Exit non-zero on BLOCKED/ERROR so CI scripts don't mistake it for success.
    if status_str in ("BLOCKED", "ERROR"):
        raise typer.Exit(code=2)


@app.command()
def lineage(
    config: str = typer.Argument(..., help="Path to project YAML"),
    ucm: str | None = typer.Option(None, "--ucm", help="Current canonical UCM JSON snapshot"),
    before: str | None = typer.Option(None, "--before", help="Explicit canonical UCM snapshot A"),
    after: str | None = typer.Option(None, "--after", help="Explicit canonical UCM snapshot B"),
    json_out: bool = typer.Option(False, "--json", help="Output deterministic lineage JSON only"),
):
    """Trace canonical constraint lineage and optionally diff two UCM snapshots.

    This command is report-only. It loads explicitly supplied canonical UCM
    snapshot(s), produces advisory context only in memory, and never applies a
    candidate, writes UCM/SDC/artifacts/history/SQLite, or executes EDA/proofs.
    Choose exactly one current ``--ucm`` report or a complete ``--before`` /
    ``--after`` semantic-comparison pair.
    """
    configure_logging(level="WARNING" if json_out else "INFO")
    current_mode = ucm is not None
    comparison_mode = before is not None or after is not None
    if current_mode == comparison_mode or (comparison_mode and (before is None or after is None)):
        message = "Provide exactly one --ucm, or provide both --before and --after canonical UCM snapshots."
        if json_out:
            typer.echo(json.dumps({"status": "INVALID", "message": message}, indent=2, sort_keys=True))
        else:
            console.print(f"[red]{message}[/red]")
        raise typer.Exit(code=2)
    cfg = _load(config)
    design, _ = _do_parse(cfg)
    timing_graph = _do_timing(cfg, design)

    def conservative_validation(cset: ConstraintSet):
        matrix = _mcmm_matrix(cfg, cset)
        active = set(matrix.active_ids) if matrix.is_enabled else None
        return run_validation(design, timing_graph, cset, backend="generic", active_scenarios=active)

    if current_mode:
        cset = _load_canonical_ucm(ucm, cfg)
        advisory = InferenceEngine().infer_candidates(
            design, timing_graph, cfg, cset, AssumptionLedger(), knowledge=KnowledgeEngine(),
        )
        validation = conservative_validation(cset)
        readiness_report = assess_constraint_readiness(
            cfg, cset, design, timing_graph, validation=validation, inference_report=advisory,
        )
        report = build_constraint_lineage(
            cset, config=cfg, design=design, timing_graph=timing_graph,
            inference_report=advisory, validation=validation, readiness=readiness_report,
        )
    else:
        before_ucm = _load_canonical_ucm(before, cfg)
        after_ucm = _load_canonical_ucm(after, cfg)
        before_validation = conservative_validation(before_ucm)
        after_validation = conservative_validation(after_ucm)
        before_readiness = assess_constraint_readiness(
            cfg, before_ucm, design, timing_graph, validation=before_validation,
        )
        after_readiness = assess_constraint_readiness(
            cfg, after_ucm, design, timing_graph, validation=after_validation,
        )
        report = build_constraint_lineage(
            after_ucm, config=cfg, design=design, timing_graph=timing_graph,
            validation=after_validation, readiness=after_readiness, before=before_ucm,
            before_readiness=before_readiness,
        )
    if json_out:
        typer.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
        return
    console.print(Panel(
        "[cyan]Read-only traceability projection; canonical UCM unchanged[/cyan]",
        title="Constraint lineage & audit trail",
        border_style="cyan",
    ))
    console.print(explain_constraint_lineage(report))


@app.command()
def review(
    config: str = typer.Argument(..., help="Path to project YAML"),
    ucm: str = typer.Option(..., "--ucm", help="Canonical UCM JSON snapshot to review"),
    decision: str | None = typer.Option(None, "--decision", help="Explicit APPROVE|APPROVE_WITH_WARNINGS|REJECT|DEFER|REVOKE"),
    reviewer: str | None = typer.Option(None, "--reviewer", help="Explicit reviewer identity; omitted is UNSPECIFIED"),
    reviewer_role: str | None = typer.Option(None, "--reviewer-role", help="Optional explicit reviewer role"),
    comment: str = typer.Option("", "--comment", help="Explicit reviewer rationale/comment"),
    scenario_ids: Annotated[list[str] | None, typer.Option(
        "--scenario", help="Selected MCMM scenario review scope (repeatable)",
    )] = None,
    all_active_scenarios: bool = typer.Option(False, "--all-active-scenarios",
                                               help="Explicitly review all active MCMM scenarios"),
    policy: str | None = typer.Option(None, "--policy", help="Explicit ReviewPolicy JSON file"),
    allow_warnings: bool = typer.Option(False, "--allow-warnings",
                                        help="Explicitly set policy.allow_approval_with_warnings"),
    prior_review: str | None = typer.Option(None, "--review", help="Prior review JSON or review-assessment JSON"),
    supersede: str | None = typer.Option(None, "--supersede", help="Prior review JSON to supersede with a new review"),
    json_out: bool = typer.Option(False, "--json", help="Output deterministic review JSON only"),
):
    """Assess or explicitly decide review of one canonical UCM snapshot.

    Review is governance only: it never modifies UCM, accepts a candidate,
    generates SDC, writes artifacts/history/SQLite, or executes EDA/formal.
    A decision is optional; without it the result always remains an assessment
    and is never an implicit approval. Supplied/created RCA review approval is
    distinct from external EDA signoff.
    """
    configure_logging(level="WARNING" if json_out else "INFO")
    if prior_review and supersede:
        message = "Use at most one of --review and --supersede."
        if json_out:
            typer.echo(json.dumps({"status": "INVALID", "message": message}, indent=2, sort_keys=True))
        else:
            console.print(f"[red]{message}[/red]")
        raise typer.Exit(code=2)
    cfg = _load(config)
    cset = _load_canonical_ucm(ucm, cfg)
    review_policy = _load_review_policy(policy, allow_warnings)
    # These are existing, in-memory evidence projections only. No candidate is
    # materialized or applied, and generic validation never invokes a formal or
    # EDA backend. Step-29 and Step-30 remain their own authorities.
    design, _ = _do_parse(cfg)
    timing_graph = _do_timing(cfg, design)
    matrix = _mcmm_matrix(cfg, cset)
    validation = run_validation(
        design, timing_graph, cset, backend="generic",
        active_scenarios=set(matrix.active_ids) if matrix.is_enabled else None,
    )
    readiness_report = assess_constraint_readiness(
        cfg, cset, design, timing_graph, validation=validation,
        scenario_ids=tuple(scenario_ids or ()) if scenario_ids else (),
    )
    lineage_report = build_constraint_lineage(
        cset, config=cfg, design=design, timing_graph=timing_graph,
        validation=validation, readiness=readiness_report,
    )
    if prior_review:
        record = _load_review_record(prior_review)
        if record.policy != review_policy:
            message = "--review must be assessed under its recorded policy; omit --policy/--allow-warnings changes."
            if json_out:
                typer.echo(json.dumps({"status": "INVALID", "message": message}, indent=2, sort_keys=True))
            else:
                console.print(f"[red]{message}[/red]")
            raise typer.Exit(code=2)
    elif supersede:
        record = supersede_review(
            _load_review_record(supersede), cset, policy=review_policy,
            scenario_ids=tuple(scenario_ids or ()), all_active_scenarios=all_active_scenarios,
            config=cfg, design=design, timing_graph=timing_graph, lineage=lineage_report,
            readiness=readiness_report, validation=validation,
        )
    else:
        record = create_constraint_review(
            cset, policy=review_policy, scenario_ids=tuple(scenario_ids or ()),
            all_active_scenarios=all_active_scenarios, config=cfg, design=design,
            timing_graph=timing_graph, lineage=lineage_report, readiness=readiness_report,
            validation=validation,
        )
    try:
        parsed_decision = ReviewDecisionKind(decision.strip().upper()) if decision else None
    except ValueError:
        message = f"Unsupported explicit review decision: {decision!r}"
        if json_out:
            typer.echo(json.dumps({"status": "INVALID", "message": message}, indent=2, sort_keys=True))
        else:
            console.print(f"[red]{message}[/red]")
        raise typer.Exit(code=2) from None
    actor = ReviewActor.from_value(reviewer, role=reviewer_role)
    try:
        if parsed_decision is not None:
            record = decide_review(
                record, cset, parsed_decision, actor=actor, comment=comment,
                config=cfg, design=design, timing_graph=timing_graph, lineage=lineage_report,
                readiness=readiness_report, validation=validation,
            )
        assessment = assess_constraint_review(
            cset, review=record, config=cfg, design=design, timing_graph=timing_graph,
            lineage=lineage_report, readiness=readiness_report, validation=validation,
        )
    except ReviewDecisionError as exc:
        payload = {"status": "INVALID", "message": str(exc), "ucm_mutated": False}
        if json_out:
            typer.echo(json.dumps(payload, indent=2, sort_keys=True))
        else:
            console.print(f"[red]{exc}[/red]")
        raise typer.Exit(code=2) from exc
    if json_out:
        typer.echo(json.dumps(assessment.to_dict(), indent=2, sort_keys=True, default=str))
        return
    color = {
        "APPROVED": "green", "APPROVED_WITH_WARNINGS": "yellow", "NEEDS_REVIEW": "cyan",
        "REJECTED": "red", "DEFERRED": "yellow", "STALE": "magenta", "INVALID": "red",
        "REVOKED": "red", "UNKNOWN": "magenta", "BLOCKED": "red", "INCOMPLETE": "yellow",
        "UNSUPPORTED": "magenta",
    }[assessment.current_status.value]
    console.print(Panel(
        f"[bold {color}]{assessment.current_status.value}[/bold {color}] — governance review only; "
        "not external EDA signoff",
        title="Constraint review & approval boundary", border_style=color,
    ))
    console.print(explain_constraint_review(assessment))


@app.command()
def readiness(
    config: str = typer.Argument(..., help="Path to project YAML"),
    ucm: str = typer.Option(..., "--ucm", help="Current canonical UCM JSON snapshot (required)"),
    scenario_ids: Annotated[list[str] | None, typer.Option(
        "--scenario", help="Restrict report to an active MCMM scenario (repeatable)",
    )] = None,
    json_out: bool = typer.Option(False, "--json", help="Output deterministic readiness JSON only"),
):
    """Assess constraint readiness without changing UCM or executing EDA.

    A current canonical UCM snapshot is deliberately required. Advisory
    inference is generated only in memory for suggested workflow context: it
    is never materialized, accepted, saved, or used as a substitute for UCM.
    This command writes no UCM/SDC/coverage/history/artifact/application state
    and does not run synthesis, STA, or formal proof execution.
    """
    configure_logging(level="WARNING" if json_out else "INFO")
    cfg = _load(config)
    cset = _load_canonical_ucm(ucm, cfg)
    design, _ = _do_parse(cfg)
    timing_graph = _do_timing(cfg, design)
    inference_report = InferenceEngine().infer_candidates(
        design, timing_graph, cfg, cset, AssumptionLedger(), knowledge=KnowledgeEngine(),
    )
    report = assess_constraint_readiness(
        cfg, cset, design, timing_graph,
        inference_report=inference_report,
        scenario_ids=tuple(scenario_ids or ()),
    )
    if json_out:
        typer.echo(json.dumps(report.to_dict(), indent=2, sort_keys=True, default=str))
        return
    color = {
        "READY": "green", "READY_WITH_WARNINGS": "yellow", "INCOMPLETE": "yellow",
        "UNKNOWN": "magenta", "BLOCKED": "red", "UNSUPPORTED": "red",
    }[report.status.value]
    console.print(Panel(
        f"[bold {color}]{report.status.value}[/bold {color}] — report only; canonical UCM unchanged",
        title="Constraint readiness & closure",
        border_style=color,
    ))
    console.print(explain_constraint_readiness(report))


@app.command()
def validate(config: str = typer.Argument(..., help="Path to project YAML"),
             sdc: str | None = typer.Option(None, help="Path to existing SDC to validate"),
             backend: str = typer.Option("generic", help="SDC backend name")):
    """Validate generated (or imported) SDC against design model."""
    configure_logging(level="INFO")
    cfg = _load(config)
    design, _ = _do_parse(cfg)
    tg = _do_timing(cfg, design)
    ledger = AssumptionLedger()
    parser_obj = None
    if sdc:
        parser = SDCParser()
        cset = parser.parse_file(sdc)
        if parser.warnings:
            for w in parser.warnings:
                console.print(f"[yellow]SDC parser: {w}[/yellow]")
        parser_obj = parser
    else:
        cset, _ = _do_inference(cfg, design, tg, ledger)
    # When MCMM is enabled, restrict scenario validation to the active
    # scenario set so scenario-specific findings keep their identity.
    matrix = _mcmm_matrix(cfg, cset)
    active = matrix.active_ids if matrix.is_enabled else None
    result = run_validation(design, tg, cset, backend=backend,
                            active_scenarios=set(active) if active else None,
                            parser=parser_obj, formal_backend=_formal_backend(cfg))
    am = _am(cfg)
    am.write_json("validation_report.json", result.as_dict())
    if result.coverage:
        am.write_json("coverage_report.json", result.coverage.as_dict())
    _maybe_print_matrix(cfg, cset, console)
    _print_validation_summary(console, result)


def _print_validation_summary(console, result):
    from rich.table import Table
    status_color = {"PASS": "green", "PASS_WITH_WARNINGS": "yellow",
                    "BLOCKED": "red", "ERROR": "red"}.get(result.status, "white")
    console.print(Panel(f"[cyan]Validation Report — [{status_color}]{result.status}[/]"))
    console.print(f"  Errors:   [{'red' if result.errors else 'green'}]{len(result.errors)}[/]")
    console.print(f"  Warnings: [{'yellow' if result.warnings else 'green'}]{len(result.warnings)}[/]")
    console.print(f"  Blocking: [{'red' if result.blocking else 'green'}]{len(result.blocking)}[/]")
    if result.coverage:
        cov = result.coverage.as_dict()
        label_map = {
            "clock_source_coverage_pct": "Clock source coverage",
            "input_timing_path_coverage_pct": "Input timing path coverage",
            "output_timing_path_coverage_pct": "Output timing path coverage",
            "reg_to_reg_coverage_pct": "Register-to-register coverage",
            "cdc_path_coverage_pct": "CDC path coverage",
            "clock_relationship_coverage_pct": "Clock relationship coverage",
        }
        for k, label in label_map.items():
            v = cov.get(k)
            if v is None:
                continue
            if v == "UNKNOWN":
                console.print(f"  {label}: [dim]UNKNOWN[/]")
            elif v == "NOT_APPLICABLE":
                console.print(f"  {label}: [dim]NOT_APPLICABLE[/]")
            else:
                console.print(f"  {label}: {v:.1f}%")
    all_issues = result.errors + result.warnings
    if all_issues:
        t = Table(title="Issues (grouped by severity)")
        t.add_column("Severity"); t.add_column("Code"); t.add_column("Message")
        for i in all_issues:
            t.add_row(i.severity.value, i.code.value, i.message)
        console.print(t)


@app.command()
def coverage(config: str = typer.Argument(..., help="Path to project YAML")):
    """Report constraint coverage over timing-path categories."""
    configure_logging(level="INFO")
    cfg = _load(config)
    design, _ = _do_parse(cfg)
    tg = _do_timing(cfg, design)
    ledger = AssumptionLedger()
    cset, _ = _do_inference(cfg, design, tg, ledger)
    result = run_validation(design, tg, cset, formal_backend=_formal_backend(cfg))
    cov = result.coverage
    am = _am(cfg)
    am.write_json("coverage_report.json", cov.as_dict() if cov else {})
    _maybe_print_matrix(cfg, cset, console)
    console.print(Panel("[cyan]Coverage Report[/cyan]"))
    if cov:
        for k, v in cov.as_dict().items():
            if isinstance(v, (int, float)):
                console.print(f"  {k}: {v:.1f}%" if "pct" in k else f"  {k}: {v}")
        if cov.uncovered:
            console.print("\n[bold]Uncovered objects:[/bold]")
            for u in cov.uncovered:
                console.print(f"  - [{u['classification']}] {u['message']}")


@app.command()
def compare(config: str = typer.Argument(..., help="Path to project YAML"),
            a: str = typer.Option(..., "--a", help="First SDC file"),
            b: str = typer.Option(..., "--b", help="Second SDC file"),
            json_out: bool = typer.Option(False, "--json", help="Emit JSON report")):
    """Semantically compare two SDC files (UCM-level, Step 9).

    Differences are reported at field granularity; provenance is
    separated from semantic identity; unsupported or unresolved options
    surface as UNKNOWN rather than false equivalence.
    """
    # JSON is a machine-readable contract, so suppress informational logs that
    # a previous in-process CLI command may have enabled.
    configure_logging(level="WARNING" if json_out else "INFO")
    # Retain the project-config argument as part of the established CLI
    # contract, but compare the supplied SDC inputs through the hardened
    # importer.  The legacy SDCParser silently drops unsupported commands,
    # which could otherwise turn materially unknown intent into EQUIVALENT.
    _load(config)
    try:
        text_a = Path(a).read_text(encoding="utf-8", errors="replace")
        text_b = Path(b).read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        console.print(f"[red]Failed to read SDC: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    result = compare_sdc_text(text_a, text_b, source_a=a, source_b=b)
    if json_out:
        # ``typer.echo`` binds to Click's current invocation stream.  Unlike a
        # module-level Rich console it stays correctly captured across repeated
        # in-process CLI invocations (for example integration tests).
        typer.echo(json.dumps(result.to_dict(), indent=2, sort_keys=True, default=str))
        return
    status_color = {
        "EQUIVALENT": "green",
        "EQUIVALENT_AFTER_NORMALIZATION": "green",
        "DIFFERENT": "yellow",
        "PARTIALLY_EQUIVALENT": "yellow",
        "NON_EQUIVALENT": "red",
        "UNKNOWN": "magenta",
        "ERROR": "red",
    }.get(result.overall_status.value, "white")
    console.print(Panel(f"[cyan]SDC COMPARISON[/cyan]  {a}  vs  {b}"))
    console.print(f"  Status: [bold {status_color}]{result.overall_status.value}[/]  "
                  f"(level={result.comparison_level.value})")
    c = result.counts()
    console.print(f"  Equivalent: [green]{c['equivalent']}[/]"
                  f"   Different: [yellow]{c['different']}[/]"
                  f"   Only in A: {c['only_in_left']}"
                  f"   Only in B: {c['only_in_right']}"
                  f"   Unknown: [magenta]{c['unknown']}[/]")
    console.print(f"  Duplicates in A: {c['duplicates_left']}   "
                  f"Duplicates in B: {c['duplicates_right']}"
                  f"   Scenario context findings: {c['scenario_differences']}")
    if result.scenario_differences:
        console.print("\n[bold]Scenario context findings:[/bold]")
        for finding in result.scenario_differences[:10]:
            where = finding.scenario_id or "active scenario matrix"
            category = escape(f"[{finding.status}]")
            console.print(f"  - {category} {escape(where)}: {escape(finding.field)}")
            console.print(f"      {escape(finding.explanation)}")
    if result.different_constraints:
        console.print("\n[bold]Top semantic differences:[/bold]")
        for shown, p in enumerate(result.different_constraints):
            if shown >= 10:
                console.print(f"  … and {len(result.different_constraints)-shown} more")
                break
            ids = f"{p.a_id} → {p.b_id}" if p.a_id and p.b_id else (p.a_id or p.b_id or "")
            category = escape(f"[{p.constraint_type}]")
            console.print(f"  - {category} {escape(ids)}")
            for fld in p.fields[:3]:
                console.print(
                    f"      {escape(fld.field)}: A={escape(repr(fld.value_a))}  "
                    f"B={escape(repr(fld.value_b))}"
                )
                console.print(f"        {escape(fld.explanation)}")
            provenance = []
            if p.a_provenance is not None:
                provenance.append(f"A={p.a_provenance!r}")
            if p.b_provenance is not None:
                provenance.append(f"B={p.b_provenance!r}")
            if provenance:
                console.print(f"      Provenance: {escape('; '.join(provenance))}")
    if result.only_in_left:
        console.print("\n[bold]Only in A:[/bold]")
        for p in result.only_in_left[:10]:
            category = escape(f"[{p.constraint_type}]")
            console.print(f"  - {category} {escape(p.a_id or '')}")
    if result.only_in_right:
        console.print("\n[bold]Only in B:[/bold]")
        for p in result.only_in_right[:10]:
            category = escape(f"[{p.constraint_type}]")
            console.print(f"  - {category} {escape(p.b_id or '')}")
    if result.unknown_constraints:
        console.print("\n[magenta][bold]UNKNOWN (cannot prove equivalence):[/bold][/magenta]")
        for p in result.unknown_constraints[:10]:
            ids = f"{p.a_id or '?'} vs {p.b_id or '?'}"
            category = escape(f"[{p.constraint_type}]")
            console.print(f"  - {category} {escape(ids)}")
            for n in p.notes[:3]:
                console.print(f"      {escape(n)}")
            provenance = []
            if p.a_provenance is not None:
                provenance.append(f"A={p.a_provenance!r}")
            if p.b_provenance is not None:
                provenance.append(f"B={p.b_provenance!r}")
            if provenance:
                console.print(f"      Provenance: {escape('; '.join(provenance))}")
    if result.normalization_notes:
        console.print("\n[dim]Normalization notes:[/dim]")
        for n in result.normalization_notes:
            console.print(f"  [dim]- {n}[/dim]")


@app.command(name="explain")
def explain_cmd(config: str = typer.Argument(..., help="Path to project YAML"),
                constraint_id: str | None = typer.Option(None, "--constraint", "-c"),
                candidate_id: str | None = typer.Option(None, "--candidate")):
    """Explain a constraint or candidate decision."""
    cfg = _load(config)
    am = _am(cfg)
    if constraint_id:
        cset_path = am.path("constraint_model.json")
        if not cset_path.is_file():
            console.print("[red]Run `rca generate` first to produce a constraint model.[/red]")
            raise typer.Exit(1)
        # Load and rebuild
        design, _ = _do_parse(cfg); tg = _do_timing(cfg, design)
        ledger = AssumptionLedger()
        cset, _ = _do_inference(cfg, design, tg, ledger)
        c = cset.get(constraint_id)
        if not c:
            console.print(f"[red]Constraint '{constraint_id}' not found.[/red]")
            raise typer.Exit(2)
        console.print(explain_constraint(c))
        return
    if candidate_id:
        console.print("[yellow]Candidate explanation requires the optimizer DB; load candidates.jsonl.[/yellow]")
        return
    console.print("Specify --constraint ID to explain a constraint.")


@app.command(name="run-sta")
def run_sta(config: str = typer.Argument(..., help="Path to project YAML"),
            backend: str = typer.Option("yosys_opensta", help="EDA flow: yosys_opensta|mock"),
            sdc: str | None = typer.Option(None, help="SDC file to use (defaults to generated)"),
            force: bool = typer.Option(False, "--force", help="Bypass cache and rerun"),
            allow_partial_sdc: bool = typer.Option(False, "--allow-partial-sdc",
                                                  help="Exploratory: allow PARTIAL SDC to reach STA")):
    """Run synthesis + STA on the design and collect QoR (Step 10)."""
    configure_logging(level="INFO")
    cfg = _load(config)
    design, _parse_diags = _do_parse(cfg)
    tg = _do_timing(cfg, design)
    ledger = AssumptionLedger()
    cset, _inf_report = _do_inference(cfg, design, tg, ledger)
    sdc_backend = get_backend("opensta" if "opensta" in backend else "generic")
    am = _am(cfg)

    # ---- MCMM (Step 12 §13, §14) ----
    matrix = _mcmm_matrix(cfg, cset)
    mcmm_enabled = bool(matrix.is_enabled and matrix.scenario_count > 1)
    if mcmm_enabled:
        from ..optimizer import Candidate
        base_cand = Candidate(id="baseline", constraint_set=cset)
        if backend == "mock":
            ev = mock_mcmm_evaluator(matrix, base_cset=cset)
            m = ev(base_cand, Path(cfg.flow.output_dir))
            am.write_json("mcmm_report.json", m.to_dict())
            session_id, persistence_warning = _record_standalone_mcmm_history(cfg, cset, m)
            if session_id:
                console.print(f"[dim]Historical QoR session: {session_id}[/dim]")
            if persistence_warning:
                console.print(f"[yellow]{persistence_warning}[/yellow]")
            _print_mcmm_result(console, m, matrix)
            return
        # Real backend: run the flow once per active scenario, then aggregate.
        sources = resolve_sources(cfg)
        include_dirs = resolve_include_dirs(cfg)
        defines = dict(getattr(cfg, "sources", None).defines or {}) if getattr(cfg, "sources", None) else {}
        per_scenario: dict[str, dict] = {}
        for scenario in matrix.active_scenarios():
            sdc_text = _mcmm_per_scenario_sdc(cset, sdc_backend, cfg.project.name,
                                              matrix, scenario.id)
            sdc_path = am.path(f"design.{scenario.id}.sdc")
            sdc_path.parent.mkdir(parents=True, exist_ok=True)
            sdc_path.write_text(sdc_text, encoding="utf-8")
            res = run_flow(
                cfg=cfg, cset=cset, sdc_text=sdc_text,
                sdc_generation_status="COMPLETE",
                sources=sources, include_dirs=include_dirs, defines=defines,
                output_dir=Path(cfg.flow.output_dir), backend=backend,
                candidate_id=f"baseline_{scenario.id}",
                scenario=scenario.id, corner=scenario.corner, mode=scenario.mode,
                allow_partial_sdc=allow_partial_sdc, force=force,
            )
            per_scenario[scenario.id] = res
        # ``qor_result`` is an internal canonical object used by optimizer
        # callbacks; persist the established JSON summary, not its repr.
        am.write_json("mcmm_report.json", {
            sid: {key: value for key, value in res.items() if key != "qor_result"}
            for sid, res in per_scenario.items()
        })
        # Keep the established mcmm_report.json layout untouched, then build
        # the existing MCMM model solely for relational aggregate indexing.
        from ..mcmm import (
            BLOCKED,
            ScenarioQoR,
            aggregate_objectives,
            finalize_limiting,
            global_feasibility,
            global_margin,
        )
        aggregate = MCMMResult(candidate_id="baseline", active_scenario_ids=list(matrix.active_ids))
        for sid in matrix.active_ids:
            scenario = matrix.scenario(sid)
            res = per_scenario[sid]
            sqor = ScenarioQoR(
                candidate_id="baseline", scenario_id=sid, mode=scenario.mode, corner=scenario.corner,
                name=scenario.name, qor=res.get("qor_result"), cache_key=res.get("cache_key", ""),
                cache_status=("HIT" if res.get("status") == "CACHE_HIT" else
                              "MISS" if res.get("cache_key") else res.get("status", "")),
                run_id=res.get("run_id", ""), backend=backend, tool=backend,
            )
            # A failed/blocked scenario has no canonical QoR for the shared
            # MCMM helper to classify. Preserve that observed flow status
            # explicitly rather than allowing ScenarioQoR's historical default
            # of ``infeasible`` to recast it as a timing verdict.
            if sqor.qor is None:
                sqor.status = BLOCKED
                sqor.blocked = True
                sqor.infeasible_reason = str(
                    res.get("blocked_reason") or "; ".join(res.get("diagnostics") or []) or
                    res.get("status") or "no QoR result"
                )
            aggregate.scenario_results[sid] = sqor
            if sqor.run_id:
                aggregate.run_ids.append(sqor.run_id)
        aggregate.eda_runs = len(aggregate.scenario_results)
        global_feasibility(aggregate)
        aggregate_objectives(aggregate)
        global_margin(aggregate)
        finalize_limiting(aggregate)
        session_id, persistence_warning = _record_standalone_mcmm_history(cfg, cset, aggregate)
        if session_id:
            console.print(f"[dim]Historical QoR session: {session_id}[/dim]")
        if persistence_warning:
            console.print(f"[yellow]{persistence_warning}[/yellow]")
        for sid, res in per_scenario.items():
            console.print(f"[cyan]Scenario {sid}[/cyan]  status={res.get('status')}  "
                          f"run_id={res.get('run_id')}")
            if isinstance(res.get("qor"), dict):
                _print_power_summary(console, res["qor"], indent="    ")
        return

    # Generate SDC (using balanced safe mode by default; user can switch via --allow-partial)
    from ..utils.enums import SafeMode
    gen = sdc_backend.generate(cset, design_name=cfg.project.name,
                               mode=SafeMode.BALANCED, with_provenance=True)
    if sdc:
        sdc_path = Path(sdc)
        sdc_text = sdc_path.read_text(encoding="utf-8")
    else:
        sdc_text = gen.text

    sources = resolve_sources(cfg)
    include_dirs = resolve_include_dirs(cfg)
    defines = dict(getattr(cfg, "sources", None).defines or {}) if getattr(cfg, "sources", None) else {}

    # Preserve a configured single scenario's identity when present so a
    # scenario-labelled power report is never silently ignored or rebound.
    flow_scenario = "default"
    flow_corner = "default"
    flow_mode = "default"
    if matrix.scenario_count == 1 and cfg.scenarios:
        only_scenario = matrix.active_scenarios()[0]
        flow_scenario = only_scenario.id
        flow_corner = only_scenario.corner
        flow_mode = only_scenario.mode
    result = run_flow(
        cfg=cfg, cset=cset, sdc_text=sdc_text,
        sdc_generation_status=gen.status if isinstance(gen.status, str) else gen.status.value,
        sources=sources, include_dirs=include_dirs, defines=defines,
        output_dir=Path(cfg.flow.output_dir), backend=backend,
        candidate_id="baseline", scenario=flow_scenario, corner=flow_corner, mode=flow_mode,
        allow_partial_sdc=allow_partial_sdc,
        force=force,
    )

    # Emit SDC even if cached (for inspection)
    if not sdc:
        sdc_out = am.path("design.generated.sdc")
        sdc_out.parent.mkdir(parents=True, exist_ok=True)
        sdc_out.write_text(sdc_text, encoding="utf-8")

    status = result["status"]
    status_color = {"SUCCESS": "green", "MOCK": "cyan", "CACHE_HIT": "blue",
                    "BLOCKED": "yellow", "SYNTHESIS_FAILED": "red",
                    "STA_FAILED": "red", "TIMING_FAIL": "yellow",
                    "ERROR": "red"}.get(status, "white")

    console.print(Panel(f"[cyan]EDA RUN[/cyan]  id={result['run_id']}"))
    console.print(f"  Status: [{status_color}]{status}[/]")
    console.print(f"  Run dir: {result['run_dir']}")
    preflight = result.get("preflight")
    if isinstance(preflight, dict):
        preflight_state = "READY" if preflight.get("ready") else "BLOCKED"
        console.print(f"  Real-EDA preflight: {preflight_state}"
                      f"  ({preflight.get('failure_classification') or 'no failure'})")
        if not preflight.get("ready"):
            for check in preflight.get("checks", []):
                if check.get("required") and not check.get("ready"):
                    console.print(f"    - {check.get('component')}: {check.get('status')} — "
                                  f"{check.get('detail')}")
    if result.get("synth") and isinstance(result["synth"], dict):
        si = result["synth"].get("tool_info") or {}
        yinfo = si.get("yosys") if isinstance(si, dict) else None
        if yinfo:
            console.print(f"  Yosys:  {yinfo.get('executable','?')}  ({yinfo.get('version','?')})")
    if result.get("sta") and isinstance(result["sta"], dict):
        si = result.get("manifest", {}).get("extra", {}).get("tool_info", {})
        oinfo = si.get("opensta") if isinstance(si, dict) else None
        if oinfo:
            console.print(f"  OpenSTA: {oinfo.get('executable','?')}  ({oinfo.get('version','?')})")
    if result.get("qor"):
        q = result["qor"]
        console.print("\n[bold]TIMING[/bold]")
        def _ns(x): return f"{x:.3f} ns" if isinstance(x, (int, float)) else "n/a"
        console.print(f"  Setup WNS: {_ns(q.get('setup_wns_ns'))}   TNS: {_ns(q.get('setup_tns_ns'))}  violations={q.get('setup_violations')}")
        console.print(f"  Hold  WNS: {_ns(q.get('hold_wns_ns'))}   TNS: {_ns(q.get('hold_tns_ns'))}  violations={q.get('hold_violations')}")
        console.print("\n[bold]QoR[/bold]")
        area = q.get("area") if q.get("area") is not None else q.get("area_proxy")
        area_label = "area" if q.get("area") is not None else "area_proxy (cell_count)"
        console.print(f"  Cell count: {q.get('cell_count')}   FF: {q.get('ff_count')}")
        console.print(f"  {area_label}: {area}")
        _print_power_summary(console, q)
        if q.get("critical_setup"):
            cs = q["critical_setup"]
            console.print(f"  Worst setup path: {cs.get('startpoint')} -> {cs.get('endpoint')}  "
                          f"(group {cs.get('path_group')}, slack={_ns(cs.get('slack'))})")
    if result.get("diagnostics"):
        console.print("\n[yellow]Diagnostics:[/yellow]")
        for d in result["diagnostics"]:
            console.print(f"  - {d}")
    if status in ("BLOCKED", "SYNTHESIS_FAILED", "STA_FAILED", "ERROR"):
        raise typer.Exit(code=2)


@app.command()
def optimize(config: str = typer.Argument(..., help="Path to project YAML"),
             backend: str = typer.Option("mock", help="EDA backend for closed-loop: mock|yosys_opensta"),
             dashboard: bool = typer.Option(False, "--dashboard", help="Launch web dashboard")):
    """Run multi-objective constraint optimization."""
    configure_logging(level="INFO")
    cfg = _load(config)
    if not cfg.optimization.enabled:
        console.print("[yellow]Optimization is disabled in config; set optimization.enabled = true to run.[/yellow]")
        if not typer.confirm("Enable temporarily and run?"):
            raise typer.Exit(1)
        cfg.optimization.enabled = True
    design, _ = _do_parse(cfg)
    tg = _do_timing(cfg, design)
    ledger = AssumptionLedger()
    cset, _ = _do_inference(cfg, design, tg, ledger)
    am = _am(cfg)
    runs_dir = am.path("runs") / "opt"
    runs_dir.mkdir(parents=True, exist_ok=True)
    sdc_backend = get_backend("opensta" if "opensta" in backend else "generic")
    sources = resolve_sources(cfg)

    # ---- MCMM (Step 12 §13) ----
    matrix = _mcmm_matrix(cfg, cset)
    mcmm_enabled = bool(matrix.is_enabled and matrix.scenario_count > 1)

    if mcmm_enabled:
        from ..mcmm import MCMMEvaluator

        def _real_scenario_evaluate(scenario, cand, work):
            cand_cset = cand.constraint_set or cset
            sdc_text = _mcmm_per_scenario_sdc(
                cand_cset, sdc_backend, cfg.project.name, matrix, scenario.id)
            task_run_id = _parallel_task_flow_id(work, cand.id, scenario.id)
            flow_result = run_flow(
                cfg=cfg, cset=cand_cset, sdc_text=sdc_text,
                sdc_generation_status="COMPLETE", sources=sources,
                include_dirs=resolve_include_dirs(cfg),
                output_dir=Path(cfg.flow.output_dir), backend="yosys_opensta",
                run_id=task_run_id, candidate_id=cand.id, scenario=scenario.id,
                corner=scenario.corner, mode=scenario.mode,
                defer_history_indexing=task_run_id is not None,
            )
            return {
                "qor": flow_result.get("qor_result"),
                "cache_key": flow_result.get("cache_key", ""),
                "cache_status": flow_result.get("status", ""),
                "run_id": flow_result.get("run_id", ""),
                "_deferred_history_evidence": flow_result.get("_deferred_history_evidence"),
            }

        if backend == "mock":
            evaluate = mock_mcmm_evaluator(matrix, base_cset=cset)
        else:
            evaluate = MCMMEvaluator(
                matrix, evaluate_scenario=_real_scenario_evaluate,
                base_cset=cset, name=backend)
    else:
        def evaluate(cand, work):
            cand_cset = cand.constraint_set or cset
            sdc_text = sdc_backend.render(cand_cset, design_name=cfg.project.name)
            if backend == "mock":
                tool = MockEDA()
                return tool.evaluate_candidate(cand, work)
            task_run_id = _parallel_task_flow_id(work, cand.id, "default")
            flow_result = run_flow(
                cfg=cfg, cset=cand_cset, sdc_text=sdc_text,
                sdc_generation_status="COMPLETE", sources=sources,
                include_dirs=resolve_include_dirs(cfg),
                output_dir=Path(cfg.flow.output_dir), backend="yosys_opensta",
                run_id=task_run_id, candidate_id=cand.id,
                defer_history_indexing=task_run_id is not None,
            )
            if task_run_id is not None:
                # Preserve the transient history evidence beside the canonical
                # QoR return value for the optimizer coordinator.
                return {
                    "qor": flow_result.get("qor_result"),
                    "cache_key": flow_result.get("cache_key", ""),
                    "cache_status": flow_result.get("status", ""),
                    "run_id": flow_result.get("run_id", ""),
                    "_deferred_history_evidence": flow_result.get("_deferred_history_evidence"),
                }
            return flow_result.get("qor_result")

    opt = Optimizer(cfg, evaluate_fn=evaluate, work_dir=runs_dir)
    result = opt.run(cset)
    # Candidates JSONL remains the established interoperable candidate record.
    cj_path = am.path("candidates.jsonl")
    with cj_path.open("w") as f:
        for c in result.all_candidates:
            f.write(json.dumps(c.to_dict(), default=str) + "\n")
    pareto_path = am.write_json("pareto_frontier.json", [c.to_dict() for c in result.pareto])
    final_sdc_path = None
    if result.final and result.final.constraint_set:
        final_sdc = sdc_backend.render(result.final.constraint_set, design_name=cfg.project.name)
        final_sdc_path = am.write_text("design.final.sdc", final_sdc)
    # Persist existing optimizer state plus the new deterministic ledger and
    # its normal RunManifest before advisory SQLite work begins.
    execution_artifacts = _persist_optimizer_execution_artifacts(
        am, cfg, result, candidates_path=cj_path, pareto_path=pareto_path,
        final_sdc_path=final_sdc_path,
    )
    # Parallel workers return completed physical-flow evidence rather than
    # writing SQLite. The coordinator consumes it in deterministic task order.
    # Database warnings are advisory and intentionally do not rewrite the
    # authoritative optimizer state/ledger/manifest artifacts.
    for evidence in opt.deferred_history_evidence:
        warning = index_deferred_history_evidence(evidence)
        if warning:
            console.print(f"[yellow]{warning}[/yellow]")

    history_session_id = None
    try:
        history_session_id = SQLiteQoRRepository.for_output_dir(cfg.flow.output_dir).record_optimizer_session(
            result, project_name=cfg.project.name, output_dir=cfg.flow.output_dir,
        )
    except Exception as exc:  # noqa: BLE001 - boundary captures are intentional.
        warning = f"QOR_DATABASE_PERSISTENCE_WARNING: {type(exc).__name__}: {exc}"
        console.print(f"[yellow]{warning}[/yellow]")
    console.print(Panel("[cyan bold]Optimization complete[/cyan bold]"))
    if history_session_id:
        console.print(f"  Historical QoR session: {history_session_id}")
    console.print(f"  Stop reason: {result.stop_reason.value if result.stop_reason else 'n/a'}")
    console.print(
        f"  Execution stop: {result.execution_stop_reason.value if result.execution_stop_reason else 'n/a'}"
    )
    console.print(f"  Execution ledger: {execution_artifacts['ledger']}")
    console.print(f"  Iterations:  {result.iterations}  EDA runs: {result.eda_runs}  Elapsed: {result.elapsed_seconds:.1f}s")
    console.print(f"  Pareto size: {len(result.pareto)}")
    if mcmm_enabled:
        _print_scenario_matrix(console, matrix)
    if result.final:
        q = result.final.qor
        console.print(f"\n[green]Final candidate: {result.final.id}[/green]")
        if mcmm_enabled and result.final.mcmm is not None:
            m = result.final.mcmm
            console.print(f"  Global status: {m.global_status}")
            console.print(f"  Limiting scenarios: {', '.join(m.limiting_scenarios) or '-'}")
            for sid in m.active_scenario_ids:
                sq = m.scenario_results.get(sid)
                if sq is None:
                    continue
                s_wns = (f"{sq.qor.setup_wns * 1e9:.3f}" if sq.qor and sq.qor.setup_wns is not None else "-")
                h_wns = (f"{sq.qor.hold_wns * 1e9:.3f}" if sq.qor and sq.qor.hold_wns is not None else "-")
                utilization = f"{sq.margin_utilization:.2f}" if sq.margin_utilization is not None else "-"
                console.print(f"    [{sid} {sq.mode}/{sq.corner}] {sq.status}  "
                              f"setup={s_wns}ns  hold={h_wns}ns  util={utilization}")
                if sq.qor:
                    _print_power_summary(console, sq.qor.summary(), indent="      ")
        elif q:
            s_wns = f"{q.setup_wns * 1e9:.3f} ns" if q.setup_wns is not None else "-"
            h_wns = f"{q.hold_wns * 1e9:.3f} ns" if q.hold_wns is not None else "-"
            console.print(f"  Setup WNS: {s_wns}")
            console.print(f"  Hold WNS:  {h_wns}")
            console.print(f"  Area:      {q.area_total}")
            _print_power_summary(console, q.summary())
    if dashboard:
        _run_dashboard(cfg=cfg)


@app.command()
def report(config: str = typer.Argument(..., help="Path to project YAML")):
    """Print a full human-readable report."""
    configure_logging(level="WARNING")
    cfg = _load(config)
    design, _diag = _do_parse(cfg)
    tg = _do_timing(cfg, design)
    ledger = AssumptionLedger()
    cset, _ = _do_inference(cfg, design, tg, ledger)
    val_result = run_validation(design, tg, cset, formal_backend=_formal_backend(cfg))
    missing = tg.missing_information()
    latest_qor = _latest_qor_summary(cfg)
    text = design_report(design.summary(), tg.summary(), val_result.as_dict(),
                         val_result.coverage.as_dict() if val_result.coverage else None,
                         cset, missing, latest_qor)
    am = _am(cfg)
    am.write_text("inference_report.txt", text)
    _maybe_print_matrix(cfg, cset, console)
    am.write_json("inference_report.json",
                  {"design": design.summary(), "timing": tg.summary(),
                   "validation": val_result.as_dict(),
                   "coverage": val_result.coverage.as_dict() if val_result.coverage else None,
                   "qor": latest_qor,
                   "constraints": cset.snapshot()})
    sys.stdout.write(text)


def _knowledge_engine(knowledge_files: list[str], history_output_dir: str | None) -> KnowledgeEngine:
    """Create an advisory corpus from declared, local, non-executing sources."""
    engine = KnowledgeEngine()
    try:
        for path in sorted(set(knowledge_files)):
            engine.add_patterns(load_knowledge_file(path))
        if history_output_dir:
            engine.index_history(SQLiteQoRRepository.for_output_dir(history_output_dir))
    except KnowledgeError as exc:
        console.print(f"[red]Knowledge input error: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    return engine


@knowledge_app.command("list")
def knowledge_list(
    knowledge_file: Annotated[list[str] | None, typer.Option("--knowledge", "-k", help="Strict JSON knowledge file (repeatable)")] = None,
    history_output_dir: str | None = typer.Option(None, "--history-output-dir", help="Read-only local QoR sidecar directory"),
    json_out: bool = typer.Option(False, "--json", help="Emit deterministic JSON"),
):
    """List loaded knowledge items; no project, UCM, or history row is modified."""
    engine = _knowledge_engine(knowledge_file or [], history_output_dir)
    data = {
        "kind": "rca_knowledge_list",
        "schema_version": 1,
        "advisory": True,
        "items": [pattern.to_dict() for pattern in engine.patterns()],
        "diagnostics": sorted(engine.diagnostics),
    }
    if json_out:
        sys.stdout.write(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")
        return
    console.print(Panel("[cyan]Constraint knowledge[/cyan] — offline advisory items, not UCM constraints"))
    table = Table(title="Knowledge patterns")
    table.add_column("ID"); table.add_column("Origin"); table.add_column("Trust")
    table.add_column("Title")
    for item in data["items"]:
        table.add_row(item["id"], item["origin"], item["trust_level"], item["title"])
    console.print(table)
    for diagnostic in data["diagnostics"]:
        console.print(f"[yellow]{diagnostic}[/yellow]")


@knowledge_app.command("show")
def knowledge_show(
    item_id: str = typer.Argument(..., help="Knowledge item ID"),
    knowledge_file: Annotated[list[str] | None, typer.Option("--knowledge", "-k", help="Strict JSON knowledge file (repeatable)")] = None,
    history_output_dir: str | None = typer.Option(None, "--history-output-dir", help="Read-only local QoR sidecar directory"),
    json_out: bool = typer.Option(False, "--json", help="Emit deterministic JSON"),
):
    """Show one pattern and its retained provenance without accepting it."""
    engine = _knowledge_engine(knowledge_file or [], history_output_dir)
    item = engine.get(item_id)
    if item is None:
        console.print(f"[red]Knowledge item '{item_id}' was not found.[/red]")
        raise typer.Exit(code=2)
    data = {"kind": "rca_knowledge_item", "schema_version": 1, "advisory": True,
            "item": item.to_dict(), "diagnostics": sorted(engine.diagnostics)}
    if json_out:
        sys.stdout.write(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")
    else:
        console.print_json(json.dumps(data, indent=2, sort_keys=True, default=str))


@knowledge_app.command("search")
def knowledge_search(
    query: str = typer.Argument(..., help="Text to search over pattern IDs, titles, descriptions, and types"),
    knowledge_file: Annotated[list[str] | None, typer.Option("--knowledge", "-k", help="Strict JSON knowledge file (repeatable)")] = None,
    history_output_dir: str | None = typer.Option(None, "--history-output-dir", help="Read-only local QoR sidecar directory"),
    limit: int = typer.Option(20, "--limit", min=0, help="Maximum deterministic result count"),
    json_out: bool = typer.Option(False, "--json", help="Emit deterministic JSON"),
):
    """Search advisory patterns only; ranking is relevance, not correctness."""
    engine = _knowledge_engine(knowledge_file or [], history_output_dir)
    data = {"kind": "rca_knowledge_search", "schema_version": 1, "advisory": True,
            **engine.search(text=query, limit=limit).to_dict()}
    if json_out:
        sys.stdout.write(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")
    else:
        console.print(Panel("[cyan]Constraint knowledge search[/cyan] — relevance is not a correctness probability"))
        console.print_json(json.dumps(data, indent=2, sort_keys=True, default=str))


@knowledge_app.command("suggest")
def knowledge_suggest(
    config: str | None = typer.Argument(None, help="Optional project YAML used only to build a read-only inferred UCM view"),
    knowledge_file: Annotated[list[str] | None, typer.Option("--knowledge", "-k", help="Strict JSON knowledge file (repeatable)")] = None,
    history_output_dir: str | None = typer.Option(None, "--history-output-dir", help="Read-only local QoR sidecar directory"),
    limit_per_constraint: int = typer.Option(5, "--limit-per-constraint", min=0, help="Deterministic result cap per UCM constraint"),
    json_out: bool = typer.Option(False, "--json", help="Emit deterministic JSON"),
):
    """Produce advisory project matches; never accepts or changes project intent.

    A supplied config is parsed/inferred in memory exactly as other analysis
    commands do. The only output is a separate advisory JSON artifact; source
    config, canonical UCM inputs, SQLite history, and SDC are not changed.
    """
    configure_logging(level="WARNING" if json_out else "INFO")
    engine = _knowledge_engine(knowledge_file or [], history_output_dir)
    cset = None
    report = None
    project_name = None
    artifact_path = None
    suggestions = []
    references: list[dict[str, Any]] = []
    if config:
        cfg = _load(config)
        design, _ = _do_parse(cfg)
        timing_graph = _do_timing(cfg, design)
        cset, report = _do_inference(cfg, design, timing_graph, AssumptionLedger())
        project_name = cfg.project.name
        # This is a pure index of the inferred view; it never writes it back.
        engine.index_constraint_set(cset)
        suggestions = engine.suggestions_for_constraint_set(cset, limit_per_constraint=limit_per_constraint)
        references = engine.inference_references(report, cset)
        payload = {
            "kind": "rca_knowledge_suggestions", "schema_version": 1, "advisory": True,
            "project": project_name, "suggestions": [item.to_dict() for item in suggestions],
            "inference_knowledge_references": references, "diagnostics": sorted(engine.diagnostics),
            "acceptance": "No suggestion was accepted or written to the project.",
        }
        artifact_path = _am(cfg).write_json("knowledge_suggestions.json", payload)
    data = {
        "kind": "rca_knowledge_suggestions", "schema_version": 1, "advisory": True,
        "project": project_name, "suggestions": [item.to_dict() for item in suggestions],
        "inference_knowledge_references": references, "diagnostics": sorted(engine.diagnostics),
        "artifact": str(artifact_path) if artifact_path else None,
        "acceptance": "No suggestion was accepted or written to the project.",
    }
    if not config:
        data["diagnostics"].append("No project config supplied; no project-UCM applicability check was performed.")
        data["diagnostics"].sort()
    if json_out:
        sys.stdout.write(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")
    else:
        console.print(Panel("[cyan]Constraint knowledge suggestions[/cyan] — advisory only; no UCM mutation"))
        console.print_json(json.dumps(data, indent=2, sort_keys=True, default=str))


@app.command()
def history(
    run_id: str | None = typer.Option(None, "--run-id", help="Physical run/evaluation id"),
    candidate_id: str | None = typer.Option(None, "--candidate", help="Candidate id"),
    session_id: str | None = typer.Option(None, "--session", help="Optimization session id"),
    scenario_id: str | None = typer.Option(None, "--scenario", help="Scenario id"),
    constraint_set_hash: str | None = typer.Option(None, "--constraint-set", help="Constraint-set hash"),
    best: str | None = typer.Option(None, "--best", help="setup_wns|area|power"),
    import_legacy: bool = typer.Option(False, "--import-legacy", help="Explicitly index existing run artifacts"),
    optimization_ledger: bool = typer.Option(
        False, "--optimization-ledger",
        help="Read the authoritative optimizer execution ledger (no SQLite query)",
    ),
    include_mock: bool = typer.Option(False, "--include-mock", help="Include mock evidence in best queries"),
    area_source: str | None = typer.Option(None, "--area-source", help="real|proxy for --best area"),
    output_dir: str = typer.Option(
        "output", "--output-dir",
        help="Flow output directory containing optimizer artifacts and/or qor.sqlite3",
    ),
    config: str | None = typer.Option(None, "--config", help="Project YAML; supplies flow.output_dir"),
    json_out: bool = typer.Option(False, "--json", help="Emit deterministic JSON"),
):
    """Query local historical QoR evidence; never executes EDA or cache reuse."""
    if config:
        output_dir = str(_load(config).flow.output_dir)
    selectors = sum(value is not None for value in (
        run_id, candidate_id, scenario_id, constraint_set_hash, best,
    )) + int(optimization_ledger)
    if import_legacy and selectors:
        console.print("[red]--import-legacy cannot be combined with a query selector.[/red]")
        raise typer.Exit(code=2)
    if selectors > 1:
        console.print(
            "[red]Choose one of --run-id, --candidate, --scenario, --constraint-set, "
            "--best, or --optimization-ledger.[/red]"
        )
        raise typer.Exit(code=2)
    if session_id and not candidate_id:
        console.print("[red]--session requires --candidate.[/red]")
        raise typer.Exit(code=2)
    if optimization_ledger:
        try:
            data = _read_optimizer_execution_ledger(output_dir)
        except (TypeError, ValueError) as exc:
            console.print(f"[red]Optimizer execution ledger error: {exc}[/red]")
            raise typer.Exit(code=2) from exc
        if json_out:
            sys.stdout.write(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")
            return
        if data is None:
            console.print("[yellow]No optimizer execution ledger artifact found.[/yellow]")
            return
        _print_optimizer_execution_ledger(data)
        return
    repo = SQLiteQoRRepository.for_output_dir(output_dir)
    try:
        if import_legacy:
            data: Any = repo.import_legacy_artifacts(output_dir)
        elif run_id:
            data = repo.get_replay_identity(run_id)
        elif candidate_id and session_id:
            candidate = repo.get_candidate(session_id, candidate_id)
            data = ({"candidate": candidate,
                     "lineage": repo.candidate_lineage(session_id, candidate_id),
                     "mcmm": repo.get_mcmm(session_id=session_id, candidate_id=candidate_id)}
                    if candidate else None)
        elif candidate_id:
            data = repo.list_runs(candidate_id=candidate_id)
        elif scenario_id:
            data = repo.list_runs(scenario_id=scenario_id)
        elif constraint_set_hash:
            data = repo.find_by_constraint_set(constraint_set_hash)
        elif best:
            data = repo.best_qor(best, include_mock=include_mock, area_source=area_source)
        else:
            data = repo.list_runs()
    except QoRRepositoryError as exc:
        console.print(f"[red]QoR history error: {exc}[/red]")
        raise typer.Exit(code=2) from exc
    if json_out:
        sys.stdout.write(json.dumps(data, indent=2, sort_keys=True, default=str) + "\n")
        return
    if data is None:
        console.print("[yellow]No matching QoR history record.[/yellow]")
        return
    console.print(Panel("[cyan]QoR historical repository[/cyan] — artifacts/cache remain authoritative"))
    console.print_json(json.dumps(data, indent=2, sort_keys=True, default=str))


@app.command()
def inspect(config: str = typer.Argument(..., help="Path to project YAML"),
            element: str = typer.Argument(..., help="clock|reset|port|register|module")):
    """Inspect discovered design elements."""
    cfg = _load(config)
    design, _ = _do_parse(cfg)
    tg = _do_timing(cfg, design)
    table = Table(title=f"{element.capitalize()}s in {design.name}")
    table.add_column("Name"); table.add_column("Details")
    if element == "clock":
        for n, c in tg.clocks.items():
            table.add_row(n, f"period={c.period_ns():.3f}ns, edge={c.edge.value}, regs={len(c.registers_driven)}")
    elif element == "reset":
        for n, r in tg.resets.items():
            table.add_row(n, f"type={r.reset_type.value}, pol={r.polarity.value}, regs={len(r.registers_driven)}")
    elif element == "port":
        for p in design.top_ports():
            table.add_row(p.local_name, f"dir={p.direction.value}, width={p.width}")
    elif element == "register":
        for r in design.top_registers():
            table.add_row(r.hierarchical_name, f"clk={r.clock_signal}, rst={r.reset_signal}, w={r.width}")
    elif element == "module":
        for m in design.modules.values():
            table.add_row(m.name, f"ports={len(m.port_names)}, instances={len(m.instance_names)}, processes={len(m.process_ids)}")
    else:
        console.print(f"[red]Unknown element type '{element}'.[/red] Options: clock, reset, port, register, module.")
        raise typer.Exit(2)
    console.print(table)


@app.command()
def dashboard(config: str | None = typer.Argument(None, help="Path to project YAML (optional)"),
              host: str = typer.Option("127.0.0.1"), port: int = typer.Option(8765),
              open_browser: bool = typer.Option(True)):
    """Launch the RCA web dashboard."""
    results_dir = Path("output")
    if config:
        cfg = _load(config)
        results_dir = Path(cfg.flow.output_dir)
    _run_dashboard(cfg=None, host=host, port=port, open_browser=open_browser, results_dir=results_dir)


def _run_dashboard(cfg=None, host="127.0.0.1", port=8765, open_browser=True, results_dir=None):
    rd = results_dir or (Path(cfg.flow.output_dir) if cfg else Path("output"))
    rd.mkdir(parents=True, exist_ok=True)
    app = create_app(rd)
    url = f"http://{host}:{port}"
    console.print(f"[green]RCA dashboard running at {url}[/green] (results_dir={rd})")
    if open_browser:
        try:
            webbrowser.open(url)
        except Exception:
            log.debug("Could not open dashboard browser", exc_info=True)
    uvicorn.run(app, host=host, port=port, log_level="warning")


@app.command(name="import")
def import_sdc(sdc: str = typer.Argument(..., help="Path to SDC file to import"),
               config: str | None = typer.Option(None, "--config", "-c", help="Project config (enables design-aware resolution)"),
               verbose: bool = typer.Option(False, "--verbose", "-v")):
    """Import an existing SDC file into the UCM and print a summary."""
    configure_logging(level="WARNING" if not verbose else "INFO")
    design, tg = None, None
    if config:
        cfg = _load(config)
        design, _ = _do_parse(cfg)
        tg = _do_timing(cfg, design)
    importer = SdcImporter(design=design, tg=tg)
    res = importer.from_file(sdc)
    counts = res.counts()
    console.print(Panel(f"[bold cyan]SDC IMPORT[/bold cyan] — {Path(sdc)}"))
    console.print(f"  Commands:         {counts['total']}")
    console.print(f"  Fully resolved:   [green]{counts['complete']}[/green]")
    console.print(f"  Partially resolved: [yellow]{counts['partial']}[/yellow]")
    console.print(f"  Unresolved:       [yellow]{counts['unresolved']}[/yellow]")
    console.print(f"  Errors:           [{'red' if counts['error'] else 'green'}]{counts['error']}[/]")
    console.print(f"  UCM constraints:  {counts['constraints']}")
    # Diagnostics summary
    diags = [d for d in res.diagnostics if d.severity.value in ("ERROR", "WARNING", "SECURITY")]
    if diags and (verbose or any(d.severity.value in ("ERROR", "SECURITY") for d in diags)):
        t = Table(title="Issues")
        t.add_column("Line"); t.add_column("Severity"); t.add_column("Code"); t.add_column("Message")
        for d in diags[:50]:
            t.add_row(str(d.line), d.severity.value, d.code or "-", d.message[:120])
        console.print(t)
    if verbose:
        t = Table(title="Imported commands")
        t.add_column("Line"); t.add_column("Command"); t.add_column("Status"); t.add_column("Constraints")
        for ic in res.imports:
            t.add_row(str(ic.source_line_start), ic.command_name,
                      ic.import_status.value, ", ".join(ic.constraint_ids) or "-")
        console.print(t)


@app.command()
def version():
    """Print RCA version."""
    console.print(f"RCA v{__version__}")


@app.command()
def doctor(
    config: str | None = typer.Argument(None, help="Optional project YAML for collateral checks"),
    backend: str = typer.Option("yosys_opensta", "--backend", help="EDA boundary to preflight"),
    json_out: bool = typer.Option(False, "--json", help="Emit machine-readable preflight evidence"),
):
    """Run bounded, non-executing real-EDA and formal readiness checks.

    This command discovers executables and checks configured collateral only.
    It never runs synthesis, STA, or formal proof jobs, and never falls back to
    mock.  ``run-sta --backend mock`` remains an explicit separate choice.
    """
    configure_logging(level="WARNING" if json_out else "INFO")
    cfg = None
    configuration_error = ""
    if config:
        try:
            cfg = _load(config)
        except (OSError, ValueError) as exc:
            configuration_error = f"{type(exc).__name__}: {exc}"

    selected_backend = backend
    yinfo = YosysBackend().discover()
    oinfo = OpenSTABackend().discover()
    # Use configured source paths directly here: the normal resolver omits
    # missing files for parser convenience, while doctor must report each
    # configured missing collateral path in its typed preflight.
    sources = [Path(item) for item in cfg.sources.files] if cfg else []
    include_dirs = [Path(item) for item in cfg.sources.include_dirs] if cfg else []
    liberty = [Path(item) for item in (cfg.flow.liberty_files() if cfg else [])]
    output = Path(cfg.flow.output_dir) if cfg else Path.cwd()
    # There is no generated SDC before a flow request. The doctor reports the
    # expected fresh location as a non-required observation rather than
    # pretending that a generated SDC exists.
    expected_sdc = output / "generated.sdc"
    config_identity = (
        cfg.model_dump(mode="json", exclude={"config_path", "project_root"}) if cfg else {}
    )
    preflight = preflight_yosys_opensta(
        backend=selected_backend, top=cfg.top_module() if cfg else "", sources=sources,
        liberty=liberty, sdc_path=None, output_dir=output,
        expected_outputs=[output / "top_synth.v"],
        yosys_info=yinfo, opensta_info=oinfo,
        liberty_hashes={str(path): hash_file(path) for path in liberty if path.is_file()},
        config_hash=stable_hash({"config": config_identity, "backend": selected_backend}),
        include_dirs=include_dirs,
    )
    # A doctor does not emit SDC, so make that expected future input explicit
    # but non-blocking in the rendered result.
    checks = [check.to_dict() for check in preflight.checks]
    for check in checks:
        if check["component"] == "generated_sdc":
            check.update({"required": False, "ready": True, "status": "not_required",
                          "classification": "available",
                          "detail": "generated during a real flow before execution",
                          "path": str(expected_sdc)})
    preflight_data = preflight.to_dict()
    preflight_data["checks"] = checks
    preflight_data["ready"] = not configuration_error and all(item["ready"] for item in checks)
    preflight_data["overall_status"] = "environment_ready" if preflight_data["ready"] else "configuration_invalid"
    preflight_data["failure_classification"] = (
        "configuration_invalid" if configuration_error
        else (None if preflight_data["ready"] else preflight_data["failure_classification"])
    )

    formal_executable = ""
    formal_version = None
    proof_paths: list[Path] = []
    if cfg and cfg.formal.backend == "symbiyosys":
        formal = SymbiYosysFormalBackend(
            executable=cfg.formal.symbiyosys_executable,
            work_dir=Path(cfg.formal.work_dir), timeout_seconds=cfg.formal.timeout_seconds,
        )
        formal_executable = formal.executable
        formal_version = formal.get_version()
        proof_paths = [Path(proof.sby_file) for proof in cfg.formal.proofs]
    else:
        formal = SymbiYosysFormalBackend(work_dir=output)
        formal_executable = formal.executable
        formal_version = formal.get_version()
    formal_preflight = preflight_symbiyosys(
        executable=formal_executable, version=formal_version, proofs=proof_paths,
        sources=sources, config_hash=preflight_data["environment_fingerprint"],
        required=bool(cfg and cfg.formal.backend == "symbiyosys"),
    )
    result = {
        "kind": "rca_doctor",
        "rca_version": __version__,
        "python": sys.version.split()[0],
        "configuration": {"path": str(Path(config).resolve()) if config else None,
                          "configured_backend": cfg.flow.backend if cfg else None,
                          "error": configuration_error or None},
        "preflight": preflight_data,
        "formal_preflight": formal_preflight.to_dict(),
        "mock_policy": "mock is available only when explicitly selected",
        "signoff": "not claimed",
    }
    if json_out:
        typer.echo(json.dumps(result, indent=2, sort_keys=True, default=str))
        return

    console.print(Panel("[cyan]RCA REAL-EDA PREFLIGHT[/cyan] — no tools executed"))
    console.print(f"  RCA: {result['rca_version']}  Python: {result['python']}")
    if configuration_error:
        console.print(f"  Configuration: [red]INVALID[/red] {configuration_error}")
    else:
        configured = result["configuration"]["configured_backend"]
        suffix = f" (project SDC backend: {configured})" if configured else ""
        console.print(f"  Backend: {selected_backend}{suffix}  Overall: "
                      f"[{'green' if preflight_data['ready'] else 'yellow'}]"
                      f"{preflight_data['overall_status']}[/]")
    table = Table(title="Yosys/OpenSTA checks")
    table.add_column("Component"); table.add_column("Status"); table.add_column("Detail")
    for check in checks:
        table.add_row(check["component"], check["status"], check["detail"])
    console.print(table)
    formal_table = Table(title="SymbiYosys checks")
    formal_table.add_column("Component"); formal_table.add_column("Status"); formal_table.add_column("Detail")
    for check in formal_preflight.to_dict()["checks"]:
        formal_table.add_row(check["component"], check["status"], check["detail"])
    console.print(formal_table)
    console.print("[dim]Mock is not selected automatically. Tool discovery is not a successful EDA run or signoff.[/dim]")


_DEFAULT_RTL = """// Auto-generated by `rca init`
module {top} #(
    parameter WIDTH = 8
)(
    input  logic             clk,
    input  logic             rst_n,
    input  logic             en,
    output logic [WIDTH-1:0] q
);
always_ff @(posedge clk or negedge rst_n) begin
    if (!rst_n)
        q <= '0;
    else if (en)
        q <= q + 1'b1;
end
endmodule
"""


def main():  # entry point
    app()


if __name__ == "__main__":
    main()
