"""Apply option patches and compute structural differences.

Patches are JSON-Patch style operations over the raw IR document and reuse the
repair module's validated applier, so an option can never mutate the frozen IR.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

import yaml


def load_ir(path: Path | str) -> dict[str, Any]:
    data = yaml.safe_load(Path(path).read_text(encoding="utf-8"))
    if not isinstance(data, dict):
        raise ValueError("IR root must be a mapping")
    return data


def dump_ir(data: dict[str, Any]) -> str:
    return yaml.safe_dump(data, allow_unicode=True, sort_keys=False,
                          default_flow_style=False)


def apply_patch(ir: dict[str, Any], operations: list[dict[str, Any]]) -> dict[str, Any]:
    from iteration.hw_ir_repair import apply_hw_ir_patch

    if not operations:
        return copy.deepcopy(ir)
    return apply_hw_ir_patch(ir, operations)


def flatten(value: Any, prefix: str = "") -> dict[str, Any]:
    """JSON-pointer -> leaf map with element-precise list paths.

    Lists are always expanded by index, including lists of scalars.  Collapsing
    a scalar list into one leaf loses the information needed to decide whether
    a patch that replaces ``/transitions/0/action/9`` stays inside that
    element's anchor: the diff would only report ``/transitions/0/action``.
    Empty lists stay a leaf because they have no element to point at.
    """
    result: dict[str, Any] = {}
    if isinstance(value, dict):
        for key, item in value.items():
            child = f"{prefix}/{_escape(key)}"
            result.update(flatten(item, child))
    elif isinstance(value, list):
        if not value:
            result[prefix or "/"] = json.dumps(value, ensure_ascii=False)
        else:
            for index, item in enumerate(value):
                result.update(flatten(item, f"{prefix}/{index}"))
    else:
        result[prefix or "/"] = value
    return result


def _escape(token: str) -> str:
    return str(token).replace("~", "~0").replace("/", "~1")


def structural_diff(left: dict[str, Any], right: dict[str, Any]) -> list[str]:
    """Paths whose value differs (added, removed or changed)."""
    a, b = flatten(left), flatten(right)
    paths = {path for path in set(a) | set(b) if a.get(path) != b.get(path)}
    return sorted(paths)


def _related(path: str, prefix: str) -> bool:
    return path == prefix or path.startswith(prefix.rstrip("/") + "/")


def within_site(paths: list[str], anchors: list[str]) -> bool:
    """Every differing path must fall under one of the declared anchors."""
    if not paths:
        return False
    if not anchors:
        return False
    return all(any(_related(path, anchor) for anchor in anchors) for path in paths)


def touched_top_level(paths: list[str]) -> set[str]:
    return {path.strip("/").split("/")[0] for path in paths if path.strip("/")}

def resolve_pointer(doc: Any, pointer: str) -> tuple[bool, Any]:
    """Resolve an RFC6901 pointer; returns ``(exists, value)``."""
    if pointer in ("", "/"):
        return True, doc
    node = doc
    for raw in pointer.strip("/").split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if token not in node:
                return False, None
            node = node[token]
        elif isinstance(node, list):
            if token == "-":
                return False, None
            try:
                node = node[int(token)]
            except (ValueError, IndexError):
                return False, None
        else:
            return False, None
    return True, node


def normalise_pointer(doc: Any, pointer: str) -> str | None:
    """Resolve list segments by object name and return an index-only pointer.

    The IR is a tree of mappings and lists; a JSON Pointer into it must address
    list elements by index.  LLM inventory output frequently uses the object
    name instead (``/derived_signals/read_data_comb/expression``), which is a
    well-defined reference as long as exactly one element carries that name.
    Returns ``None`` when a segment cannot be resolved unambiguously.
    """
    if pointer in ("", "/"):
        return pointer
    node = doc
    tokens: list[str] = []
    for raw in pointer.strip("/").split("/"):
        token = raw.replace("~1", "/").replace("~0", "~")
        if isinstance(node, dict):
            if token not in node:
                return None
            node = node[token]
            tokens.append(_escape(token))
        elif isinstance(node, list):
            index = None
            if token.isdigit() and int(token) < len(node):
                index = int(token)
            else:
                matches = [position for position, item in enumerate(node)
                           if isinstance(item, dict) and str(item.get("name")) == token]
                if len(matches) == 1:
                    index = matches[0]
            if index is None:
                return None
            node = node[index]
            tokens.append(str(index))
        else:
            return None
    return "/" + "/".join(tokens)


def normalise_question_pointers(ir: dict[str, Any], question: Any) -> list[str]:
    """Rewrite name-addressed anchors/patches onto index-only JSON pointers.

    Returns the unresolvable pointers.  The question is only mutated when every
    pointer resolves, so a partial rewrite can never hide a broken option.
    """
    problems: list[str] = []
    anchors: list[str] = []
    for anchor in question.anchors:
        fixed = normalise_pointer(ir, str(anchor))
        if fixed is None:
            problems.append(f"anchor does not resolve: {anchor}")
        else:
            anchors.append(fixed)
    rewritten: list[tuple[dict[str, Any], str]] = []
    for option in question.options:
        for operation in option.patch:
            if not isinstance(operation, dict):
                continue
            path = operation.get("path")
            if not isinstance(path, str):
                problems.append(f"option {option.value}: patch without path")
                continue
            if path == "/" or path == "":
                fixed = path
            elif path.endswith("/-"):
                parent = normalise_pointer(ir, path[:-2])
                fixed = None if parent is None else parent.rstrip("/") + "/-"
            elif operation.get("op") == "add":
                head, _, tail = path.rstrip("/").rpartition("/")
                parent = normalise_pointer(ir, head or "/")
                fixed = None if parent is None else parent.rstrip("/") + "/" + tail
            else:
                fixed = normalise_pointer(ir, path)
            if fixed is None:
                problems.append(f"option {option.value}: patch path does not resolve: {path}")
            else:
                rewritten.append((operation, fixed))
    if problems:
        return problems
    question.anchors = anchors
    for operation, fixed in rewritten:
        operation["path"] = fixed
    return []


def pointer_exists(doc: Any, pointer: str, *, allow_prefix: bool = False) -> bool:
    """True when the pointer resolves (or, optionally, one of its prefixes does)."""
    if resolve_pointer(doc, pointer)[0]:
        return True
    if not allow_prefix:
        return False
    parts = pointer.strip("/").split("/")
    for cut in range(len(parts) - 1, 0, -1):
        if resolve_pointer(doc, "/" + "/".join(parts[:cut]))[0]:
            return True
    return False
