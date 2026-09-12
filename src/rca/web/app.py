"""
FastAPI web dashboard (Enhancement: visualisation of constraints,
coverage, Pareto fronts, and timing results).
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse

from ..utils.logging import get_logger

log = get_logger("web")


def create_app(results_dir: str | Path | None = None) -> FastAPI:
    app = FastAPI(title="RCA Dashboard", version="0.1.0")
    results_dir = Path(results_dir) if results_dir else Path("output")

    @app.get("/", response_class=HTMLResponse)
    def index() -> str:
        return _DASHBOARD_HTML

    @app.get("/api/status")
    def status() -> dict[str, Any]:
        return {"status": "ok", "results_dir": str(results_dir)}

    @app.get("/api/design")
    def design() -> dict[str, Any]:
        p = results_dir / "design_model.json"
        if not p.is_file():
            raise HTTPException(404, "No design_model.json found; run `rca analyze` first.")
        return json.loads(p.read_text(encoding="utf-8"))

    @app.get("/api/constraints")
    def constraints() -> dict[str, Any]:
        p = results_dir / "constraint_model.json"
        if not p.is_file():
            raise HTTPException(404, "No constraint_model.json; run `rca generate` first.")
        return json.loads(p.read_text(encoding="utf-8"))

    @app.get("/api/validation")
    def validation() -> dict[str, Any]:
        p = results_dir / "validation_report.json"
        if not p.is_file():
            raise HTTPException(404, "No validation_report.json; run `rca validate` first.")
        return json.loads(p.read_text(encoding="utf-8"))

    @app.get("/api/coverage")
    def coverage() -> dict[str, Any]:
        p = results_dir / "coverage_report.json"
        if not p.is_file():
            raise HTTPException(404, "No coverage_report.json; run `rca coverage` first.")
        return json.loads(p.read_text(encoding="utf-8"))

    @app.get("/api/optimization")
    def optimization() -> dict[str, Any]:
        p = results_dir / "optimizer_state.json"
        if not p.is_file():
            raise HTTPException(404, "No optimizer_state.json; run `rca optimize` first.")
        return json.loads(p.read_text(encoding="utf-8"))

    @app.get("/api/workflow")
    def workflow() -> dict[str, Any]:
        p = results_dir / "workflow_report.json"
        if not p.is_file():
            raise HTTPException(404, "No workflow_report.json found; use `rca run --report workflow_report.json`.")
        return _read_dashboard_json(p, "workflow report")

    @app.get("/api/replay-evidence")
    def replay_evidence() -> dict[str, Any]:
        p = results_dir / "replay_evidence.json"
        if not p.is_file():
            raise HTTPException(404, "No replay_evidence.json found; use `rca replay-evidence --report replay_evidence.json`.")
        return _read_dashboard_json(p, "replay evidence")

    @app.get("/api/sdc")
    def sdc() -> dict[str, Any]:
        for name in ("design.sdc", "design.generic.sdc"):
            p = results_dir / name
            if p.is_file():
                return {"file": name, "content": p.read_text(encoding="utf-8")}
        raise HTTPException(404, "No design.sdc found; run `rca generate` first.")

    return app


def _read_dashboard_json(path: Path, label: str) -> dict[str, Any]:
    """Read a user-created presentation artifact without evaluating its content."""
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise HTTPException(422, f"Stored {label} is not valid JSON: {type(exc).__name__}") from exc
    if not isinstance(data, dict):
        raise HTTPException(422, f"Stored {label} must be a JSON object.")
    return data


_DASHBOARD_HTML = """
<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8"/>
<title>RCA Dashboard</title>
<style>
  body { font-family: system-ui, sans-serif; margin: 2em; background:#0e1116; color:#e6edf3; }
  h1 { color:#79c0ff; }
  h2 { color:#d2a8ff; margin-top: 2em; border-bottom:1px solid #30363d; padding-bottom:4px;}
  .card { background:#161b22; border:1px solid #30363d; padding:1em; border-radius:6px; margin:1em 0; }
  .grid { display:grid; grid-template-columns: repeat(auto-fit, minmax(200px,1fr)); gap:1em;}
  .metric { background:#161b22; border:1px solid #30363d; padding:0.8em; border-radius:6px;}
  .metric .v { font-size:1.8em; font-weight:bold; color:#7ee787;}
  .metric .l { font-size:0.85em; color:#8b949e;}
  table { width:100%; border-collapse: collapse; font-size:0.9em;}
  th,td { padding:6px 10px; text-align:left; border-bottom:1px solid #21262d;}
  th { background:#161b22; color:#79c0ff;}
  pre { background:#0d1117; padding:1em; border-radius:6px; overflow:auto; max-height:400px;}
  .ok { color:#7ee787; } .warn { color:#d29922; } .err { color:#f85149; }
  button { background:#238636; color:#fff; border:0; padding:8px 14px; border-radius:6px; cursor:pointer;}
</style>
</head>
<body>
<h1>RTL Constraint Assistant Dashboard</h1>
<p><button onclick="refresh()">Refresh</button></p>
<div id="content"><p>Loading…</p></div>
<script>
async function get(url){ const r = await fetch(url); if(!r.ok) return null; return r.json(); }
async function refresh() {
  const root = document.getElementById('content');
  root.innerHTML = '<p>Loading…</p>';
  const [design, constraints, val, cov, opt, workflow, replay] = await Promise.all([
    get('/api/design'), get('/api/constraints'), get('/api/validation'),
    get('/api/coverage'), get('/api/optimization'), get('/api/workflow'), get('/api/replay-evidence')
  ]);
  const esc = value => String(value ?? '').replace(/&/g, '&amp;').replace(/</g, '&lt;')
    .replace(/>/g, '&gt;').replace(/"/g, '&quot;').replace(/'/g, '&#039;');
  const json = value => esc(JSON.stringify(value));
  let html = '';
  if(design) {
    const s = design;
    html += '<h2>Design</h2><div class="grid">';
    html += `<div class="metric"><div class="v">${esc(s.top||'-')}</div><div class="l">top module</div></div>`;
    html += `<div class="metric"><div class="v">${(s.modules&&Object.keys(s.modules).length)||0}</div><div class="l">modules</div></div>`;
    html += `<div class="metric"><div class="v">${Object.keys(s.registers||{}).length}</div><div class="l">registers</div></div>`;
    html += `<div class="metric"><div class="v">${s.clock_candidates?.length||0}</div><div class="l">clock candidates</div></div>`;
    html += `<div class="metric"><div class="v">${s.reset_candidates?.length||0}</div><div class="l">reset candidates</div></div>`;
    html += '</div>';
  }
  if(cov) {
    html += '<h2>Coverage</h2><div class="grid">';
    for(const [k,v] of Object.entries(cov)){
      if(typeof v === 'number'){
        html += `<div class="metric"><div class="v">${v.toFixed? v.toFixed(1):v}%</div><div class="l">${k}</div></div>`;
      }
    }
    html += '</div>';
    if(cov.uncovered && cov.uncovered.length) {
      html += '<div class="card"><strong>Uncovered:</strong><ul>';
      for(const u of cov.uncovered) html += `<li>${esc(u.category)}: ${esc(u.message)}</li>`;
      html += '</ul></div>';
    }
  }
  if(constraints) {
    html += '<h2>Constraints</h2>';
    const cs = constraints.constraints || constraints;
    const entries = Object.entries(cs);
    html += `<p>${esc(entries.length)} constraints in model.</p>`;
    html += '<table><tr><th>ID</th><th>Type</th><th>Status</th><th>Source</th><th>Values</th></tr>';
    for(const [id,c] of entries.slice(0,200)){
      const status = c.status || '?';
      const cls = status==='FIXED'||status==='CONFIRMED' ? 'ok' :
                  status==='PROPOSED' ? 'warn' : 'err';
      html += `<tr><td>${esc(id)}</td><td>${esc(c.type)}</td><td class="${cls}">${esc(status)}</td><td>${esc(c.source||c.source_kind||'')}</td><td><code>${json(c.values||{})}</code></td></tr>`;
    }
    html += '</table>';
  }
  if(val) {
    html += '<h2>Validation</h2>';
    const sum = val.summary || {};
    html += `<div class="grid"><div class="metric"><div class="v ${sum.errors?'err':'ok'}">${sum.errors||0}</div><div class="l">errors</div></div>`;
    html += `<div class="metric"><div class="v warn">${sum.warnings||0}</div><div class="l">warnings</div></div></div>`;
    if(sum.issues) {
      html += '<table><tr><th>Severity</th><th>Code</th><th>Message</th></tr>';
      for(const i of sum.issues){
        const cls = i.severity==='ERROR'||i.severity==='CRITICAL' ? 'err' :
                    i.severity==='WARNING' ? 'warn' : '';
        html += `<tr><td class="${cls}">${esc(i.severity)}</td><td>${esc(i.code)}</td><td>${esc(i.message)}</td></tr>`;
      }
      html += '</table>';
    }
  }
  if(opt) {
    html += '<h2>Optimization</h2>';
    html += `<div class="card">Stop reason: <strong>${esc(opt.stop_reason||'-')}</strong>, `;
    html += `iterations=${esc(opt.iterations||0)}, eda_runs=${esc(opt.eda_runs||0)}, elapsed=${esc((opt.elapsed_s||0).toFixed(1))}s</div>`;
    if(opt.final) {
      const q = opt.final.qor||{};
      html += '<h3>Final candidate</h3>';
      html += `<table><tr><th>Setup WNS</th><th>Hold WNS</th><th>Area</th><th>Power</th><th>ID</th></tr>`;
      html += `<tr><td>${esc(q.setup_wns_ns??'-')} ns</td><td>${esc(q.hold_wns_ns??'-')} ns</td><td>${esc(q.area_total??'-')}</td><td>${esc(q.power_total??'-')}</td><td>${esc(opt.final.id)}</td></tr></table>`;
    }
  }
  if(workflow) {
    html += '<h2>Governed workflow</h2><div class="card">';
    html += `<strong>${esc(workflow.id)}</strong> — next action: <strong>${esc(workflow.summary?.next_action||'UNKNOWN')}</strong>`;
    html += '<table><tr><th>Stage</th><th>Status</th><th>Message</th></tr>';
    for(const stage of workflow.stages||[]) html += `<tr><td>${esc(stage.name)}</td><td>${esc(stage.status)}</td><td>${esc(stage.message)}</td></tr>`;
    html += '</table></div>';
  }
  if(replay) {
    html += '<h2>Replay evidence</h2><div class="card">';
    html += `Readiness: <strong>${esc(replay.replay_readiness)}</strong>; automatic replay: <strong>${esc(replay.automatic_replay_supported)}</strong>`;
    html += '<table><tr><th>Component</th><th>Status</th><th>Identity</th></tr>';
    for(const item of replay.components||[]) html += `<tr><td>${esc(item.name)}</td><td>${esc(item.status)}</td><td><code>${esc(item.identity||'-')}</code></td></tr>`;
    html += '</table></div>';
  }
  root.innerHTML = html || '<p>Run RCA commands to populate results.</p>';
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
