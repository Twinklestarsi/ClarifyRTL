"""Versioned IR representability model for phase-A closure.

The matrix is a hypothesis catalogue; the concrete verdict always comes from
building and compiling every option in :mod:`detect`.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import yaml

from .schema import S0, S1, S2, S3, Question, SiteVerdict


DEFAULT_MATRIX = Path(__file__).resolve().parents[2] / "capability" / "matrix.yaml"


def load_matrix(path: Path | str | None = None) -> dict[str, Any]:
    source = Path(path or DEFAULT_MATRIX)
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    if not isinstance(data, dict) or not isinstance(data.get("categories"), dict):
        raise ValueError(f"invalid representability matrix: {source}")
    data = dict(data)
    data["_source"] = str(source)
    return data


def _hint(question: Question, matrix: dict[str, Any]) -> tuple[str, str | None]:
    row = matrix.get("categories", {}).get(question.category, {})
    witness = row.get("witness") if isinstance(row, dict) else None
    requested = question.gap_hypothesis.strip().upper()
    if requested == "G1":
        return "schema_extension", witness
    if requested == "G2":
        return "instance_restructure", witness
    construct = question.required_construct
    mode = row.get(construct) if construct and isinstance(row, dict) else None
    if mode in {"direct", "instance_restructure", "schema_extension", "unsupported"}:
        return mode, witness
    return "unknown", witness


def assess(question: Question, verdict: SiteVerdict, matrix: dict[str, Any], *,
           allow_schema_extension: bool = False) -> dict[str, Any]:
    """Combine a concrete compile verdict with the versioned capability hint."""
    hint, witness = _hint(question, matrix)
    reason = verdict.detail.get("reason") or question.reason
    if verdict.status == S0:
        mode, ok = "direct", True
    elif verdict.detail.get("reason") == "no_behavioural_difference":
        mode, ok = "degenerate", True
    elif verdict.status in {S1, S2}:
        mode, ok = "instance_restructure", False
    elif verdict.status == S3:
        mode, ok = "schema_extension", False
    else:
        mode, ok = hint, False
    if mode == "unknown" and hint != "unknown":
        mode = hint
    extension = matrix.get("local_extensions", {}).get(question.required_construct, {})
    extension_safe = bool(extension.get("safe")) if isinstance(extension, dict) else False
    # ``allow_schema_extension`` only authorizes versioned local lowering.  It
    # must never turn a shared-schema/backend change into a pass.
    blocked = mode == "schema_extension" and (not allow_schema_extension or not extension_safe)
    return {
        "ok": ok,
        "status": verdict.status,
        "gap_type": mode,
        "required_construct": question.required_construct or None,
        "witness": witness,
        "schema_version": matrix.get("schema_version"),
        "matrix_version": matrix.get("matrix_version"),
        "schema_extension_allowed": bool(allow_schema_extension),
        "local_extension": extension or None,
        "local_extension_safe": extension_safe,
        "blocked": blocked,
        "reason": reason,
    }


def option_signature(question: Question) -> tuple[str, ...]:
    """The option set is immutable across representability rebuilds."""
    return tuple(option.value for option in question.options)
