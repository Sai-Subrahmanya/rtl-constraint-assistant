"""
JSON Schema for RCA project YAML configuration files (Manual §10, §108).

Used both for validating user-supplied project files and as a reference
for documentation. The schema is versioned so future revisions can be
detected and migrated.
"""

from __future__ import annotations

import json
from pathlib import Path

SCHEMA_VERSION = "1.0"

PROJECT_SCHEMA: dict = {
    "$schema": "http://json-schema.org/draft-07/schema#",
    "title": "RCA Project Configuration",
    "type": "object",
    "required": ["project", "sources"],
    "additionalProperties": False,
    "properties": {
        "schema_version": {"type": "string", "default": SCHEMA_VERSION},
        "project": {
            "type": "object",
            "required": ["name"],
            "additionalProperties": False,
            "properties": {
                "name": {"type": "string", "minLength": 1},
                "top": {"type": "string"},
                "description": {"type": "string"},
            },
        },
        "sources": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "files": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
                "include_dirs": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
                "defines": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
            },
        },
        "parameters": {
            "type": "object",
            "additionalProperties": {"type": ["string", "integer", "number", "boolean"]},
            "default": {},
        },
        "constraints": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "user": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "clocks": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["name"],
                                "properties": {
                                    "name": {"type": "string"},
                                    "period": {"type": "string"},
                                    "frequency": {"type": "string"},
                                    "waveform": {
                                        "type": "array",
                                        "items": {"type": "number"},
                                        "minItems": 2,
                                    },
                                    "port": {"type": "string"},
                                    "fixed": {"type": "boolean", "default": True},
                                    "uncertainty": {"type": "string"},
                                },
                            },
                            "default": [],
                        },
                        "generated_clocks": {
                            "type": "array",
                            "items": {"type": "object"},
                            "default": [],
                        },
                        "io": {
                            "type": "object",
                            "additionalProperties": False,
                            "properties": {
                                "inputs": {
                                    "type": "object",
                                    "additionalProperties": {
                                        "type": "object",
                                        "properties": {
                                            "delay": {"type": "string"},
                                            "clock": {"type": "string"},
                                            "fixed": {"type": "boolean", "default": True},
                                        },
                                    },
                                    "default": {},
                                },
                                "outputs": {
                                    "type": "object",
                                    "additionalProperties": {
                                        "type": "object",
                                        "properties": {
                                            "delay": {"type": "string"},
                                            "clock": {"type": "string"},
                                            "fixed": {"type": "boolean", "default": True},
                                        },
                                    },
                                    "default": {},
                                },
                            },
                            "default": {},
                        },
                        "relationships": {
                            "type": "array",
                            "items": {
                                "type": "object",
                                "required": ["clocks", "relationship"],
                                "properties": {
                                    "clocks": {
                                        "type": "array",
                                        "items": {"type": "string"},
                                        "minItems": 2,
                                    },
                                    "relationship": {
                                        "type": "string",
                                        "enum": ["synchronous", "related", "asynchronous"],
                                    },
                                    "fixed": {"type": "boolean", "default": True},
                                },
                            },
                            "default": [],
                        },
                        "exceptions": {
                            "type": "array",
                            "items": {"type": "object"},
                            "default": [],
                        },
                        "design_rules": {
                            "type": "object",
                            "additionalProperties": {"type": "string"},
                            "default": {},
                        },
                    },
                    "default": {},
                },
                "existing_sdc": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
            },
            "default": {},
        },
        "analysis": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "language": {
                    "type": "string",
                    "enum": ["verilog", "systemverilog", "vhdl"],
                    "default": "systemverilog",
                },
                "top": {"type": "string"},
                "safe_mode": {
                    "type": "string",
                    "enum": ["strict", "balanced", "exploratory"],
                    "default": "balanced",
                },
            },
            "default": {},
        },
        "flow": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "backend": {
                    "type": "string",
                    "enum": ["generic", "opensta", "synopsys", "cadence", "yosys_opensta"],
                    "default": "generic",
                },
                "stage": {
                    "type": "string",
                    "enum": [
                        "rtl_synthesis",
                        "pre_layout_sta",
                        "post_placement_sta",
                        "post_route_sta",
                        "signoff_sta",
                        "synthesis_sta",
                    ],
                    "default": "synthesis_sta",
                },
                "liberty": {
                    "oneOf": [
                        {"type": "string"},
                        {"type": "array", "items": {"type": "string"}},
                    ],
                },
                "output_dir": {"type": "string", "default": "output"},
            },
            "default": {},
        },
        "optimization": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "enabled": {"type": "boolean", "default": False},
                "max_iterations": {"type": "integer", "minimum": 1, "default": 20},
                "max_eda_runs": {"type": "integer", "minimum": 1, "default": 20},
                "max_runtime_minutes": {"type": "integer", "minimum": 1, "default": 120},
                "convergence_patience": {"type": "integer", "minimum": 1, "default": 5},
                "required_setup_margin_ns": {"type": "number", "default": 0.0},
                "required_hold_margin_ns": {"type": "number", "default": 0.0},
                "priorities": {
                    "type": "object",
                    "additionalProperties": {
                        "type": "string",
                        "enum": ["high", "medium", "low", "off"],
                    },
                    "default": {
                        "timing": "high",
                        "area": "medium",
                        "power": "medium",
                        "timing_margin_utilization": "medium",
                        "runtime": "low",
                    },
                },
                "perturbation": {
                    "type": "object",
                    "additionalProperties": False,
                    "properties": {
                        "uncertainty_range_ns": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 2,
                        },
                        "io_delay_range_ns": {
                            "type": "array",
                            "items": {"type": "number"},
                            "minItems": 2,
                            "maxItems": 2,
                        },
                    },
                    "default": {},
                },
                "thresholds": {
                    "type": "object",
                    "properties": {
                        "wns_ps": {"type": "number", "default": 1.0},
                        "area_pct": {"type": "number", "default": 0.1},
                        "power_pct": {"type": "number", "default": 0.1},
                    },
                    "default": {},
                },
            },
            "default": {},
        },
        "scenarios": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "mode": {"type": "string", "default": "functional"},
                    "corner": {"type": "string", "default": "slow"},
                    "libraries": {"type": "array", "items": {"type": "string"}, "default": []},
                    "parasitics": {"type": "string"},
                    "sdc_set_id": {"type": "string"},
                    "environment": {
                        "type": "object",
                        "additionalProperties": {"type": ["string", "number", "boolean"]},
                        "default": {},
                    },
                    "active": {"type": "boolean", "default": True},
                    "constraints": {"type": "object", "default": {}},
                },
                "default": {},
            },
            "default": [],
        },
        "mcmm": {
            "type": "object",
            "additionalProperties": False,
            "properties": {
                "enabled": {"type": "boolean", "default": False},
                "active_scenario_ids": {
                    "type": "array",
                    "items": {"type": "string"},
                    "default": [],
                },
            },
            "default": {},
        },
    },
}


def write_schema(path: str | Path) -> None:
    """Write the schema to ``path`` as JSON."""
    Path(path).write_text(json.dumps(PROJECT_SCHEMA, indent=2), encoding="utf-8")
