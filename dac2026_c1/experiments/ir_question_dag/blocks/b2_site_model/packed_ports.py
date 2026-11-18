"""Normalize packed-array port indexing.

IR ports carry only ``width``.  A port declared as
``width: NUM_INPUTS*DATAW`` is emitted as one flat vector, while expressions
often still use the original two-dimensional form ``data_in[sel_index]``.
That selects one bit; the intended slice is ``DATAW`` bits.
"""

from __future__ import annotations

import re
from typing import Any

_INDEX = re.compile(r"\b([A-Za-z_][A-Za-z0-9_$]*)\s*\[\s*([^\[\]:]+?)\s*\](?!\s*\[)")
_DIMENSION_NAMES = {"NUM_INPUTS", "NUM_REQS", "NUM_BANKS", "NUM_LANES",
                    "SIZE", "DEPTH", "COUNT"}


def _element_width(width: Any, parameters: set[str]) -> int | str | None:
    text = str(width).replace(" ", "")
    if "*" not in text:
        return None
    factors = [factor.strip("()") for factor in text.split("*") if factor]
    for factor in factors:
        if factor.isdigit():
            value = int(factor)
            if value > 1:
                return value
        elif factor in parameters and factor not in _DIMENSION_NAMES:
            return factor
    # Fallback: NUM_INPUTS*W where W is a parameter not in the dimension set.
    for factor in factors:
        if factor in parameters and factor not in _DIMENSION_NAMES:
            return factor
    return None


def normalize_packed_indexing(ir: dict[str, Any]) -> int:
    module = ir.get("module") or {}
    parameters = {str(item.get("name")) for item in module.get("parameters") or []
                  if isinstance(item, dict)}
    widths: dict[str, Any] = {}
    for port in module.get("ports") or []:
        if isinstance(port, dict) and port.get("name"):
            widths[str(port["name"])] = port.get("width")
    changed = 0

    def rewrite(text: str) -> str:
        nonlocal changed

        def replace(match):
            nonlocal changed
            name, index = match.group(1), match.group(2).strip()
            if name not in widths:
                return match.group(0)
            element = _element_width(widths[name], parameters)
            if element is None:
                return match.group(0)
            changed += 1
            return f"{name}[({index})*({element}) +: ({element})]"

        return _INDEX.sub(replace, text)

    for signal in ir.get("derived_signals") or []:
        if isinstance(signal, dict) and signal.get("expression"):
            signal["expression"] = rewrite(str(signal["expression"]))
    for transition in ir.get("transitions") or []:
        if not isinstance(transition, dict):
            continue
        for key in ("guard",):
            if transition.get(key):
                transition[key] = rewrite(str(transition[key]))
        actions = transition.get("action") or []
        if isinstance(actions, str):
            actions = [actions]
        for index, action in enumerate(actions):
            text = str(action)
            if "=" in text:
                lhs, rhs = text.split("=", 1)
                actions[index] = lhs + "=" + rewrite(rhs)
            else:
                actions[index] = rewrite(text)
        if isinstance(transition.get("action"), list):
            transition["action"] = actions
    return changed
