"""Compare spec storage facts with the IR's own state inventory.

Facts come from ``spec_facts.extract_spec_facts`` (one LLM call per spec) or
from the legacy regex scanner.  Comparison is deterministic and independent of
the spec's language: names are normalised, table fields are matched by suffix,
and the IR's declared shape decides whether the spec object is representable.

A finding is a *conformance* problem, not a question: the spec already states
the shape, so there is nothing to ask.
"""

from __future__ import annotations

import re
from typing import Any

_INDEX_USE = re.compile(r"([A-Za-z_]\w*)\s*\[\s*([^\]]+?)\s*\]")
_CONSTANT = re.compile(r"^\s*(\d+|0x[0-9a-fA-F]+|\d*'[sS]?[bBoOdDhH][0-9a-fA-F_]+)\s*$")


def _norm(text: str) -> str:
    return re.sub(r"[^a-z0-9]", "", str(text).lower())


def ir_state_facts(ir: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalised view of every state plus how it is indexed."""
    states = ir.get("states") or []
    indexed: dict[str, set[str]] = {}
    blobs: list[str] = []
    for signal in ir.get("derived_signals") or []:
        if isinstance(signal, dict):
            blobs.append(str(signal.get("expression", "")))
    for transition in ir.get("transitions") or []:
        if not isinstance(transition, dict):
            continue
        blobs.append(str(transition.get("guard", "")))
        actions = transition.get("action")
        if isinstance(actions, str):
            blobs.append(actions)
        elif isinstance(actions, list):
            for action in actions:
                if isinstance(action, str):
                    blobs.append(action)
                elif isinstance(action, dict):
                    blobs.extend(str(value) for value in action.values())
        else_actions = transition.get("else_action")
        if isinstance(else_actions, str):
            blobs.append(else_actions)
        elif isinstance(else_actions, list):
            for action in else_actions:
                if isinstance(action, str):
                    blobs.append(action)
    for text in blobs:
        for match in _INDEX_USE.finditer(text):
            index = match.group(2)
            if not _CONSTANT.match(index):
                indexed.setdefault(match.group(1), set()).add(index.strip())

    facts: list[dict[str, Any]] = []
    for position, state in enumerate(states):
        if not isinstance(state, dict):
            continue
        name = str(state.get("name") or "")
        facts.append({
            "index": position,
            "name": name,
            "norm": _norm(name),
            "type": str(state.get("type") or "register"),
            "width": state.get("width"),
            "depth": state.get("depth"),
            "shape": state.get("shape"),
            "indexed_by": sorted(indexed.get(name, set())),
            "reset_value": state.get("reset_value"),
        })
    return facts


def _match(fact: dict[str, Any], states: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """States that carry the spec object (exact name, contained name, fields)."""
    hinted = str(fact.get("ir_name") or "").strip()
    if hinted:
        exact_hint = [row for row in states if row["name"] == hinted]
        if exact_hint:
            return exact_hint
    name = _norm(fact.get("name"))
    exact = [row for row in states if row["norm"] == name]
    if exact:
        return exact
    members = [row for row in states if name and (name in row["norm"] or row["norm"] in name)]
    if members:
        return members
    fields = [_norm(field) for field in fact.get("fields") or [] if field]
    if fields:
        matched = [row for row in states
                   if any(row["norm"].endswith(field) for field in fields)]
        # A single field hit is enough to prove the table exists; keep all hits.
        if matched:
            return matched
    return []


def _as_count(value: Any) -> int | None:
    try:
        return int(str(value))
    except (TypeError, ValueError):
        return None


def compare_storage(spec_facts: list[dict[str, Any]],
                    ir: dict[str, Any]) -> list[dict[str, Any]]:
    """Return conformance points for spec storage that the IR flattened."""
    states = ir_state_facts(ir)
    port_names = {_norm(row.get("name")) for row in
                  (ir.get("module") or {}).get("ports") or [] if isinstance(row, dict)}
    points: list[dict[str, Any]] = []
    for fact in spec_facts:
        if len(_norm(fact.get("name"))) <= 2 or _norm(fact.get("name")) in port_names:
            continue  # ports and one-letter hallucinations are not storage
        shape = str(fact.get("shape") or "scalar")
        count = str(fact.get("count") or "1")
        if shape == "scalar" and _as_count(count) == 1:
            continue
        element = str(fact.get("element_width") or "unknown")
        matched = _match(fact, states)
        if not matched:
            points.append({
                "kind": "storage_missing",
                "spec_name": fact.get("name"),
                "ir_pointer": "",
                "expected": {"type": "memory", "depth": count, "width": element,
                             "index": fact.get("index")},
                "spec_quote": fact.get("evidence", ""),
                "reason": f"spec declares {fact.get('name')} ({shape}, count={count}) "
                          "but no matching IR state exists",
            })
            continue
        # Multiple matches = table spread over several scalar states.
        if len(matched) > 1:
            flattened = [row for row in matched
                         if row["type"] not in {"memory", "ram"}
                         and _as_count(row.get("width")) == _as_count(count)]
            if flattened:
                points.append({
                    "kind": "storage_table_flattened",
                    "spec_name": fact.get("name"),
                    "ir_pointer": f"/states/{flattened[0]['index']}",
                    "members": [row["name"] for row in flattened],
                    "expected": {"type": "memory", "depth": count,
                                 "width": element, "index": fact.get("index")},
                    "spec_quote": fact.get("evidence", ""),
                    "reason": (f"{len(flattened)} states of '{fact.get('name')}' use "
                               f"the entry count {count} as their bit width"),
                })
            continue
        row = matched[0]
        is_array = (row["type"] in {"memory", "ram"} or bool(row.get("shape"))
                    or row.get("depth") is not None)
        if is_array:
            declared_depth = row.get("depth")
            if declared_depth is None and row.get("shape"):
                declared_depth = row["shape"][0] if isinstance(row["shape"], list) else None
            depth_ok = (str(declared_depth) == count
                        or _as_count(declared_depth) == _as_count(count))
            width_ok = element == "unknown" or str(row.get("width")) == element
            if not depth_ok or not width_ok:
                points.append({
                    "kind": "storage_shape_mismatch",
                    "spec_name": fact.get("name"),
                    "ir_pointer": f"/states/{row['index']}",
                    "expected": {"type": "memory", "depth": count,
                                 "width": element, "index": fact.get("index")},
                    "actual": {"type": row["type"], "depth": row.get("depth"),
                               "width": row.get("width")},
                    "spec_quote": fact.get("evidence", ""),
                    "reason": "IR memory shape differs from the spec",
                })
            continue
        if _as_count(count) == 1 and not fact.get("index"):
            continue  # a scalar state is a legitimate scalar storage
        points.append({
            "kind": ("selector_indexed_storage"
                     if fact.get("index") or shape == "per_selector"
                     else "storage_array_flattened"),
            "spec_name": fact.get("name"),
            "ir_pointer": f"/states/{row['index']}",
            "state": row["name"],
            "declared_width": row.get("width"),
            "indexed_by": row.get("indexed_by"),
            "expected": {"type": "memory", "depth": count, "width": element,
                         "index": fact.get("index")},
            "spec_quote": fact.get("evidence", ""),
            "reason": (f"spec stores {count} x {element} for '{fact.get('name')}', "
                       f"IR has one {row.get('type')} of width {row.get('width')}"),
        })
    return points