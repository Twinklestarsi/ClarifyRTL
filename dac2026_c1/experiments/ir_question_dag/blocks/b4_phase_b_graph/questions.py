"""Separate question graph layered over the immutable structural IR graph."""

from __future__ import annotations

import hashlib
import re
from collections import defaultdict
from pathlib import Path
from typing import Any

import yaml

from blocks.b2_site_model.schema import Option, Question
from blocks.b4_phase_b_graph.graph import resolve_anchor_nodes

DEFAULT_TAXONOMY = Path(__file__).resolve().parents[2] / "templates" / "eight_categories.yaml"


def _question_id(category: str, template: str, anchor: str) -> str:
    digest = hashlib.sha256(f"{category}|{template}|{anchor}".encode()).hexdigest()[:10]
    return f"auto_{category.lower()}_{digest}"


def _replace(path: str, value: Any) -> list[dict[str, Any]]:
    return [{"op": "replace", "path": path, "value": value}]


def _make_question(category: str, template: str, text: str, anchor: str | list[str],
                   current: str, alternatives: list[tuple[str, str, list[dict[str, Any]]]],
                   *, required_construct: str, gap: str = "direct",
                   source_point: str = "") -> Question | None:
    # A question is binary: keep the current IR, or apply one concrete change.
    # A candidate with no patch is not a question, so such a template returns
    # None instead of emitting an "unmaterialized" placeholder.
    alternatives = [item for item in alternatives if item[2]]
    if not alternatives:
        return None
    anchors = [anchor] if isinstance(anchor, str) else [str(item) for item in anchor]
    options = [Option(value=current, label="保持当前实现", patch=[])]
    options.extend(Option(value=value, label=label, patch=patch)
                   for value, label, patch in alternatives)
    return Question(
        id=_question_id(category, template, "|".join(anchors)), category=category,
        template=template, text=text, anchors=anchors, options=options,
        current=current, required_construct=required_construct,
        gap_hypothesis=gap, evidence=[f"IR 结构锚点 {anchor}" for anchor in anchors],
        evidence_source="ir_structure", generated_by="deterministic_ir_scan",
        source_point=source_point,
    )


def _top_level_and(expression: str) -> tuple[str, str] | None:
    """Split ``A && B`` on the single top-level ``&&``; ``None`` otherwise."""
    text = expression.strip()
    while text.startswith("(") and text.endswith(")") and _balanced(text[1:-1]):
        text = text[1:-1].strip()
    depth = 0
    quoted = False
    for index, char in enumerate(text):
        if char == '"':
            quoted = not quoted
        elif quoted:
            continue
        elif char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
        elif depth == 0 and text[index:index + 2] == "&&":
            left, right = text[:index].strip(), text[index + 2:].strip()
            if left and right and "&&" not in right:
                return left, right
            return None
    return None


def _balanced(text: str) -> bool:
    depth = 0
    for char in text:
        if char == "(":
            depth += 1
        elif char == ")":
            depth -= 1
            if depth < 0:
                return False
    return depth == 0


def _declared_names(ir: dict[str, Any]) -> set[str]:
    """Every identifier that may legally appear in an IR expression."""
    names: set[str] = set()
    module = ir.get("module") or {}
    for group in (module.get("ports"), module.get("parameters")):
        for item in group or []:
            if isinstance(item, dict) and item.get("name"):
                names.add(str(item["name"]))
    for key in ("states", "derived_signals"):
        for item in ir.get(key) or []:
            if isinstance(item, dict) and item.get("name"):
                names.add(str(item["name"]))
    return names


def _handshake_split(ir: dict[str, Any]) -> tuple[int, str, str, str] | None:
    """Find a derived ``valid && ready`` condition and its index.

    Returns ``(index, expression_key, valid_operand, expression)``.  The
    alternative answer can then drop the ready term without inventing any
    identifier, so the candidate IR always compiles.
    """
    declared = _declared_names(ir)
    for index, row in enumerate(ir.get("derived_signals") or []):
        if not isinstance(row, dict):
            continue
        key = "expression" if "expression" in row else "expr"
        expression = str(row.get(key, "")).strip()
        parts = _top_level_and(expression)
        if not parts:
            continue
        left, right = parts
        if left not in declared or right not in declared:
            continue
        if re.search(r"valid", left, re.I) and re.search(r"ready", right, re.I):
            return index, key, left, expression
        if re.search(r"valid", right, re.I) and re.search(r"ready", left, re.I):
            return index, key, right, expression
    return None


def _action_targets(transition: dict[str, Any]) -> list[str]:
    """Base targets assigned by a transition's action list."""
    actions = transition.get("action")
    rows = actions if isinstance(actions, list) else [actions]
    targets: list[str] = []
    for action in rows:
        if isinstance(action, dict):
            target = action.get("target")
        else:
            match = re.match(
                r"^\s*([A-Za-z_][A-Za-z0-9_$]*)\s*(?:\[[^]]+\]\s*)?(?:<=|=)",
                str(action or ""))
            target = match.group(1) if match else None
        if target:
            targets.append(str(target).split("[", 1)[0].strip())
    return targets


def _process_index(ir: dict[str, Any], transition_name: str) -> int | None:
    matches = [index for index, process in enumerate(ir.get("sequential_processes") or [])
               if isinstance(process, dict)
               and transition_name in (process.get("transitions") or [])]
    return matches[0] if len(matches) == 1 else None


def _state_writers(ir: dict[str, Any]) -> dict[str, set[str]]:
    writers: dict[str, set[str]] = defaultdict(set)
    for transition in ir.get("transitions") or []:
        if not isinstance(transition, dict) or not transition.get("name"):
            continue
        for target in _action_targets(transition):
            writers[target].add(str(transition["name"]))
    return writers


def _clear_on_false_candidate(ir: dict[str, Any], index: int) -> dict[str, Any] | None:
    """Mechanically checked candidate for "clear when the guard is false".

    The candidate may only be emitted when every structural precondition is
    provable from the IR: a sequential transition in exactly one process, a
    scalar target that all of its writers share in that same process, a
    declared clock domain, a unique new name and a well-formed priority list.
    Anything else is left to the generic materialization flow.
    """
    transitions = ir.get("transitions") or []
    if index >= len(transitions):
        return None
    transition = transitions[index]
    if not isinstance(transition, dict):
        return None
    name = str(transition.get("name") or "")
    if not name or str(transition.get("transition_type", "sequential")) != "sequential":
        return None
    guard = str(transition.get("guard", "1")).strip()
    if guard.lower() in {"1", "1'b1", "true"}:
        return None
    process_index = _process_index(ir, name)
    if process_index is None:
        return None
    process = (ir.get("sequential_processes") or [])[process_index]
    targets = list(dict.fromkeys(_action_targets(transition)))
    if not targets:
        return None
    states = {str(row.get("name")): row for row in ir.get("states") or []
              if isinstance(row, dict)}
    writers = _state_writers(ir)
    process_transitions = set(process.get("transitions") or [])
    for target in targets:
        state = states.get(target)
        if state is None or str(state.get("type", "")).lower() in {"memory", "ram"}:
            return None
        if writers.get(target, set()) - process_transitions:
            return None  # another RTL process writes the same state
    domain = str(transition.get("clock_domain") or process.get("clock_domain") or "")
    domains = {str(row.get("name")) for row in ir.get("clock_domains") or []
               if isinstance(row, dict)}
    if not domain or domain not in domains:
        return None
    new_name = f"{name}__clear_on_false"
    if any(isinstance(row, dict) and row.get("name") == new_name for row in transitions):
        return None
    priority = ir.get("priority")
    known_names = {str(row.get("name")) for row in transitions if isinstance(row, dict)}
    if priority is not None:
        if not isinstance(priority, list):
            return None
        for edge in priority:
            if not isinstance(edge, dict):
                return None
            if str(edge.get("higher")) not in known_names or str(edge.get("lower")) not in known_names:
                return None
    domain_row = next((row for row in ir.get("clock_domains") or []
                       if isinstance(row, dict) and row.get("name") == domain), {})
    trigger = transition.get("trigger") or domain_row.get("clock")
    if not trigger:
        return None
    clear_actions = [
        f"{target} = {states[target].get('reset_value', states[target].get('initial_value', 0))}"
        for target in targets]
    new_transition = {
        "name": new_name, "clock_domain": domain, "trigger": trigger,
        "guard": f"!({guard})",
        "action": clear_actions,
        "latency": transition.get("latency", 0),
        "transition_type": "sequential",
        "update_semantics": "non_blocking",
    }
    precedence = {"higher": name, "lower": new_name}
    if isinstance(priority, list):
        priority_patch = {"op": "add", "path": "/priority/-", "value": precedence}
        priority_anchor = f"/priority/{len(priority)}"
    else:
        priority_patch = {"op": "add", "path": "/priority", "value": [precedence]}
        priority_anchor = "/priority"
    return {
        "patch": [
            {"op": "add", "path": "/transitions/-", "value": new_transition},
            {"op": "add", "path": f"/sequential_processes/{process_index}/transitions/-",
             "value": new_name},
            priority_patch,
        ],
        "anchors": [f"/transitions/{index}", f"/transitions/{len(transitions)}",
                    f"/sequential_processes/{process_index}", priority_anchor],
        "transition": transition,
    }


_WRITE_TARGET = re.compile(r"^\s*([A-Za-z_][A-Za-z0-9_$]*)")


def _transition_writes(ir: dict[str, Any]) -> dict[str, set[str]]:
    """Map every transition name to the set of registers it assigns."""
    written: dict[str, set[str]] = {}
    for row in ir.get("transitions") or []:
        if not isinstance(row, dict):
            continue
        targets: set[str] = set()
        if row.get("target"):
            targets.add(str(row["target"]))
        actions = row.get("action") or []
        if isinstance(actions, str):
            actions = [actions]
        for action in actions:
            text = str(action)
            lhs = text.split("=", 1)[0] if "=" in text else text
            match = _WRITE_TARGET.match(lhs.strip())
            if match:
                targets.add(match.group(1))
        written[str(row.get("name") or "")] = targets
    return written


def _taxonomy(path: Path | str | None = None) -> dict[str, dict[str, Any]]:
    source = Path(path or DEFAULT_TAXONOMY)
    if not source.is_absolute() and not source.is_file():
        source = Path(__file__).resolve().parents[2] / source
    data = yaml.safe_load(source.read_text(encoding="utf-8"))
    return {str(row["id"]): row for row in data.get("categories", [])}


def expand_ir_question_templates(ir: dict[str, Any], *,
                                 taxonomy_path: Path | str | None = None,
                                 problem_points: list[dict[str, Any]] | None = None,
                                 ) -> list[Question]:
    """Deterministically expand concrete IR nodes into C1..C8 candidates.

    These are structural candidates, not inferred specification facts.  Every
    direct option is an RFC6902 patch at the reported anchor.  A concept that
    the shared schema cannot safely carry is emitted as an explicit G1
    candidate so the phase-A controller can block instead of guessing.
    """
    rows = _taxonomy(taxonomy_path)
    found: list[Question] = []

    priority = ir.get("priority") or []
    if priority and isinstance(priority[0], dict):
        # A priority edge only changes behaviour when both transitions write
        # the same target register.  Reversing an edge between disjoint
        # writers compiles to byte-identical RTL (scoreboard's C1), so asking
        # it can never produce a site.
        written = _transition_writes(ir)
        item = next((row for row in priority if isinstance(row, dict)
                     and written.get(str(row.get("higher")))
                     and written.get(str(row.get("higher"))) & written.get(str(row.get("lower")), set())),
                    None)
        if item is not None:
            index = priority.index(item)
            anchor = f"/priority/{index}"
            reverse = dict(item)
            reverse["higher"], reverse["lower"] = item.get("lower"), item.get("higher")
            found.append(_make_question(
                "C1", rows["C1"]["site_template"], "并发条件同时成立时，是否反转当前优先级？",
                anchor, "declared_order", [("reverse_order", "反转优先级", _replace(anchor, reverse))],
                required_construct="priority[]"))

    transitions = ir.get("transitions") or []
    timed = next(((i, row) for i, row in enumerate(transitions)
                  if isinstance(row, dict) and row.get("latency") in {0, 1}), None)
    if timed:
        index, row = timed
        anchor = f"/transitions/{index}/latency"
        value = int(row["latency"])
        found.append(_make_question(
            "C2", rows["C2"]["site_template"], "该次状态更新应在同拍还是下一拍生效？",
            anchor, f"latency_{value}",
            [(f"latency_{1 - value}", f"latency={1 - value}", _replace(anchor, 1 - value))],
            required_construct="transition_latency"))

    derived = ir.get("derived_signals") or []
    boundary_pattern = re.compile(r"(?:full|empty|max|min|overflow|underflow|bound|limit)", re.I)
    boundary = next(((i, row) for i, row in enumerate(derived)
                     if isinstance(row, dict) and (boundary_pattern.search(str(row.get("name", "")))
                     or re.search(r"(?:==|>=|<=)", str(row.get("expression", ""))))), None)
    if boundary:
        index, row = boundary
        key = "expression" if "expression" in row else "expr"
        expression = str(row.get(key, "1'b0"))
        anchor = f"/derived_signals/{index}/{key}"
        found.append(_make_question(
            "C3", rows["C3"]["site_template"], "边界条件成立的判定是否采用当前表达式？",
            anchor, "current_boundary",
            [("inverse_boundary", "取反边界判定", _replace(anchor, f"!({expression})"))],
            required_construct="derived_boundary_signal"))

    domains = ir.get("clock_domains") or []
    reset = next(((i, row) for i, row in enumerate(domains)
                  if isinstance(row, dict) and row.get("reset_type")), None)
    if reset:
        index, row = reset
        current_type = str(row["reset_type"])
        other_type = "asynchronous" if current_type == "synchronous" else "synchronous"
        anchor = f"/clock_domains/{index}/reset_type"
        found.append(_make_question(
            "C4", rows["C4"]["site_template"], "复位应当是同步还是异步？",
            anchor, current_type,
            [(other_type, other_type, _replace(anchor, other_type))],
            required_construct="clock_domain_reset"))

    handshake = _handshake_split(ir)
    if handshake:
        index, key, valid_operand, _expression = handshake
        anchor = f"/derived_signals/{index}/{key}"
        found.append(_make_question(
            "C5", rows["C5"]["site_template"], "握手成立条件是否采用当前 valid/ready 组合？",
            anchor, "current_handshake",
            [("ignore_ready", "忽略 ready，valid 即成立",
              _replace(anchor, valid_operand))],
            required_construct="handshake_guard"))

    guarded = next(((i, row) for i, row in enumerate(transitions)
                    if isinstance(row, dict) and str(row.get("guard", "1")).strip().lower()
                    not in {"1", "1'b1", "true"}), None)
    if guarded:
        index, _ = guarded
        anchor = f"/transitions/{index}/guard"
        found.append(_make_question(
            "C6", rows["C6"]["site_template"], "条件不成立时保持，还是允许状态继续更新？",
            anchor, "guarded_hold",
            [("always_update", "不保持，继续更新", _replace(anchor, "1'b1"))],
            required_construct="guarded_transition"))

    numeric = next(((i, row) for i, row in enumerate(ir.get("states") or [])
                    if isinstance(row, dict) and isinstance(row.get("width"), int)), None)
    if numeric:
        index, row = numeric
        width = int(row["width"])
        anchor = f"/states/{index}/width"
        found.append(_make_question(
            "C8", rows["C8"]["site_template"], "状态位宽是否采用当前宽度？",
            anchor, f"width_{width}",
            [(f"width_{width + 1}", f"扩为 {width + 1} 位", _replace(anchor, width + 1))],
            required_construct="state_width"))

    # Scan-specific fallbacks cover points that have no naturally direct
    # template yet.  G2 rows are handed back to the existing restructuring
    # loop; G1 rows stop the freeze with an explicit schema carrier request.
    state_index = {str(row.get("name")): index
                   for index, row in enumerate(ir.get("states") or [])
                   if isinstance(row, dict)}
    port_index = {str(row.get("name")): index
                  for index, row in enumerate(ir.get("module", {}).get("ports") or [])
                  if isinstance(row, dict)}
    for point in problem_points or []:
        kind = str(point.get("kind", ""))
        node_name = str(point.get("ir_node", "")).rsplit(":", 1)[-1]
        if kind == "multiple_writers" and node_name in state_index:
            anchor = f"/states/{state_index[node_name]}"
            found.append(_make_question(
                "C1", rows["C1"]["site_template"],
                f"多个写入同时作用于 {node_name} 时，哪一个优先？",
                anchor, "implicit_order", [("explicit_priority", "显式优先级", [])],
                required_construct="priority[]", gap="G2", source_point=kind))
        elif kind == "partial_write" and node_name in state_index:
            anchor = f"/states/{state_index[node_name]}"
            found.append(_make_question(
                "C3", rows["C3"]["site_template"],
                f"对 {node_name} 的部分写入之外，其余位应保持还是重置？",
                anchor, "implicit_hold", [("explicit_hold", "显式保持", [])],
                required_construct="transitions[].else_action", gap="G2", source_point=kind))
        elif kind == "missing_else_path":
            pointer = str(point.get("ir_pointer", ""))
            match = re.match(r"^/transitions/(\d+)/else_action$", pointer)
            if not match:
                continue
            index = int(match.group(1))
            if index >= len(transitions) or not isinstance(transitions[index], dict):
                continue
            transition = transitions[index]
            candidate = _clear_on_false_candidate(ir, index)
            # The template only discovers the problem point and, when every
            # structural precondition is provable, offers one conservative
            # candidate.  Complex rewrites belong to the generic materializer.
            alternative_patch = candidate["patch"] if candidate else []
            anchors = candidate["anchors"] if candidate else [f"/transitions/{index}"]
            found.append(_make_question(
                "C6", rows["C6"]["site_template"],
                f"{transition.get('name', index)} 的 guard 不成立时，状态应保持还是清零？",
                anchors, "implicit_hold",
                [("reset_on_false", "条件不成立时清零", alternative_patch)],
                required_construct="transitions[].else_action",
                gap="direct" if alternative_patch else "G2", source_point=kind))

    unique: list[Question] = []
    seen = set()
    for question in (item for item in found if item is not None):
        key = (question.category, tuple(question.anchors), question.source_point)
        if key not in seen:
            unique.append(question)
            seen.add(key)
    return unique


def merge_deterministic_questions(existing: list[Question], candidates: list[Question],
                                  *, include_g1: bool = False) -> list[Question]:
    """Append non-duplicate structural questions in stable order."""
    result = list(existing)
    keys = {(q.category, tuple(q.anchors)) for q in existing}
    for question in candidates:
        key = (question.category, tuple(question.anchors))
        if key in keys or (question.gap_hypothesis.upper() == "G1" and not include_g1):
            continue
        result.append(question)
        keys.add(key)
    return result


def scan_problem_points(ir: dict[str, Any], graph: dict[str, Any]) -> list[dict[str, Any]]:
    """Deterministic structural scan; findings are candidates, not answers."""
    findings: list[dict[str, Any]] = []
    writers: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in graph.get("edges", []):
        if edge.get("relation") == "writes":
            writers[edge["target"]].append(edge)
            if edge.get("subtype") == "partial_unknown":
                findings.append({"kind": "partial_write", "ir_node": edge["target"],
                                 "source": edge["source"]})
    for target, edges in sorted(writers.items()):
        if len(edges) > 1:
            findings.append({"kind": "multiple_writers", "ir_node": target,
                             "sources": sorted(edge["source"] for edge in edges)})

    output_names = {str(port.get("name")) for port in (ir.get("module", {}).get("ports") or [])
                    if isinstance(port, dict) and port.get("direction") == "output"}
    derived = {str(item.get("name")) for item in ir.get("derived_signals") or []
               if isinstance(item, dict)}
    states = {str(item.get("name")) for item in ir.get("states") or []
              if isinstance(item, dict)}
    for name in sorted(output_names):
        node = f"IR:state:{name}" if name in states else f"IR:port:{name}"
        if name not in derived and node not in writers:
            findings.append({"kind": "output_without_driver", "ir_node": node})

    for index, transition in enumerate(ir.get("transitions") or []):
        if not isinstance(transition, dict):
            continue
        guard = str(transition.get("guard", "1")).strip().lower()
        if guard not in {"1", "1'b1", "true"} and not transition.get("else_action"):
            findings.append({"kind": "missing_else_path",
                             "ir_node": f"IR:transition:{transition.get('name')}",
                             "ir_pointer": f"/transitions/{index}/else_action"})

    # The shared schema has no read-during-write policy.  A memory object is
    # therefore itself an uncovered semantic point until a bypass structure or
    # an explicitly supported local extension carries the choice.
    for index, state in enumerate(ir.get("states") or []):
        if isinstance(state, dict) and str(state.get("type", "")).lower() in {"memory", "ram"}:
            findings.append({"kind": "memory_read_during_write_policy",
                             "ir_node": f"IR:state:{state.get('name')}",
                             "ir_pointer": f"/states/{index}",
                             "gap_type": "G1"})
    return findings


def build_question_graph(ir_graph: dict[str, Any], ir: dict[str, Any], questions,
                         verdicts: list[dict[str, Any]] | None = None,
                         problem_points: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    """Build only question relations; IR dependency edges remain in ir_graph.json."""
    by_id = {row.get("id"): row for row in (verdicts or [])}
    question_nodes, option_nodes, evidence_nodes = [], [], []
    relations = {"anchors_to": [], "changes": [], "depends_on": [], "evidenced_by": []}
    known_questions = {question.id for question in questions}

    for question in questions:
        qid = f"Q:{question.id}"
        row = by_id.get(question.id, {})
        question_nodes.append({
            "id": qid, "question_id": question.id, "category": question.category,
            "template": question.template, "text": question.text,
            "status": question.status, "fingerprint": question.inventory_fingerprint,
            "generated_by": question.generated_by,
        })
        resolved = resolve_anchor_nodes(ir_graph, ir, question.anchors)
        for pointer in question.anchors:
            relations["anchors_to"].append({"source": qid, "pointer": pointer,
                                             "targets": resolved})
        for option in question.options:
            oid = f"Q:{question.id}:O:{option.value}"
            option_nodes.append({"id": oid, "question_id": question.id,
                                 "value": option.value, "label": option.label,
                                 "current": option.value == question.current})
            paths = sorted({str(operation.get("path")) for operation in option.patch
                            if isinstance(operation, dict) and operation.get("path")})
            relations["changes"].append({"source": oid, "question": qid,
                                          "paths": paths,
                                          "validated_diff_paths": row.get("diff_paths", [])})
        conditional = {str(rule.get("question")): [str(value) for value in rule.get("values", [])]
                       for rule in question.active_when if isinstance(rule, dict)}
        for dependency in sorted(set(question.depends_on) | set(conditional)):
            if dependency in known_questions and dependency != question.id:
                relations["depends_on"].append({"source": qid,
                                                 "target": f"Q:{dependency}",
                                                 "when_values": conditional.get(dependency)})
        for index, excerpt in enumerate(question.evidence):
            eid = f"Q:{question.id}:E:{index}"
            evidence_nodes.append({"id": eid, "text": excerpt,
                                   "source": question.evidence_source})
            relations["evidenced_by"].append({"source": qid, "target": eid})

    problem_nodes = []
    for index, point in enumerate(problem_points or []):
        node = {"id": f"P:{index:03d}", **point, "question_ids": []}
        target = point.get("ir_node")
        pointer = str(point.get("ir_pointer", ""))
        for question in questions:
            resolved = resolve_anchor_nodes(ir_graph, ir, question.anchors)
            pointer_match = pointer and any(
                anchor == pointer or anchor.startswith(pointer.rstrip("/") + "/")
                or pointer.startswith(anchor.rstrip("/") + "/")
                for anchor in question.anchors)
            kind = str(point.get("kind", ""))
            construct = question.required_construct
            semantic_match = (
                question.source_point == kind and bool(kind)
                or (kind == "memory_read_during_write_policy" and question.category == "C7"
                    and construct in {"read_during_write_policy", "bypass_derived_signal"})
                or (kind == "multiple_writers" and question.category == "C1"
                    and construct == "priority[]")
                or (kind == "partial_write" and question.category == "C3"
                    and construct == "transitions[].else_action")
                or (kind == "output_without_driver" and construct == "output_driver")
            )
            typed_point = kind in {"memory_read_during_write_policy", "multiple_writers",
                                   "partial_write", "output_without_driver",
                                   "missing_else_path"}
            structural_match = pointer_match if pointer else target in resolved
            if semantic_match or (structural_match and not typed_point) or (
                    kind == "missing_else_path" and pointer_match):
                node["question_ids"].append(question.id)
        problem_nodes.append(node)

    return {
        "schema_version": "question-graph-1",
        "ir_graph_ref": "ir_graph.json",
        "question_nodes": question_nodes,
        "option_nodes": option_nodes,
        "evidence_nodes": evidence_nodes,
        "problem_nodes": problem_nodes,
        "relations": relations,
        "counts": {"questions": len(question_nodes), "options": len(option_nodes),
                   "problem_points": len(problem_nodes),
                   "uncovered_problem_points": sum(1 for node in problem_nodes
                                                   if not node["question_ids"]),
                   **{name: len(items) for name, items in relations.items()}},
    }


def to_mermaid(question_graph: dict[str, Any]) -> str:
    lines = ["flowchart LR"]
    for node in question_graph.get("question_nodes", []):
        key = _mermaid_id(node["id"])
        label = str(node.get("text") or node["id"]).replace('"', "'")[:60]
        lines.append(f'    {key}["{label}"]')
    for node in question_graph.get("option_nodes", []):
        key = _mermaid_id(node["id"])
        label = str(node.get("label") or node.get("value") or node["id"]).replace('"', "'")[:50]
        lines.append(f'    {key}(["{label}"])')
    for edge in question_graph.get("relations", {}).get("changes", []):
        lines.append(f"    {_mermaid_id(edge['question'])} -->|option| {_mermaid_id(edge['source'])}")
    for edge in question_graph.get("relations", {}).get("depends_on", []):
        lines.append(f"    {_mermaid_id(edge['target'])} -->|precedes| {_mermaid_id(edge['source'])}")
    return "\n".join(lines) + "\n"


def _mermaid_id(value: str) -> str:
    return "n" + "".join(character if character.isalnum() else "_" for character in value)


def dependency_map(question_graph: dict[str, Any]) -> dict[str, set[str]]:
    result: dict[str, set[str]] = defaultdict(set)
    for edge in question_graph.get("relations", {}).get("depends_on", []):
        source = str(edge.get("source", "")).removeprefix("Q:")
        target = str(edge.get("target", "")).removeprefix("Q:")
        if source and target:
            result[source].add(target)
    return result


def dependency_rules(question_graph: dict[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Return conditional and unconditional rules without breaking v1 callers."""
    result: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for edge in question_graph.get("relations", {}).get("depends_on", []):
        source = str(edge.get("source", "")).removeprefix("Q:")
        target = str(edge.get("target", "")).removeprefix("Q:")
        if source and target:
            values = edge.get("when_values")
            result[source].append({"question": target,
                                   "values": None if values is None else [str(v) for v in values]})
    return dict(result)
