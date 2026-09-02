"""
QoR metric utilities (Manual §38, §81, §96).
"""

from __future__ import annotations

from typing import Any

from ..utils.units import nearly_equal


def excess_setup_margin(wns_seconds: float | None, required_ns: float = 0.0) -> float:
    """Manual §81: excess_margin = max(0, WNS - required_operating_margin)."""
    if wns_seconds is None:
        return 0.0
    return max(0.0, wns_seconds - required_ns * 1e-9)


def compare_metrics(a: dict[str, Any], b: dict[str, Any], thresholds: dict[str, float] | None = None) -> dict[str, Any]:
    thresholds = thresholds or {"wns_ps": 1.0, "area_pct": 0.1, "power_pct": 0.1}
    changes = {}
    for key in ("setup_wns_ns", "hold_wns_ns", "area", "power"):
        va = a.get(key)
        vb = b.get(key)
        if va is None or vb is None:
            continue
        delta = vb - va
        changes[key] = {"delta": delta, "improved": False, "meaningful": False}
        if key.endswith("_ns"):
            changes[key]["meaningful"] = abs(delta) > thresholds.get("wns_ps", 1.0) * 1e-3
            changes[key]["improved"] = delta > 0
        else:
            base = abs(va) if va != 0 else 1.0
            pct = abs(delta) / base * 100
            changes[key]["pct"] = pct
            changes[key]["meaningful"] = pct > thresholds.get("area_pct", 0.1) if "area" in key else pct > thresholds.get("power_pct", 0.1)
            changes[key]["improved"] = delta < 0
    return changes
