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

_SENSITIVE_ARGUMENT_NAMES = {"password", "passphrase", "secret", "token", "api-key", "apikey"}


def _redact_command_evidence(argv: list[str]) -> tuple[list[str], list[str]]:
    """Return an auditable argv with conventional sensitive option values masked.

    RCA's built-in EDA calls contain executable/script/input paths only, but
    this boundary is also safe for future explicit credential-like options.
    The unmodified argv is passed to the subprocess; only persisted command
    evidence and matching bounded output text are redacted.
    """
    redacted: list[str] = []
    secrets: list[str] = []
    redact_next = False
    for argument in argv:
        if redact_next:
            secrets.append(argument)
            redacted.append("<redacted>")
            redact_next = False
            continue
        name, separator, value = argument.partition("=")
        normalized = name.lstrip("-").replace("_", "-").lower()
        if normalized in _SENSITIVE_ARGUMENT_NAMES:
            if separator:
                if value:
                    secrets.append(value)
                redacted.append(f"{name}=<redacted>")
            else:
                redacted.append(argument)
                redact_next = True
            continue
        redacted.append(argument)
    return redacted, secrets


@dataclass
class ToolInfo:
    vendor: str
    tool: str
    version: str
    executable: str = ""
    platform: str = field(default_factory=platform.platform)
    available: bool = False
    capabilities: dict[str, bool] = field(default_factory=dict)
    error: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "vendor": self.vendor, "tool": self.tool, "version": self.version,
            "executable": self.executable, "platform": self.platform,
            "available": self.available,
            "capabilities": dict(self.capabilities), "error": self.error,
        }


@dataclass
class CommandRecord:
    """Exact argv + bounded metadata of one subprocess invocation.

    The runtime environment is deliberately *not* retained.  It may contain
    credentials and is neither reproducibility evidence nor safe diagnostic
    data.  ``execution_status`` uses the terminal Step-25 lifecycle vocabulary.
    """
    argv: list[str]
    cwd: str
    timeout_seconds: int
    returncode: int | None = None
    timed_out: bool = False
    execution_status: str = ""
    stdout_tail: str = ""
    stderr_tail: str = ""
    duration_seconds: float | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "argv": list(self.argv), "cwd": self.cwd,
            "timeout_seconds": self.timeout_seconds,
            "returncode": self.returncode,
            "timed_out": self.timed_out,
            "execution_status": self.execution_status,
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
        """Run an argv-only subprocess and retain bounded, non-secret evidence.

        A real tool is placed in a new POSIX process group.  On timeout RCA
        terminates that group before returning, so a timed-out wrapper cannot
        leave a child compiler/STA process behind.  No environment values are
        serialized by :meth:`CommandRecord.to_dict`.
        """
        import signal
        import time

        # Preserve tool-specific search/library variables supplied by the
        # operator, but pin locale/time-zone dependent diagnostics so parsing
        # and bounded provenance are reproducible where practical. The map is
        # deliberately transient and never retained in CommandRecord.
        run_env = dict(os.environ)
        if env:
            run_env.update(env)
        run_env.update({"LC_ALL": "C", "LANG": "C", "TZ": "UTC"})
        cwd.mkdir(parents=True, exist_ok=True)
        t0 = time.monotonic()
        timed_out = False
        try:
            proc = subprocess.Popen(
                argv, cwd=str(cwd), env=run_env, stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True,
                shell=False, start_new_session=(os.name == "posix"),
            )
            try:
                out, err = proc.communicate(input=stdin, timeout=timeout)
                rc = proc.returncode
            except subprocess.TimeoutExpired as exc:
                timed_out = True
                # Communicate may hold partial bytes/text.  Terminate the
                # complete process group where available, then drain pipes.
                if os.name == "posix":
                    try:
                        os.killpg(proc.pid, signal.SIGTERM)
                    except ProcessLookupError:
                        pass
                else:  # pragma: no cover - Windows process-group behaviour
                    proc.kill()
                try:
                    out, err = proc.communicate(timeout=5)
                except subprocess.TimeoutExpired:  # pragma: no cover - rare uncooperative child
                    if os.name == "posix":
                        try:
                            os.killpg(proc.pid, signal.SIGKILL)
                        except ProcessLookupError:
                            pass
                    else:
                        proc.kill()
                    out, err = proc.communicate()
                partial_out = exc.stdout or ""
                partial_err = exc.stderr or ""
                if isinstance(partial_out, (bytes, bytearray)):
                    partial_out = partial_out.decode("utf-8", errors="replace")
                if isinstance(partial_err, (bytes, bytearray)):
                    partial_err = partial_err.decode("utf-8", errors="replace")
                out = str(out or partial_out or "")
                err = f"timeout after {timeout}s\n{err or partial_err or ''}".rstrip()
                rc = -1
        except OSError as exc:
            rc = 127
            out = ""
            err = str(exc)
        duration = time.monotonic() - t0
        execution_status = (
            "execution_timed_out" if timed_out
            else "execution_completed" if rc == 0
            else "execution_failed"
        )
        recorded_argv, sensitive_values = _redact_command_evidence(list(argv))
        # Keep bounded diagnostics useful without serializing a value supplied
        # through a conventional credential-like command option.
        for sensitive_value in sensitive_values:
            if sensitive_value:
                out = out.replace(sensitive_value, "<redacted>")
                err = err.replace(sensitive_value, "<redacted>")
        return CommandRecord(
            argv=recorded_argv, cwd=str(cwd), timeout_seconds=timeout,
            returncode=rc, timed_out=timed_out, execution_status=execution_status,
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
