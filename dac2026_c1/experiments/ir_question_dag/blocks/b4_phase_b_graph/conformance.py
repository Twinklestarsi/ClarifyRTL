"""Spec <-> IR conformance scan.

The question DAG only holds *decisions*: sites where two answers both compile
and produce different RTL.  A whole class of real defects never appears there
because the IR is structurally legal but contradicts the specification, e.g. a
per-warp register file flattened into an ``N``-bit vector.

This module produces conformance *problem points* instead of questions.  Each
point quotes the spec line, names the IR object that violates it and states the
shape the spec mandates, so the DAG can carry it as a ``conformance`` node and
phase C can turn it into a repair patch.
"""

from __future__ import annotations

import re
from typing import Any

# "MSHR_SIZE", "NUM_WARPS", "DEPTH", "CHAIN_COUNT" ... parameters that mean
# "how many of something", never a bit width of a single scalar.
_SIZE_PARAMETER = re.compile(r"(NUM_|^N$|_N$|SIZE|DEPTH|COUNT|ENTRIES|LANES)")
# Spec wording that promises storage per entry / per warp / per lane.
_STORAGE_WORDS = re.compile(
    r"(per[- ]\w+|one independent .*?register|entry table|table of|"
    r"array of|entries|register file|scratch register|bitmap)", re.I)
# "0xCCB WARP_SCRATCH accesses one independent XLEN-bit register"
_NAMED_STORAGE = re.compile(
    r"(0x[0-9A-Fa-f]+)?\s*([A-Z][A-Z0-9_]{2,})\s+(?:accesses|stores|holds|is)\b"
    r"[^\n]{0,120}", re.I)


def _spec_line(spec_text: str, needle: str) -> str:
    for line in spec_text.splitlines():
        if needle and needle.lower() in line.lower():
            return line.strip()
    return ""


def _state_spec_name(name: str) -> str:
    """``warp_scratch`` -> ``WARP_SCRATCH`` (specs upper-case CSR names)."""
    return str(name).upper()


def scan_storage_shapes(ir: dict[str, Any], spec_text: str) -> list[dict[str, Any]]:
    """Find states that flatten an array/table into one scalar bit-vector."""
    found: list[dict[str, Any]] = []
    parameters = {
        str(row.get("name")): str(row.get("value"))
        for row in (ir.get("module") or {}).get("parameters") or []
        if isinstance(row, dict)
    }
    size_parameters = {name for name in parameters if _SIZE_PARAMETER.search(name)}
    states = ir.get("states") or []
    table_named = bool(re.search(r"entry table|entries", spec_text, re.I))
    for index, state in enumerate(states):
        if not isinstance(state, dict):
            continue
        if str(state.get("type")) in {"memory", "ram"}:
            continue
        name = str(state.get("name") or "")
        width = str(state.get("width") or "")
        lookups = [_state_spec_name(name), name]
        quote = next((line for needle in lookups
                      if (line := _spec_line(spec_text, needle))), "")
        storage_context = quote or (
            _spec_line(spec_text, "entry table") if table_named and name.startswith("entry")
            else "")
        if not storage_context:
            continue
        if not _STORAGE_WORDS.search(storage_context):
            continue
        # A width that is a *size* parameter is the tell: the model used the
        # entry count as the bit width of a single register.
        if width in size_parameters:
            found.append({
                "kind": "storage_array_flattened",
                "ir_pointer": f"/states/{index}",
                "state": name,
                "declared_width": width,
                "size_parameter": width,
                "spec_quote": storage_context[:240],
                "expected": (f"type: memory (or one register per entry), "
                             f"depth: {width}, width: the element width"),
                "reason": (f"state {name!r} uses the entry count {width} as a bit "
                           "width; the spec describes per-entry storage"),
            })
    return found


def scan_per_selector_storage(ir: dict[str, Any], spec_text: str) -> list[dict[str, Any]]:
    """Find ``selected by <warp>`` storage modelled as a plain register."""
    found: list[dict[str, Any]] = []
    states = ir.get("states") or []
    by_upper = {str(row.get("name")).upper(): (i, row)
                for i, row in enumerate(states) if isinstance(row, dict)}
    for match in _NAMED_STORAGE.finditer(spec_text):
        name = match.group(2).upper()
        if name not in by_upper:
            continue
        index, state = by_upper[name]
        if str(state.get("type")) in {"memory", "ram"}:
            continue
        window = spec_text[match.start(): match.start() + 400]
        if not re.search(r"selected by|per[- ]\w+|each\b", window, re.I):
            continue
        found.append({
            "kind": "selector_indexed_storage",
            "ir_pointer": f"/states/{index}",
            "state": str(state.get("name")),
            "declared_width": str(state.get("width")),
            "spec_quote": match.group(0).strip()[:240],
            "expected": ("type: memory + depth = number of selectable entries, "
                         "indexed by the selector"),
            "reason": (f"state {state.get('name')!r} is a single register but the "
                       "spec selects one independent register by index"),
        })
    return found


def scan_spec_conformance(ir: dict[str, Any], spec_text: str) -> list[dict[str, Any]]:
    """All conformance problem points for one IR."""
    points = scan_storage_shapes(ir, spec_text) + scan_per_selector_storage(ir, spec_text)
    # de-duplicate by (kind, ir_pointer)
    seen: set[tuple[str, str]] = set()
    unique: list[dict[str, Any]] = []
    for point in points:
        key = (str(point.get("kind")), str(point.get("ir_pointer")))
        if key in seen:
            continue
        seen.add(key)
        unique.append(point)
    return unique