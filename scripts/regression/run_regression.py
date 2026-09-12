#!/usr/bin/env python3
"""Run RCA validation gates in a documented order and retain pytest failures.

This is a thin orchestrator over the repository's ordinary pytest commands; it
does not replace pytest, hide output, retry failures, or turn unavailable
optional EDA tools into passes.
"""

from __future__ import annotations

import argparse
import re
import subprocess
import sys
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
CORE_TESTS = ["tests/unit"]
SUITES = (("core", CORE_TESTS), ("golden", ["tests/golden"]),
          ("integration", ["tests/integration"]), ("stress", ["tests/stress"]))
_COLLECTED = re.compile(r"collected\s+(\d+)\s+items")
_COUNT = re.compile(r"(\d+)\s+(passed|failed|skipped|errors?)\b")
_ELAPSED = re.compile(r"\bin\s+([\d.]+)s\b")


@dataclass
class SuiteResult:
    name: str
    collected: int = 0
    passed: int = 0
    failed: int = 0
    skipped: int = 0
    errors: int = 0
    elapsed_seconds: float = 0.0
    exit_code: int = 0


def _parse(name: str, output: str, elapsed: float, exit_code: int) -> SuiteResult:
    result = SuiteResult(name=name, elapsed_seconds=elapsed, exit_code=exit_code)
    collected = _COLLECTED.findall(output)
    if collected:
        result.collected = int(collected[-1])
    # Pytest places failures before passes ("1 failed, 482 passed") but prints
    # passes first on green runs. Parse the final result line by named counter,
    # never by positional ordering, so a failing summary remains informative.
    summary_line = next(
        (
            line for line in reversed(output.splitlines())
            if _ELAPSED.search(line) and _COUNT.search(line)
        ),
        "",
    )
    counters = {name.rstrip("s"): int(value) for value, name in _COUNT.findall(summary_line)}
    result.passed = counters.get("passed", 0)
    result.failed = counters.get("failed", 0)
    result.skipped = counters.get("skipped", 0)
    result.errors = counters.get("error", 0)
    if match := _ELAPSED.search(summary_line):
        result.elapsed_seconds = float(match.group(1))
    return result


def _run(name: str, targets: list[str], python: str) -> SuiteResult:
    command = [python, "-m", "pytest", "-q", *targets]
    print(f"\n== RCA regression suite: {name} ==\n$ {' '.join(command)}", flush=True)
    started = time.monotonic()
    process = subprocess.run(command, cwd=ROOT, text=True, stdout=subprocess.PIPE,
                             stderr=subprocess.STDOUT, check=False)
    output = process.stdout or ""
    print(output, end="" if output.endswith("\n") else "\n", flush=True)
    return _parse(name, output, time.monotonic() - started, process.returncode)


def _print_summary(results: list[SuiteResult]) -> None:
    print("\n== RCA regression summary ==")
    print("suite          collected  passed  failed  skipped  errors  elapsed(s)  exit")
    for result in results:
        print(f"{result.name:14} {result.collected:9} {result.passed:7} {result.failed:7} "
              f"{result.skipped:8} {result.errors:7} {result.elapsed_seconds:10.2f} {result.exit_code:5}")
    print("Failures are never suppressed; a non-zero suite exit makes this runner non-zero.")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--python", default=sys.executable, help="Python interpreter used for pytest")
    parser.add_argument("--suite", action="append", choices=[name for name, _ in SUITES],
                        help="run only one named suite (repeatable)")
    parser.add_argument("--real-eda", action="store_true",
                        help="add explicitly opt-in optional-real-EDA pytest marker suite")
    parser.add_argument("--list", action="store_true", help="list suite targets and exit")
    args = parser.parse_args()
    selected = [(name, list(targets)) for name, targets in SUITES
                if not args.suite or name in args.suite]
    if args.real_eda:
        selected.append(("optional-real-eda", ["-m", "optional_real_eda", "tests/integration/test_optional_real_eda.py"]))
    if args.list:
        for name, targets in selected:
            print(f"{name}: {' '.join(targets)}")
        return 0
    results = [_run(name, targets, args.python) for name, targets in selected]
    _print_summary(results)
    return 0 if all(result.exit_code == 0 for result in results) else 1


if __name__ == "__main__":
    raise SystemExit(main())
