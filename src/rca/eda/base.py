"""
Tool backend interface (Step 10 — WP-M).

Every backend must:
- be discoverable (executable resolution from explicit config, env var,
  PATH, or project-local tools directory) without mutating global state;
- expose `get_version()` via a safe subprocess invocation that uses
  argument lists (no shell=True), captures stdout/stderr/rc, and times out;
- record the exact argv used in the RunManifest;
- return structured BLOCKED results when prerequisites (executable,
  Liberty) are missing instead of fabricating output.
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..utils.enums import RunStatus
from ..utils.logging import get_logger

log = get_logger("eda.base")


@dataclass
class ToolInfo:
    vendor: str
    tool: str
    version: str
    executable: str = ""
    host: str = field(default_factory=platform.node)
    platform: str = field(default_factory=platform.platform)
    available: bool = False
    capabilities: dict[str, bool] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor, "tool": self.tool, "version": self.version,
            "executable": self.executable, "host": self.host,
            "platform": self.platform, "available": self.available,
            "capabilities": dict(self.capabilities), "error": self.error,
        }


@dataclass
class CommandRecord:
    """Exact argv + metadata of one subprocess invocation, captured for
    the run manifest (Step 10 §3, §16)."""
    argv: list[str]
    cwd: str
    env: dict[str, str]
    timeout_seconds: int
    returncode: int | None = None
    stdout_tail: str = ""
    stderr_tail: str = ""
    duration_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv), "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "returncode": self.returncode,
            "stdout_tail": self.stdout_tail,
            "stderr_tail": self.stderr_tail,
            "duration_seconds": self.duration_seconds,
        }


class ToolBackend(ABC):
    name: str = "base"
    default_binary_name: str = ""
    env_var: str = ""

    def __init__(self, executable: str | None = None,
                 project_local_dirs: list[Path] | None = None) -> None:
        self.executable = executable or self._resolve_executable(project_local_dirs or [])
        self._info: ToolInfo | None = None

    # -------------- discovery --------------

    def _resolve_executable(self, project_local_dirs: list[Path]) -> str:
        """Deterministic discovery: explicit > env var > shutil.which >
        project-local dirs. Never modifies PATH."""
        # 1) env var
        if self.env_var:
            env_path = os.environ.get(self.env_var)
            if env_path and Path(env_path).is_file() and os.access(env_path, os.X_OK):
                return env_path
            if env_path and shutil.which(env_path):
                return shutil.which(env_path) or env_path
        # 2) PATH lookup (default binary name)
        if self.default_binary_name:
            found = shutil.which(self.default_binary_name)
            if found:
                return found
        # 3) project-local tools dirs
        for d in project_local_dirs:
            cand = Path(d) / self.default_binary_name
            if cand.is_file() and os.access(cand, os.X_OK):
                return str(cand)
        return self.default_binary_name  # may not exist; discover() reports it

    @abstractmethod
    def discover(self) -> ToolInfo: ...

    def _safe_run(self, argv: list[str], *, cwd: Path,
                  timeout: int = 60, env: dict[str, str] | None = None,
                  stdin: str | None = None) -> CommandRecord:
        """Argument-list subprocess invocation; NO shell=True. Captures
        rc, stdout, stderr, duration."""
        import time
        run_env = dict(os.environ)
        if env:
            run_env.update(env)
        cwd.mkdir(parents=True, exist_ok=True)
        t0 = time.time()
        try:
            proc = subprocess.run(
                argv, cwd=str(cwd), env=run_env,
                input=stdin, capture_output=True, text=True,
                timeout=timeout, shell=False, check=False,
            )
            rc = proc.returncode
            out = proc.stdout
            err = proc.stderr
        except subprocess.TimeoutExpired as e:
            rc = -1
            out = (e.stdout or b"").decode("utf-8", errors="replace") if isinstance(e.stdout, (bytes, bytearray)) else (e.stdout or "")
            err = f"timeout after {timeout}s"
        except FileNotFoundError as e:
            rc = 127
            out = ""
            err = str(e)
        duration = time.time() - t0
        return CommandRecord(
            argv=list(argv), cwd=str(cwd), env=run_env,
            timeout_seconds=timeout, returncode=rc,
            stdout_tail=out[-2000:] if out else "",
            stderr_tail=err[-2000:] if err else "",
            duration_seconds=duration,
        )

    # -------------- synthesis / STA --------------

    def synthesize(self, sources: list[Path], top: str, liberty: list[Path],
                   work_dir: Path, sdc_out: Path | None = None,
                   extra_args: dict[str, Any] | None = None) -> Path:
        raise NotImplementedError(f"{self.name} does not support synthesis.")

    @abstractmethod
    def run_sta(self, netlist: Path, sdc: Path, liberty: list[Path],
                work_dir: Path, top: str, corner: str = "default",
                extra_args: dict[str, Any] | None = None): ...

    def parse_reports(self, work_dir: Path):
        raise NotImplementedError


def blocked_result(tool_name: str, reason: str, stage: str) -> dict[str, Any]:
    """Helper to construct a structured BLOCKED result."""
    return {
        "status": RunStatus.BLOCKED.value,
        "tool": tool_name, "stage": stage, "reason": reason,
    }
