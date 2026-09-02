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

    @app.get("/api/sdc")
    def sdc() -> dict[str, Any]:
        for name in ("design.sdc", "design.generic.sdc"):
            p = results_dir / name
            if p.is_file():
                return {"file": name, "content": p.read_text(encoding="utf-8")}
        raise HTTPException(404, "No design.sdc found; run `rca generate` first.")

    return app


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
  const [design, constraints, val, cov, opt] = await Promise.all([
    get('/api/design'), get('/api/constraints'), get('/api/validation'),
    get('/api/coverage'), get('/api/optimization')
  ]);
  let html = '';
  if(design) {
    const s = design;
    html += '<h2>Design</h2><div class="grid">';
    html += `<div class="metric"><div class="v">${s.top||'-'}</div><div class="l">top module</div></div>`;
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
      for(const u of cov.uncovered) html += `<li>${u.category}: ${u.message}</li>`;
      html += '</ul></div>';
    }
  }
  if(constraints) {
    html += '<h2>Constraints</h2>';
    const cs = constraints.constraints || constraints;
    const entries = Object.entries(cs);
    html += `<p>${entries.length} constraints in model.</p>`;
    html += '<table><tr><th>ID</th><th>Type</th><th>Status</th><th>Source</th><th>Values</th></tr>';
    for(const [id,c] of entries.slice(0,200)){
      const status = c.status || '?';
      const cls = status==='FIXED'||status==='CONFIRMED' ? 'ok' :
                  status==='PROPOSED' ? 'warn' : 'err';
      html += `<tr><td>${id}</td><td>${c.type}</td><td class="${cls}">${status}</td><td>${c.source||c.source_kind||''}</td><td><code>${JSON.stringify(c.values||{})}</code></td></tr>`;
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
        html += `<tr><td class="${cls}">${i.severity}</td><td>${i.code}</td><td>${i.message}</td></tr>`;
      }
      html += '</table>';
    }
  }
  if(opt) {
    html += '<h2>Optimization</h2>';
    html += `<div class="card">Stop reason: <strong>${opt.stop_reason||'-'}</strong>, `;
    html += `iterations=${opt.iterations||0}, eda_runs=${opt.eda_runs||0}, elapsed=${(opt.elapsed_s||0).toFixed(1)}s</div>`;
    if(opt.final) {
      const q = opt.final.qor||{};
      html += '<h3>Final candidate</h3>';
      html += `<table><tr><th>Setup WNS</th><th>Hold WNS</th><th>Area</th><th>Power</th><th>ID</th></tr>`;
      html += `<tr><td>${q.setup_wns_ns??'-'} ns</td><td>${q.hold_wns_ns??'-'} ns</td><td>${q.area_total??'-'}</td><td>${q.power_total??'-'}</td><td>${opt.final.id}</td></tr></table>`;
    }
  }
  root.innerHTML = html || '<p>Run RCA commands to populate results.</p>';
}
refresh();
setInterval(refresh, 5000);
</script>
</body>
</html>
"""
