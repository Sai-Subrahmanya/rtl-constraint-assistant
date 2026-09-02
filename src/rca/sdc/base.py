"""
Abstract SDC backend interface (Manual §51).
"""

from __future__ import annotations

from abc import ABC, abstractmethod

from ..constraint_model import ConstraintSet


class SDCBackend(ABC):
    """Base class for tool-specific SDC emitters."""

    name: str = "base"
    file_extension: str = ".sdc"

    @abstractmethod
    def render(self, cset: ConstraintSet, design_name: str = "top") -> str:
        """Render a ConstraintSet to SDC text for this backend."""

    def capabilities(self) -> dict[str, bool]:
        return {
            "create_clock": True,
            "create_generated_clock": True,
            "set_input_delay": True,
            "set_output_delay": True,
            "set_clock_uncertainty": True,
            "set_false_path": True,
            "set_multicycle_path": True,
            "set_clock_groups": True,
            "set_max_transition": True,
            "set_load": True,
            "mcmm": False,
        }
