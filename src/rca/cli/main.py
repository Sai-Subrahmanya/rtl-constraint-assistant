"""
RCA Command-Line Interface (Manual §59).

Provides: rca init, analyze, infer, generate, validate, compare, coverage,
explain, run-sta, optimize, inspect, report, dashboard.
"""

from __future__ import annotations

import json
import sys
import webbrowser
from pathlib import Path
from typing import Optional

import typer
import uvicorn
from rich.console import Console
from rich.panel import Panel
from rich.table import Table

from .. import __version__
from ..artifacts import ArtifactManager, RunManifest
from ..config.model import ProjectConfig, default_config, load_config, write_config
from ..constraint_model import ConstraintSet
from ..design_model import Design
from ..eda import MockEDA, OpenSTABackend, YosysBackend, get_tool, run_flow
from ..equivalence import compare as compare_sets
from ..exceptions import analyze_exceptions, verify_exceptions
from ..explanation import design_report, explain_candidate, explain_constraint
from ..inference import InferenceEngine
from ..optimizer import Optimizer
from ..parser import SlangAdapter
from ..provenance import AssumptionLedger
from ..qor.model import QoRResult
from ..sdc import SDCParser, get_backend
from ..sdc_importer import SdcImporter
from ..scenarios import build_scenarios
from ..source.manifest import resolve_include_dirs, resolve_sources
from ..timing_model import TimingGraph
from ..utils import configure_logging, get_logger
from ..utils.enums import SafeMode
from ..validation import validate as run_validation
from ..web import create_app

app = typer.Typer(
    add_completion=False,
    no_args_is_help=True,
    help="RTL Constraint Assistant - RTL-aware timing constraint intelligence and optimization",
)
console = Console()
log = get_logger("cli")


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


def _am(cfg: ProjectConfig) -> ArtifactManager:
    out = Path(cfg.flow.output_dir)
    out.mkdir(parents=True, exist_ok=True)
    return ArtifactManager(out)


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
    console.print(f"\n[dim]Artifacts written to {am.output_dir}/[/dim]")


@app.command()
def infer(config: str = typer.Argument(..., help="Path to project YAML")):
    """Run the inference engine and report proposed constraints (no SDC written)."""
    configure_logging(level="INFO")
    cfg = _load(config)
    design, _ = _do_parse(cfg)
    tg = _do_timing(cfg, design)
    ledger = AssumptionLedger()
    cset, report = _do_inference(cfg, design, tg, ledger)
    console.print(Panel(f"[cyan]Inference report[/cyan] — {cfg.project.name}"))
    t = Table(title=f"Proposed constraints ({len(cset)})")
    for col in ("ID", "Type", "Targets", "Source", "Confidence", "Status"):
        t.add_column(col)
    for c in cset:
        t.add_row(c.id, c.type.value, ", ".join(c.target_objects[:3]),
                  c.source_kind.value, c.confidence.value, c.status.value)
    console.print(t)

    # Structured missing-information display.
    required = report.required_information()
    if required:
        mt = Table(title="Required information (blocking complete SDC generation)",
                   show_lines=False)
        mt.add_column("ID"); mt.add_column("Category"); mt.add_column("Object")
        mt.add_column("Message"); mt.add_column("Blocking")
        for i, mi in enumerate(required, 1):
            mt.add_row(mi.get("id", f"REQ-{i:03d}"),
                       mi.get("category", ""),
                       mi.get("object", ""),
                       mi.get("message", ""),
                       "YES" if mi.get("blocking") else "no")
        console.print(mt)
    if report.warnings:
        console.print(f"\n[yellow]Warnings: {len(report.warnings)}[/yellow]")
        for w in report.warnings[:20]:
            console.print(f"  - {w.get('message', w)}")
    if report.conflicts:
        console.print(f"\n[magenta]Conflicts (user vs inference): {len(report.conflicts)}[/magenta]")
        for c in report.conflicts[:10]:
            console.print(f"  - {c.get('message', c)}")


@app.command()
def generate(config: str = typer.Argument(..., help="Path to project YAML"),
             backend: str = typer.Option("generic", help="SDC backend: generic|opensta|synopsys|cadence"),
             output: str | None = typer.Option(None, help="Output SDC path (default: output/design.sdc)"),
             safe_mode: str = typer.Option("balanced", help="strict|balanced|aggressive"),
             provenance_comments: bool = typer.Option(True, "--provenance/--no-provenance")):
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
                                   with_provenance=provenance_comments)
    am = _am(cfg)
    out_path = Path(output) if output else am.path(f"design.{backend}.sdc")
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(result.text, encoding="utf-8")
    am.write_json("constraint_model.json", cset.snapshot())
    am.write_json("assumptions.json", ledger.to_list())

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

    color = "green" if result.status.value == "COMPLETE" else "yellow"
    console.print(Panel(f"[{color}]Generated {out_path}[/{color}]"))
    # Exit non-zero on BLOCKED/ERROR so CI scripts don't mistake it for success.
    status_str = result.status if isinstance(result.status, str) else result.status.value
    if status_str in ("BLOCKED", "ERROR"):
        raise typer.Exit(code=2)


@app.command()
def validate(config: str = typer.Argument(..., help="Path to project YAML"),
             sdc: Optional[str] = typer.Option(None, help="Path to existing SDC to validate"),
             backend: str = typer.Option("generic", help="SDC backend name")):
    """Validate generated (or imported) SDC against design model."""
    configure_logging(level="INFO")
    cfg = _load(config)
    design, _ = _do_parse(cfg)
    tg = _do_timing(cfg, design)
    ledger = AssumptionLedger()
    if sdc:
        parser = SDCParser()
        cset = parser.parse_file(sdc)
        if parser.warnings:
            for w in parser.warnings:
                console.print(f"[yellow]SDC parser: {w}[/yellow]")
    else:
        cset, _ = _do_inference(cfg, design, tg, ledger)
    result = run_validation(design, tg, cset, backend=backend)
    am = _am(cfg)
    am.write_json("validation_report.json", result.as_dict())
    if result.coverage:
        am.write_json("coverage_report.json", result.coverage.as_dict())
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
    result = run_validation(design, tg, cset)
    cov = result.coverage
    am = _am(cfg)
    am.write_json("coverage_report.json", cov.as_dict() if cov else {})
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
    cfg = _load(config)
    am = _am(cfg)
    try:
        parser = SDCParser()
        cset_a = parser.parse_file(a)
        cset_b = parser.parse_file(b)
    except Exception as exc:
        console.print(f"[red]Failed to parse SDC: {exc}[/red]")
        raise typer.Exit(code=2)
    result = compare_sets(cset_a, cset_b)
    if json_out:
        import json as _json
        console.print_json(data=result.to_dict())
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
                  f"Duplicates in B: {c['duplicates_right']}")
    if result.different_constraints:
        console.print("\n[bold]Top semantic differences:[/bold]")
        shown = 0
        for p in result.different_constraints:
            if shown >= 10:
                console.print(f"  … and {len(result.different_constraints)-shown} more")
                break
            ids = f"{p.a_id} → {p.b_id}" if p.a_id and p.b_id else (p.a_id or p.b_id or "")
            console.print(f"  - [{p.constraint_type}] {ids}")
            for fld in p.fields[:3]:
                console.print(f"      {fld.field}: A={fld.value_a!r}  B={fld.value_b!r}")
                console.print(f"        {fld.explanation}")
            shown += 1
    if result.only_in_left:
        console.print("\n[bold]Only in A:[/bold]")
        for p in result.only_in_left[:10]:
            console.print(f"  - [{p.constraint_type}] {p.a_id}")
    if result.only_in_right:
        console.print("\n[bold]Only in B:[/bold]")
        for p in result.only_in_right[:10]:
            console.print(f"  - [{p.constraint_type}] {p.b_id}")
    if result.unknown_constraints:
        console.print("\n[magenta][bold]UNKNOWN (cannot prove equivalence):[/bold][/magenta]")
        for p in result.unknown_constraints[:10]:
            console.print(f"  - [{p.constraint_type}] {p.a_id or '?'} vs {p.b_id or '?'}")
            for n in p.notes[:3]:
                console.print(f"      {n}")
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
        from ..constraint_model import ConstraintSet as _CS  # not used directly
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
        p = am.path("candidates.jsonl")
        console.print("[yellow]Candidate explanation requires the optimizer DB; load candidates.jsonl.[/yellow]")
        return
    console.print("Specify --constraint ID to explain a constraint.")


@app.command(name="run-sta")
def run_sta(config: str = typer.Argument(..., help="Path to project YAML"),
            backend: str = typer.Option("yosys_opensta", help="EDA flow: yosys_opensta|mock"),
            sdc: Optional[str] = typer.Option(None, help="SDC file to use (defaults to generated)"),
            force: bool = typer.Option(False, "--force", help="Bypass cache and rerun"),
            allow_partial_sdc: bool = typer.Option(False, "--allow-partial-sdc",
                                                  help="Exploratory: allow PARTIAL SDC to reach STA")):
    """Run synthesis + STA on the design and collect QoR (Step 10)."""
    configure_logging(level="INFO")
    cfg = _load(config)
    design, parse_diags = _do_parse(cfg)
    tg = _do_timing(cfg, design)
    ledger = AssumptionLedger()
    cset, inf_report = _do_inference(cfg, design, tg, ledger)
    sdc_backend = get_backend("opensta" if "opensta" in backend else "generic")
    am = _am(cfg)

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

    result = run_flow(
        cfg=cfg, cset=cset, sdc_text=sdc_text,
        sdc_generation_status=gen.status if isinstance(gen.status, str) else gen.status.value,
        sources=sources, include_dirs=include_dirs, defines=defines,
        output_dir=Path(cfg.flow.output_dir), backend=backend,
        candidate_id="baseline", allow_partial_sdc=allow_partial_sdc,
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
        def _ns(x): return f"{x*1e9:.3f} ns" if isinstance(x,(int,float)) else "n/a"
        console.print(f"  Setup WNS: {_ns(q.get('setup_wns_ns'))}   TNS: {_ns(q.get('setup_tns_ns'))}  violations={q.get('setup_violations')}")
        console.print(f"  Hold  WNS: {_ns(q.get('hold_wns_ns'))}   TNS: {_ns(q.get('hold_tns_ns'))}  violations={q.get('hold_violations')}")
        console.print("\n[bold]QoR[/bold]")
        area = q.get("area") if q.get("area") is not None else q.get("area_proxy")
        area_label = "area" if q.get("area") is not None else "area_proxy (cell_count)"
        console.print(f"  Cell count: {q.get('cell_count')}   FF: {q.get('ff_count')}")
        console.print(f"  {area_label}: {area}")
        console.print(f"  Power: {q.get('power_status','UNAVAILABLE')}")
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
    libs = [Path(p) for p in cfg.flow.liberty_files()]
    sources = resolve_sources(cfg)

    def evaluate(cand, work):
        cand_cset = cand.constraint_set or cset
        sdc_text = sdc_backend.render(cand_cset, design_name=cfg.project.name)
        sdc_p = work / f"{cand.id}.sdc"
        sdc_p.write_text(sdc_text, encoding="utf-8")
        if backend == "mock":
            tool = MockEDA()
            return tool.evaluate_candidate(cand, work)
        yosys = YosysBackend()
        netlist = yosys.synthesize(sources, cfg.top_module(), libs, work / cand.id)
        sta = OpenSTABackend()
        return sta.run_sta(netlist, sdc_p, libs, work / cand.id, cfg.top_module())

    opt = Optimizer(cfg, evaluate_fn=evaluate, work_dir=runs_dir)
    result = opt.run(cset)
    am.write_json("optimizer_state.json", result.to_dict())
    # Candidates JSONL
    cj_path = am.path("candidates.jsonl")
    with cj_path.open("w") as f:
        for c in result.all_candidates:
            f.write(json.dumps(c.to_dict(), default=str) + "\n")
    # Pareto
    am.write_json("pareto_frontier.json", [c.to_dict() for c in result.pareto])
    # Final SDC
    if result.final and result.final.constraint_set:
        final_sdc = sdc_backend.render(result.final.constraint_set, design_name=cfg.project.name)
        am.write_text("design.final.sdc", final_sdc)
    console.print(Panel("[cyan bold]Optimization complete[/cyan bold]"))
    console.print(f"  Stop reason: {result.stop_reason.value if result.stop_reason else 'n/a'}")
    console.print(f"  Iterations:  {result.iterations}  EDA runs: {result.eda_runs}  Elapsed: {result.elapsed_seconds:.1f}s")
    console.print(f"  Pareto size: {len(result.pareto)}")
    if result.final:
        q = result.final.qor
        console.print(f"\n[green]Final candidate: {result.final.id}[/green]")
        if q:
            console.print(f"  Setup WNS: {q.setup_wns*1e9 if q.setup_wns is not None else '-':.3f} ns")
            console.print(f"  Hold WNS:  {q.hold_wns*1e9 if q.hold_wns is not None else '-':.3f} ns")
            console.print(f"  Area:      {q.area_total}  Power: {q.power_total}")
    if dashboard:
        _run_dashboard(cfg=cfg)


@app.command()
def report(config: str = typer.Argument(..., help="Path to project YAML")):
    """Print a full human-readable report."""
    configure_logging(level="WARNING")
    cfg = _load(config)
    design, diag = _do_parse(cfg)
    tg = _do_timing(cfg, design)
    ledger = AssumptionLedger()
    cset, _ = _do_inference(cfg, design, tg, ledger)
    val_result = run_validation(design, tg, cset)
    missing = tg.missing_information()
    text = design_report(design.summary(), tg.summary(), val_result.as_dict(),
                         val_result.coverage.as_dict() if val_result.coverage else None,
                         cset, missing)
    am = _am(cfg)
    am.write_text("inference_report.txt", text)
    am.write_json("inference_report.json",
                  {"design": design.summary(), "timing": tg.summary(),
                   "validation": val_result.as_dict(),
                   "coverage": val_result.coverage.as_dict() if val_result.coverage else None,
                   "constraints": cset.snapshot()})
    sys.stdout.write(text)


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
def dashboard(config: Optional[str] = typer.Argument(None, help="Path to project YAML (optional)"),
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
            pass
    uvicorn.run(app, host=host, port=port, log_level="warning")


@app.command(name="import")
def import_sdc(sdc: str = typer.Argument(..., help="Path to SDC file to import"),
               config: Optional[str] = typer.Option(None, "--config", "-c", help="Project config (enables design-aware resolution)"),
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
def doctor(config: Optional[str] = typer.Argument(None, help="Optional project YAML for library/tool config")):
    """Report environment / tool availability (Step 10 §32)."""
    import platform as _platform
    import shutil as _shutil
    console.print(Panel("[cyan]RCA DIAGNOSTICS[/cyan]"))
    t = Table(); t.add_column("Item"); t.add_column("Value")
    t.add_row("Python", sys.version.split()[0])
    t.add_row("RCA version", __version__)
    t.add_row("Platform", _platform.platform())
    # pyslang availability
    try:
        import pyslang  # type: ignore
        t.add_row("pyslang", f"available ({getattr(pyslang, '__version__', 'unknown')})")
    except Exception:
        t.add_row("pyslang", "[yellow]unavailable[/yellow]")
    # Yosys
    try:
        y = YosysBackend()
        yi = y.discover()
        avail = f"[green]available[/green]" if yi.available else "[yellow]unavailable[/yellow]"
        t.add_row("yosys", f"{avail}  {yi.executable}  {yi.version}")
    except Exception as e:
        t.add_row("yosys", f"[red]error: {e}[/red]")
    # OpenSTA
    try:
        o = OpenSTABackend()
        oi = o.discover()
        avail = f"[green]available[/green]" if oi.available else "[yellow]unavailable[/yellow]"
        t.add_row("opensta", f"{avail}  {oi.executable}  {oi.version}")
    except Exception as e:
        t.add_row("opensta", f"[red]error: {e}[/red]")
    # Liberty config
    if config:
        try:
            cfg = _load(config)
            libs = cfg.flow.liberty_files() or []
            t.add_row("Liberty files", "; ".join(libs) if libs else "[yellow]none configured[/yellow]")
            t.add_row("Project top", cfg.top_module())
            t.add_row("Output dir", cfg.flow.output_dir)
        except Exception as e:
            t.add_row("config", f"[red]failed to load: {e}[/red]")
    else:
        t.add_row("Liberty files", "[dim](pass project.yaml to see)[/dim]")
    console.print(t)


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
