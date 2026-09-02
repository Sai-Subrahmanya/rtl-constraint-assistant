"""
Structured diagnostics/errors for parser and other subsystems (Manual §69).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from ..utils.enums import ErrorCode, Severity


@dataclass
class Diagnostic:
    """A structured diagnostic message with location, severity, and code."""
    code: ErrorCode
    severity: Severity
    message: str
    file: str | None = None
    line: int | None = None
    column: int | None = None
    hint: str | None = None
    context: dict[str, Any] = field(default_factory=dict)

    def __str__(self) -> str:
        loc = ""
        if self.file:
            loc = f"{self.file}"
            if self.line is not None:
                loc += f":{self.line}"
                if self.column is not None:
                    loc += f":{self.column}"
            loc += ": "
        return f"[{self.severity.value}] {loc}{self.code.value}: {self.message}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "code": self.code.value,
            "severity": self.severity.value,
            "message": self.message,
            "file": self.file,
            "line": self.line,
            "column": self.column,
            "hint": self.hint,
            "context": self.context,
        }


class DiagnosticCollector:
    """Collects diagnostics while allowing inspection by severity."""

    def __init__(self) -> None:
        self._items: list[Diagnostic] = []

    def add(self, d: Diagnostic) -> None:
        self._items.append(d)

    def error(self, code: ErrorCode, message: str, **kw: Any) -> Diagnostic:
        sev = kw.pop("severity", Severity.ERROR)
        d = Diagnostic(code=code, severity=sev, message=message, **kw)
        self._items.append(d)
        return d

    def warning(self, code: ErrorCode, message: str, **kw: Any) -> Diagnostic:
        d = Diagnostic(code=code, severity=Severity.WARNING, message=message, **kw)
        self._items.append(d)
        return d

    def info(self, code: ErrorCode, message: str, **kw: Any) -> Diagnostic:
        d = Diagnostic(code=code, severity=Severity.INFO, message=message, **kw)
        self._items.append(d)
        return d

    # --- queries ---

    def items(self) -> list[Diagnostic]:
        return list(self._items)

    def errors(self) -> list[Diagnostic]:
        return [d for d in self._items if d.severity in (Severity.ERROR, Severity.CRITICAL)]

    def has_errors(self) -> bool:
        return bool(self.errors())

    def max_severity(self) -> Severity:
        if not self._items:
            return Severity.INFO
        order = [Severity.INFO, Severity.LOW, Severity.MEDIUM, Severity.HIGH,
                 Severity.WARNING, Severity.ERROR, Severity.CRITICAL]
        return max((d.severity for d in self._items), key=lambda s: order.index(s))

    def to_list(self) -> list[dict[str, Any]]:
        return [d.to_dict() for d in self._items]

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self):
        return iter(self._items)
