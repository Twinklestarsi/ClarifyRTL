"""Deterministic port-index normalisation.

The IR declares ports with ``width`` only, so the compiler always emits
``[width-1:0]``.  Reference designs occasionally use one-based ranges such as
``input [4:1] x``; a model copying those indices produces ``x[4]`` against a
four-bit port, which JasperGold rejects at elaboration.  Shifting *all*
constant indices of one symbol by the same amount preserves bit positions, so
it is a renaming rather than a semantic change.
"""

from __future__ import annotations

import copy
import re
from typing import Any

_SELECT = re.compile(r"\b([A-Za-z_][A-Za-z0-9_$]*)\s*\[\s*(\d+)\s*(?::\s*(\d+)\s*)?\]")


def _widths(ir: dict[str, Any]) -> dict[str, int]:
    widths: dict[str, int] = {}
    for port in ir.get("module", {}).get("ports") or []:
        if not isinstance(port, dict) or "name" not in port:
            continue
        try:
            widths[str(port["name"])] = int(str(port.get("width", 1)))
        except (TypeError, ValueError, KeyError):
            continue
    for state in ir.get("states") or []:
        if not isinstance(state, dict) or "name" not in state:
            continue
        try:
            widths[str(state["name"])] = int(str(state.get("width", 1)))
        except (TypeError, ValueError, KeyError):
            continue
    return widths


def _expressions(ir: dict[str, Any]):
    """Yield (container, key) pairs holding Verilog expression text."""
    for signal in ir.get("derived_signals") or []:
        yield signal, "expression"
    for transition in ir.get("transitions") or []:
        yield transition, "guard"
        for key in ("action", "else_action"):
            actions = transition.get(key)
            if isinstance(actions, str):
                yield transition, key
            elif isinstance(actions, list):
                for index in range(len(actions)):
                    yield actions, index


def _shifts(ir: dict[str, Any]) -> dict[str, int]:
    widths = _widths(ir)
    observed: dict[str, set[int]] = {}
    for container, key in _expressions(ir):
        text = container[key] if isinstance(container, list) else container.get(key)
        if not isinstance(text, str):
            continue
        for match in _SELECT.finditer(text):
            name, high, low = match.group(1), int(match.group(2)), match.group(3)
            if name not in widths:
                continue
            values = {high} | ({int(low)} if low is not None else set())
            observed.setdefault(name, set()).update(values)
    shifts: dict[str, int] = {}
    for name, values in observed.items():
        width = widths[name]
        if max(values) < width:
            continue
        shift = max(values) - width + 1
        if shift <= 0 or min(values) < shift:
            continue
        shifts[name] = shift
    return shifts


def normalize_port_indices(ir: dict[str, Any]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return a normalised copy and the list of applied shifts."""
    candidate = copy.deepcopy(ir)
    shifts = _shifts(candidate)
    if not shifts:
        return candidate, []

    def rewrite(text: str) -> str:
        def replace(match):
            name, high, low = match.group(1), int(match.group(2)), match.group(3)
            shift = shifts.get(name)
            if not shift:
                return match.group(0)
            if low is None:
                return f"{name}[{high - shift}]"
            return f"{name}[{high - shift}:{int(low) - shift}]"

        return _SELECT.sub(replace, text)

    for container, key in _expressions(candidate):
        if isinstance(container, list):
            if isinstance(container[key], str):
                container[key] = rewrite(container[key])
        elif isinstance(container.get(key), str):
            container[key] = rewrite(container[key])
    return candidate, [{"symbol": name, "shift": shift} for name, shift in sorted(shifts.items())]


def normalize_file(path, output_path=None):
    """Convenience wrapper over YAML files."""
    import yaml

    data = yaml.safe_load(open(path, encoding="utf-8"))
    normalised, applied = normalize_port_indices(data)
    if applied:
        target = output_path or path
        with open(target, "w", encoding="utf-8") as handle:
            yaml.safe_dump(normalised, handle, allow_unicode=True,
                           sort_keys=False, default_flow_style=False)
    return applied