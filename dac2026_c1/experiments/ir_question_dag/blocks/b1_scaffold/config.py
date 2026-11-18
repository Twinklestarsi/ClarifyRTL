"""Configuration loading and path resolution for the experiment."""

from __future__ import annotations

import copy
from pathlib import Path
from typing import Any

import yaml

DEFAULT_CONFIG = Path(__file__).resolve().parents[2] / "config" / "default.yaml"


def experiment_root() -> Path:
    """``.../dac2026_c1/experiments/ir_question_dag``."""
    return Path(__file__).resolve().parents[2]


def project_root() -> Path:
    """``.../dac2026_c1`` — the package that owns the IR/compiler code."""
    return Path(__file__).resolve().parents[4]


def load_config(path: Path | str | None = None) -> dict[str, Any]:
    """Load the YAML config, resolving ``output_root``/``matrix``-style paths."""
    source = Path(path or DEFAULT_CONFIG)
    config = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(config, dict):
        raise ValueError(f"config must be a mapping: {source}")
    config = copy.deepcopy(config)
    config["_source"] = str(source)
    run = config.setdefault("run", {})
    output = Path(run.get("output_root", "runs"))
    run["output_root"] = str(output if output.is_absolute() else experiment_root() / output)
    for section, key, default in (("site_model", "registry", "capability/sites.json"),
                                  ("site_model", "witnesses", "capability/witnesses"),
                                  ("site_model", "taxonomy", "templates/eight_categories.yaml"),
                                  ("capability", "matrix", "capability/matrix.json")):
        block = config.get(section)
        if isinstance(block, dict) and key in block:
            value = Path(block[key])
            if not value.is_absolute():
                block[key] = str(experiment_root() / value)
        elif isinstance(block, dict) and key == "registry":
            block[key] = str(experiment_root() / default)
    return config


def get(config: dict[str, Any], dotted: str, default: Any = None) -> Any:
    """``get(cfg, "site_model.require_local_diff", True)``."""
    node: Any = config
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            return default
        node = node[part]
    return node