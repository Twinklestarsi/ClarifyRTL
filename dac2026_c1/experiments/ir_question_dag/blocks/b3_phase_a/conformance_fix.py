"""Turn storage-conformance findings into a legal IR patch (generic part).

A finding says "the spec stores N elements of width W here; the IR has one
scalar of width N".  The patch below rewrites the state declaration and, when
the spec names a selector, indexes every bare use by that selector.

Only findings that can be expressed as a local rewrite produce a patch: a
table spread over many scalar fields needs a structural rebuild, so it is
reported as ``unfixable`` instead of being silently mangled.
"""

from __future__ import annotations

import re
from typing import Any

from blocks.b2_site_model.schema import Option, Question

_BARE = re.compile(r"\b{name}\b(?!\s*\[)")


def _set_field(index: int, field: str, value: Any, state: dict[str, Any]) -> dict[str, Any]:
    """``add`` when the field is absent, ``replace`` when it exists."""
    op = "replace" if field in state else "add"
    return {"op": op, "path": f"/states/{index}/{field}", "value": value}


def _index_patch(ir: dict[str, Any], name: str, selector: str) -> list[dict[str, Any]]:
    """Index bare uses of ``name`` in guards/actions/derived expressions."""
    bare = re.compile(rf"\b{re.escape(name)}\b(?!\s*\[)")
    operations: list[dict[str, Any]] = []
    for index, signal in enumerate(ir.get("derived_signals") or []):
        if isinstance(signal, dict):
            expression = str(signal.get("expression", ""))
            if bare.search(expression):
                operations.append({"op": "replace",
                                   "path": f"/derived_signals/{index}/expression",
                                   "value": bare.sub(f"{name}[{selector}]", expression)})
    for index, transition in enumerate(ir.get("transitions") or []):
        if not isinstance(transition, dict):
            continue
        guard = transition.get("guard")
        if isinstance(guard, str) and bare.search(guard):
            operations.append({"op": "replace", "path": f"/transitions/{index}/guard",
                               "value": bare.sub(f"{name}[{selector}]", guard)})
        for key in ("action", "else_action"):
            value = transition.get(key)
            if isinstance(value, str):
                if bare.search(value):
                    operations.append({"op": "replace",
                                       "path": f"/transitions/{index}/{key}",
                                       "value": bare.sub(f"{name}[{selector}]", value)})
            elif isinstance(value, list):
                for position, entry in enumerate(value):
                    if isinstance(entry, str):
                        if bare.search(entry):
                            operations.append({
                                "op": "replace",
                                "path": f"/transitions/{index}/{key}/{position}",
                                "value": bare.sub(f"{name}[{selector}]", entry)})
                    elif isinstance(entry, dict):
                        # dict action: rewrite the textual values only
                        updated = {k: (bare.sub(f"{name}[{selector}]", str(v))
                                       if isinstance(v, str) and bare.search(v) else v)
                                   for k, v in entry.items()}
                        if updated != entry:
                            operations.append({
                                "op": "replace",
                                "path": f"/transitions/{index}/{key}/{position}",
                                "value": updated})
    return operations


def build_patch(ir: dict[str, Any], point: dict[str, Any]) -> tuple[list[dict[str, Any]] | None, str]:
    """Return ``(patch, reason)``; ``patch is None`` means "cannot fix locally"."""
    expected = point.get("expected") or {}
    depth = expected.get("depth")
    width = expected.get("width")
    selector = expected.get("index")
    pointer = str(point.get("ir_pointer") or "")
    if point.get("kind") == "storage_table_flattened" or not pointer.startswith("/states/"):
        return None, "table-level finding needs a structural rebuild"
    try:
        index = int(pointer.rsplit("/", 1)[-1])
        state = ir["states"][index]
    except (KeyError, IndexError, ValueError):
        return None, "state not found"
    if not depth or str(depth) in {"0", "1"}:
        return None, "no element count to expand"
    operations: list[dict[str, Any]] = [
        _set_field(index, "type", "memory", state),
    ]
    if width and str(width) != "unknown":
        operations.append(_set_field(index, "width", width, state))
    operations.append(_set_field(index, "depth", depth, state))
    if state.get("shape"):
        operations.append({"op": "remove", "path": f"/states/{index}/shape"})
    operations.append(_set_field(index, "reset_value",
                                 state.get("reset_value") if state.get("reset_value") is not None else 0,
                                 state))
    if selector:
        operations.extend(_index_patch(ir, str(state.get("name")), str(selector)))
    return operations, ""


def question_for_point(ir: dict[str, Any], point: dict[str, Any]) -> Question | None:
    patch, _reason = build_patch(ir, point)
    if not patch:
        return None
    expected = point.get("expected") or {}
    name = point.get("state") or point.get("spec_name") or "storage"
    return Question(
        id=f"spec_{str(name).lower()}",
        category="C7",
        template="storage_shape",
        text=(f"spec 写明 {point.get('spec_name')} 是 {expected.get('depth')} 份 "
              f"{expected.get('width')} 位存储，当前 IR 是单个寄存器；是否改为按索引存储？"),
        anchors=[str(point.get("ir_pointer"))],
        options=[Option(value="current", label="保持当前寄存器"),
                 Option(value=f"memory_{expected.get('depth')}",
                        label=f"改为 memory[{expected.get('depth')}]", patch=patch)],
        current="current",
        required_construct="memory",
        evidence=[str(point.get("spec_quote", ""))],
        evidence_source="visible_spec",
        generated_by="spec_conformance",
        source_point=str(point.get("kind", "")))

def _normalise_point(point: dict[str, Any]) -> dict[str, Any]:
    """Accept both the fact-based points and the legacy regex findings."""
    if isinstance(point.get("expected"), dict):
        return point
    declared = str(point.get("declared_width") or "")
    return dict(point, expected={
        "type": "memory",
        "depth": declared or "1",
        "width": point.get("element_width") or "unknown",
        "index": point.get("selector") or point.get("index"),
    })


def conformance_questions(ir: dict[str, Any], spec_text: str, *,
                          facts: list[dict[str, Any]] | None = None) -> list[Question]:
    """Questions for every storage finding that admits a local patch."""
    if facts is not None:
        from blocks.b4_phase_b_graph.storage_conformance import compare_storage

        points = compare_storage(facts, ir)
    else:
        from blocks.b4_phase_b_graph.conformance import scan_spec_conformance

        points = scan_spec_conformance(ir, spec_text)
    questions: list[Question] = []
    for point in points:
        question = question_for_point(ir, _normalise_point(point))
        if question is not None:
            questions.append(question)
    return questions
