#!/usr/bin/env python3
"""Report deterministic RCA setup readiness without installing or changing anything.

Exit status is non-zero only when a required Python/runtime condition is
missing. EDA tools are optional: mock-only RCA operation remains supported.
"""

from __future__ import annotations

import argparse
import importlib.metadata
import importlib.util
import json
import shutil
import subprocess
import sys
from typing import Any

REQUIRED_PYTHON = (3, 10)
REQUIRED_PACKAGES = (
    "pydantic", "typer", "rich", "PyYAML", "jsonschema", "pyslang", "networkx",
    "numpy", "jinja2", "fastapi", "uvicorn",
)
OPTIONAL_TOOLS = (
    ("Yosys", "RCA_YOSYS", "yosys", ["-V"]),
    ("OpenSTA", "RCA_OPENSTA", "sta", ["-version"]),
    ("SymbiYosys", "RCA_SYMBIYOSYS", "sby", ["--version"]),
)


def _package(name: str) -> dict[str, Any]:
    spec = importlib.util.find_spec(name.lower().replace("pyyaml", "yaml"))
    try:
        version = importlib.metadata.version(name)
    except importlib.metadata.PackageNotFoundError:
        version = None
    return {"name": name, "required": True, "status": "AVAILABLE" if spec and version else "MISSING", "version": version}


def _tool(name: str, env_var: str, default: str, version_args: list[str]) -> dict[str, Any]:
    import os

    requested = os.environ.get(env_var) or default
    executable = shutil.which(requested)
    record: dict[str, Any] = {
        "name": name, "required": False, "env_var": env_var, "requested": requested,
        "executable": executable or "", "status": "MISSING", "version": None,
    }
    if not executable:
        return record
    try:
        probe = subprocess.run([executable, *version_args], capture_output=True, text=True,
                               timeout=15, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        record["status"] = "MISSING"
        record["detail"] = f"version probe failed: {type(exc).__name__}: {exc}"
        return record
    output = ((probe.stdout or "") + "\n" + (probe.stderr or "")).strip()
    if probe.returncode == 0:
        record["status"] = "AVAILABLE"
        record["version"] = output.splitlines()[0][:200] if output else "version not reported"
    else:
        record["detail"] = f"version probe returned {probe.returncode}: {output[-200:]}"
    return record


def _report() -> dict[str, Any]:
    packages = [_package(package) for package in REQUIRED_PACKAGES]
    tools = [_tool(*tool) for tool in OPTIONAL_TOOLS]
    python_ok = sys.version_info >= REQUIRED_PYTHON
    return {
        "kind": "rca_setup_verification",
        "python": {
            "required": f">={REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}",
            "version": sys.version.split()[0],
            "status": "AVAILABLE" if python_ok else "MISSING",
        },
        "packages": packages,
        "optional_tools": tools,
        "mock_only_supported": True,
        "required_ready": python_ok and all(item["status"] == "AVAILABLE" for item in packages),
    }


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--json", action="store_true", help="emit machine-readable report")
    args = parser.parse_args()
    report = _report()
    if args.json:
        print(json.dumps(report, indent=2, sort_keys=True))
    else:
        print("RCA SETUP VERIFICATION (no installation or configuration changes)")
        print(f"REQUIRED Python {report['python']['required']}: {report['python']['status']} ({report['python']['version']})")
        print("REQUIRED Python packages:")
        for item in report["packages"]:
            print(f"  {item['status']:9} {item['name']} {item['version'] or ''}".rstrip())
        print("OPTIONAL EDA tools (missing tools do not block mock-only RCA):")
        for item in report["optional_tools"]:
            detail = item["version"] or item.get("detail", "")
            print(f"  {item['status']:9} {item['name']} {detail}".rstrip())
        print(f"Mock-only operation: AVAILABLE\nRequired environment ready: {report['required_ready']}")
    return 0 if report["required_ready"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
