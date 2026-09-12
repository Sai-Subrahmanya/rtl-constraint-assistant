"""Step 25 subprocess and failed-cache safety contracts."""

from __future__ import annotations

import os
import time

import pytest

from rca.artifacts import ArtifactManager, RunManifest
from rca.eda.base import ToolBackend, ToolInfo
from rca.eda.flow import _find_cached_run, _hash_artifacts
from rca.utils.enums import RunStatus


class _StubBackend(ToolBackend):
    """Minimal concrete tool host for exercising the base subprocess boundary."""

    def discover(self) -> ToolInfo:
        return ToolInfo(vendor="test", tool="stub", version="test")

    def run_sta(self, *args, **kwargs):  # pragma: no cover - never called by these tests
        raise NotImplementedError


def test_safe_argv_and_command_evidence_exclude_runtime_environment(tmp_path):
    fake = tmp_path / "echo-argv"
    fake.write_text(
        "#!/bin/sh\nprintf 'argc=%d' \"$#\"\nfor arg in \"$@\"; do printf ' [%s]' \"$arg\"; done\n",
        encoding="utf-8",
    )
    fake.chmod(0o755)
    record = _StubBackend()._safe_run(
        [str(fake), "hello; rm -rf /", "world", "--token=not-for-evidence"],
        cwd=tmp_path, timeout=5, env={"RCA_TEST_SECRET": "also-not-for-evidence"},
    )
    assert record.returncode == 0
    assert record.execution_status == "execution_completed"
    assert "argc=3" in record.stdout_tail
    assert "[hello; rm -rf /]" in record.stdout_tail
    assert "--token=<redacted>" in record.argv
    assert "env" not in record.to_dict()
    assert "not-for-evidence" not in str(record.to_dict())


def test_timeout_terminates_posix_process_group_descendants(tmp_path):
    """A timed-out wrapper cannot leave a child compiler/STA process running."""
    if os.name != "posix":
        pytest.skip("process-group contract is specific to POSIX")
    fake = tmp_path / "spawn-child"
    fake.write_text(
        """#!/usr/bin/env python3
import signal
import subprocess
import sys
import time
from pathlib import Path
child = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(30)'])
Path('child.pid').write_text(str(child.pid), encoding='utf-8')
def stop(_signum, _frame):
    try:
        child.wait(timeout=5)
    except subprocess.TimeoutExpired:
        child.kill()
        child.wait()
    raise SystemExit(0)
signal.signal(signal.SIGTERM, stop)
while True:
    time.sleep(1)
""",
        encoding="utf-8",
    )
    fake.chmod(0o755)

    record = _StubBackend()._safe_run([str(fake)], cwd=tmp_path, timeout=1)
    assert record.timed_out is True
    assert record.execution_status == "execution_timed_out"
    child_pid = int((tmp_path / "child.pid").read_text(encoding="utf-8"))
    for _ in range(20):
        try:
            os.kill(child_pid, 0)
        except ProcessLookupError:
            break
        time.sleep(0.05)
    else:
        pytest.fail(f"timed-out child process {child_pid} still exists")


def test_failed_execution_manifest_never_becomes_a_cache_hit(tmp_path):
    artifacts = ArtifactManager(tmp_path)
    failed_dir = artifacts.runs_dir / "failed"
    failed_dir.mkdir(parents=True)
    for name in ("generated.sdc", "top_synth.v", "qor.json"):
        (failed_dir / name).write_text("stale-or-partial evidence\n", encoding="utf-8")
    relative = {"sdc": "generated.sdc", "netlist": "top_synth.v", "qor": "qor.json"}
    absolute = {key: str(failed_dir / value) for key, value in relative.items()}
    manifest = RunManifest(
        candidate_id="baseline", tool="yosys_opensta", execution_status=RunStatus.STA_FAILED.value,
        artifacts=relative, artifact_hashes=_hash_artifacts(failed_dir, absolute),
        extra={"cache_key": "FAILED_CACHE_KEY", "diagnostics": []},
    )
    artifacts.write_manifest_to("failed", manifest)
    assert _find_cached_run(artifacts, "FAILED_CACHE_KEY") is None
