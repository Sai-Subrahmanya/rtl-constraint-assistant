"""Generic SDC backend (Step 6 hardened renderer).

The backend is an :class:`SdcRenderer` subclass with backward-compatible
``render(cset, ...) -> str`` and a new ``generate(cset, ...) ->
SdcGenerationResult`` entry point used by the CLI.
"""

from __future__ import annotations

from ...constraint_model import ConstraintSet
from ...utils.enums import SafeMode
from ..base import SDCBackend
from ..generation.renderer import SdcRenderer


class GenericSDCBackend(SdcRenderer, SDCBackend):
    name = "generic"
    file_extension = ".sdc"

    # MRO: SdcRenderer first, then SDCBackend (ABC).
    def render(self, cset: ConstraintSet, design_name: str = "top",
               mode: SafeMode = SafeMode.BALANCED,
               with_provenance: bool = True,
               scenario: str | None = None) -> str:
        """Backward-compatible: return SDC text."""
        return self.generate(cset, design_name=design_name, mode=mode,
                             with_provenance=with_provenance,
                             scenario=scenario).text

    def generate(self, cset: ConstraintSet, design_name: str = "top",
                 mode: SafeMode = SafeMode.BALANCED,
                 with_provenance: bool = True,
                 scenario: str | None = None):
        return SdcRenderer.render(self, cset, design_name=design_name, mode=mode,
                                  with_provenance=with_provenance,
                                  scenario=scenario)

    def capabilities(self) -> dict[str, bool]:
        return {
            "create_clock": True,
            "create_generated_clock": True,
            "set_input_delay": True,
            "set_output_delay": True,
            "set_clock_uncertainty": True,
            "set_clock_latency": True,
            "set_clock_transition": True,
            "set_propagated_clock": True,
            "set_clock_groups": True,
            "set_false_path": True,
            "set_multicycle_path": True,
            "set_min_delay": True,
            "set_max_delay": True,
            "set_load": True,
            "set_input_transition": True,
            "set_max_transition": True,
            "set_max_capacitance": True,
            "set_max_fanout": True,
            "set_driving_cell": True,
            "design_rules": True,
            "mcmm": False,
            "waveform": True,
            "edge_qualifiers": True,
            "generated_clock_options": True,
        }
