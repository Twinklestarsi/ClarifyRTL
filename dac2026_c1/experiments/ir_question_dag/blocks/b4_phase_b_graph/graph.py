"""Typed IR dependency graph and anchor resolution.

The graph is the block-2 ``build_graph`` output (ports, states, derived
signals, guards, actions, schedules, clock/reset) with no requirements or
assumptions attached: it describes the implementation only.
"""

from __future__ import annotations

import re
from collections import defaultdict, deque
from pathlib import Path
from typing import Any

_POINTER = re.compile(r"^/([^/]+)(?:/(\d+))?(?:/([^/]+))?(?:/(\d+))?")
_IDENTIFIER = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")
_KEYWORDS = {
    "and", "or", "not", "if", "else", "begin", "end", "case", "endcase",
    "posedge", "negedge", "wire", "reg", "logic", "signed", "unsigned",
}


def build_ir_graph(ir_path: Path, *, ir_hash: str = "", spec_hash: str = "") -> dict[str, Any]:
    from assumption_modeling.graph import build_graph
    from Compiler.rtl_compiler.parser import parse_ir

    model = parse_ir(ir_path)
    metadata = {"ir_hash": ir_hash, "spec_hash": spec_hash, "rtl_hash": "",
                "scope": "ir_structure_only"}
    graph = build_graph(model, [], [], {}, metadata)
    add_inferred_dependencies(graph, model)
    return graph


def unsupported_elements(graph: dict[str, Any]) -> set[str]:
    """Nodes whose expression the deterministic parser could not resolve."""
    coverage = graph.get("coverage") or {}
    return {str(row.get("element_id")) for row in coverage.get("unsupported_expressions") or []
            if isinstance(row, dict) and row.get("element_id")}


def add_inferred_dependencies(graph: dict[str, Any], model: Any) -> dict[str, Any]:
    """Recover missing ``reads`` edges as explicitly *inferred* structure.

    ``build_graph`` extracts dependencies with a deliberately restricted Verilog
    AST; when an expression uses a parameterized select, a replication count or
    a module parameter, it raises and adds *no* edge for that element.  The
    missing edges make a legal IR look disconnected, so for exactly those
    elements a lexical symbol scan adds fallback edges.

    These edges are marked ``verification_state='inferred'`` (origin
    ``structural_fallback``).  They never satisfy an observability proof on
    their own: :func:`structural_observability` returns ``known=False`` when a
    path exists only through them.  A token that is a declared symbol is a read
    of that symbol, so the fallback cannot invent a dependency; it merely
    recovers one the restricted parser refused to express.
    """
    from Compiler.rtl_compiler.backend import _derived_expression

    unsupported = unsupported_elements(graph)
    if not unsupported:
        return graph
    symbols: dict[str, str] = {}
    state_names = {state.name for state in getattr(model, "states", []) or []}
    for port in getattr(model, "ports", []) or []:
        kind = "state" if port.name in state_names else "port"
        symbols[port.name] = f"IR:{kind}:{port.name}"
    for state in getattr(model, "states", []) or []:
        symbols[state.name] = f"IR:state:{state.name}"
    for signal in getattr(model, "derived_signals", []) or []:
        symbols[str(signal.get("name"))] = f"IR:derived:{signal.get('name')}"
    existing = {(edge.get("source"), edge.get("target"), edge.get("relation"))
                for edge in graph.get("edges", [])}

    def reads(identifier: str, expression: Any) -> None:
        if identifier not in unsupported or expression is None:
            return
        for token in sorted(set(_IDENTIFIER.findall(str(expression)))):
            if token in _KEYWORDS:
                continue
            target = symbols.get(token)
            if not target or target == identifier:
                continue
            key = (identifier, target, "reads")
            if key in existing:
                continue
            graph.setdefault("edges", []).append({
                "source": identifier, "target": target, "relation": "reads",
                "subtype": "expression_scan", "origin": "structural_fallback",
                "verification_state": "inferred", "evidence_ids": [],
                "snapshot_visibility": "G0"})
            existing.add(key)

    for signal in getattr(model, "derived_signals", []) or []:
        reads(f"IR:derived:{signal.get('name')}", _derived_expression(signal))
    for transition in getattr(model, "transitions", []) or []:
        name = transition.name
        reads(f"IR:guard:{name}", transition.guard)
        for index, action in enumerate(transition.actions):
            reads(f"IR:action:{name}:{index}", action.expression)
        for index, action in enumerate(transition.else_actions):
            reads(f"IR:else_action:{name}:{index}", action.expression)
    return graph


def adjacency(graph: dict[str, Any]) -> dict[str, list[str]]:
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in graph.get("edges", []):
        outgoing[edge["source"]].append(edge["target"])
    return outgoing


def influence_adjacency(graph: dict[str, Any], *,
                        verified_only: bool = True) -> dict[str, list[str]]:
    """Orient dependency edges from a changed value toward its consumers.

    ``verified_only`` keeps only edges the deterministic graph builder proved;
    ``inferred`` fallback edges are excluded and can therefore never be the
    sole basis of an observability claim.
    """
    outgoing: dict[str, list[str]] = defaultdict(list)
    for edge in graph.get("edges", []):
        if verified_only and edge.get("verification_state", "verified") != "verified":
            continue
        source, target = edge["source"], edge["target"]
        relation = edge.get("relation")
        if relation in {"reads", "depends-on", "depends_on"}:
            outgoing[target].append(source)
        elif relation in {"writes", "contains"}:
            outgoing[source].append(target)
    return outgoing


def output_nodes(graph: dict[str, Any]) -> set[str]:
    outputs = set()
    for node in graph.get("nodes", []):
        roles = node.get("attributes", {}).get("roles") or []
        if "output" in roles:
            outputs.add(node["id"])
    return outputs


def reachable(graph: dict[str, Any], anchors: list[str], horizon: int = 16) -> dict[str, int]:
    """BFS over data/control edges; returns node -> shortest distance."""
    outgoing = adjacency(graph)
    distance: dict[str, int] = {}
    queue = deque((anchor, 0) for anchor in anchors)
    while queue:
        node, depth = queue.popleft()
        if node in distance or depth > horizon:
            continue
        distance[node] = depth
        for child in outgoing.get(node, ()):
            if child not in distance:
                queue.append((child, depth + 1))
    return distance


def influence_reachable(graph: dict[str, Any], anchors: list[str],
                        horizon: int = 16, *,
                        verified_only: bool = True) -> dict[str, int]:
    outgoing = influence_adjacency(graph, verified_only=verified_only)
    distance: dict[str, int] = {}
    queue = deque((anchor, 0) for anchor in anchors)
    while queue:
        node, depth = queue.popleft()
        if node in distance or depth > horizon:
            continue
        distance[node] = depth
        queue.extend((child, depth + 1) for child in outgoing.get(node, ())
                     if child not in distance)
    return distance


def anchor_to_nodes(ir: dict[str, Any], anchor: str) -> list[str]:
    """Map an IR pointer prefix to graph node identifiers."""
    port_match = re.match(r"^/module/ports/(\d+)", anchor)
    if port_match:
        ports = ir.get("module", {}).get("ports") or []
        index = int(port_match.group(1))
        if index < len(ports):
            return [f"IR:port:{ports[index].get('name')}"]
    match = _POINTER.match(anchor)
    if not match:
        return []
    section, first, field, _ = match.groups()
    nodes: list[str] = []
    if section == "states":
        states = ir.get("states") or []
        index = _as_index(first)
        if index is not None and index < len(states):
            name = states[index].get("name")
            nodes.append(f"IR:state:{name}")
            if field == "reset_value":
                nodes.append(f"IR:state:{name}")
    elif section == "transitions":
        transitions = ir.get("transitions") or []
        index = _as_index(first)
        if index is not None and index < len(transitions):
            name = transitions[index].get("name")
            nodes += [f"IR:transition:{name}", f"IR:guard:{name}"]
            if field in ("action", "else_action", "guard"):
                nodes.append(f"IR:{'action' if field != 'else_action' else 'else_action'}:{name}:0")
    elif section == "priority":
        nodes.append("IR:schedule:*")
    elif section == "derived_signals":
        signals = ir.get("derived_signals") or []
        index = _as_index(first)
        if index is not None and index < len(signals):
            nodes.append(f"IR:derived:{signals[index].get('name')}")
    elif section == "clock_domains":
        domains = ir.get("clock_domains") or []
        index = _as_index(first)
        if index is not None and index < len(domains):
            name = domains[index].get("name")
            nodes += [f"IR:clock:{name}", f"IR:reset:{name}"]
    return nodes


def _generate_anchor_nodes(graph: dict[str, Any], ir: dict[str, Any],
                           anchor: str) -> list[str]:
    """Map a ``/generate/<block>/emit/<item>/<kind>`` anchor to instances.

    A generate template describes every expanded object it emits, so the
    question's site is the set of those objects.  The name template's braced
    placeholders become one identifier component; matching is exact on the
    elaborated node name (no substring or case folding).
    """
    parts = [token.replace("~1", "/").replace("~0", "~")
             for token in anchor.strip("/").split("/")]
    if len(parts) < 4 or parts[0] != "generate" or parts[2] != "emit":
        return []
    try:
        block = (ir.get("generate") or [])[int(parts[1])]
        item = (block.get("emit") or [])[int(parts[3])]
    except (ValueError, IndexError, TypeError, AttributeError):
        return []
    if not isinstance(item, dict):
        return []
    known = {node["id"] for node in graph.get("nodes", [])}
    kinds = {"derived_signal": "derived", "transition": "transition",
             "state": "state"}
    for key, subtype in kinds.items():
        body = item.get(key)
        if not isinstance(body, dict) or not body.get("name"):
            continue
        pattern = "".join(
            "[A-Za-z0-9_]+" if part.startswith("{") else re.escape(part)
            for part in re.split(r"(\{[^}]*\})", str(body["name"])))
        prefix = f"IR:{subtype}:"
        return sorted(node for node in known
                      if node.startswith(prefix)
                      and re.fullmatch(pattern, node[len(prefix):]))
    return []


def _as_index(token: str | None) -> int | None:
    if token is None:
        return None
    try:
        return int(token)
    except (TypeError, ValueError):
        return None


def resolve_anchor_nodes(graph: dict[str, Any], ir: dict[str, Any],
                         anchors: list[str]) -> list[str]:
    """Best-effort mapping from pointer anchors to existing graph node ids."""
    known = {node["id"] for node in graph.get("nodes", [])}
    resolved: list[str] = []
    for anchor in anchors:
        if anchor.startswith("/generate/"):
            resolved.extend(node for node in _generate_anchor_nodes(graph, ir, anchor)
                            if node in known)
            continue
        for node in anchor_to_nodes(ir, anchor):
            if node in known:
                resolved.append(node)
            elif node.endswith("*"):
                prefix = node[:-1]
                resolved.extend(sorted(item for item in known if item.startswith(prefix)))
    return sorted(dict.fromkeys(resolved))


def graph_stats(graph: dict[str, Any]) -> dict[str, int]:
    kinds: dict[str, int] = defaultdict(int)
    for node in graph.get("nodes", []):
        kinds[f"{node.get('type')}:{node.get('subtype')}"] += 1
    return {"nodes": len(graph.get("nodes", [])), "edges": len(graph.get("edges", [])),
            **{f"kind:{k}": v for k, v in sorted(kinds.items())}}


def structural_observability(graph: dict[str, Any], ir: dict[str, Any], anchors: list[str],
                             *, horizon: int = 16) -> dict[str, Any]:
    """Prove a structural path from an anchored choice to a module output.

    ``known=False`` is different from ``observable=False``: the former means
    the anchor could not be mapped and must not silently satisfy a required
    observability check.
    """
    starts = resolve_anchor_nodes(graph, ir, anchors)
    outputs = output_nodes(graph)
    if not starts or not outputs:
        return {"known": False, "observable": None, "anchors": starts,
                "outputs": sorted(outputs), "horizon": horizon,
                "reason": "anchor_or_output_not_mapped"}
    verified = influence_reachable(graph, starts, horizon=horizon,
                                   verified_only=True)
    reached = sorted(node for node in outputs if node in verified)
    if reached:
        return {"known": True, "observable": True, "anchors": starts,
                "outputs": sorted(outputs), "reached_outputs": reached,
                "min_distance": min((verified[node] for node in reached), default=None),
                "horizon": horizon, "basis": "verified", "reason": ""}
    inferred = influence_reachable(graph, starts, horizon=horizon,
                                   verified_only=False)
    reached_inferred = sorted(node for node in outputs if node in inferred)
    unresolved = sorted(set(starts) & unsupported_elements(graph))
    if reached_inferred or unresolved:
        return {"known": False, "observable": None, "anchors": starts,
                "outputs": sorted(outputs), "reached_outputs": [],
                "inferred_outputs": reached_inferred,
                "unresolved_anchors": unresolved,
                "horizon": horizon, "basis": "inferred",
                "reason": "inferred_or_unparsed_dependency"}
    return {"known": True, "observable": False, "anchors": starts,
            "outputs": sorted(outputs), "reached_outputs": [],
            "min_distance": None, "horizon": horizon, "basis": "verified",
            "reason": "no_output_path_within_horizon"}


def attach_source_map(graph: dict[str, Any], source_map: dict[str, Any]) -> dict[str, Any]:
    """Add explicit IR --implemented-by--> RTL region links."""
    known = {node["id"] for node in graph.get("nodes", [])}
    nodes = graph.setdefault("nodes", [])
    edges = graph.setdefault("edges", [])
    seen_edges = {(edge.get("source"), edge.get("target"), edge.get("relation"))
                  for edge in edges}
    for region in source_map.get("regions", []):
        region_id = str(region.get("region_id", ""))
        if not region_id:
            continue
        if region_id not in known:
            nodes.append({"id": region_id, "type": "RTL", "subtype": region.get("role"),
                          "text": f"RTL lines {region.get('line_start')}-{region.get('line_end')}",
                          "attributes": {"line_start": region.get("line_start"),
                                         "line_end": region.get("line_end"),
                                         "precision": region.get("precision")}})
            known.add(region_id)
        for ir_id in region.get("ir_ids", []):
            key = (ir_id, region_id, "implemented-by")
            if ir_id in known and key not in seen_edges:
                edges.append({"source": ir_id, "target": region_id,
                              "relation": "implemented-by"})
                seen_edges.add(key)
    graph.setdefault("metadata", {})["source_map_schema"] = source_map.get("schema_version")
    return graph


def to_mermaid(graph: dict[str, Any], limit: int = 40) -> str:
    lines = ["flowchart LR"]
    nodes = graph.get("nodes", [])[:limit]
    ids = {node["id"] for node in nodes}
    for node in nodes:
        label = str(node.get("text", node["id"])).replace('"', "'")[:40]
        lines.append(f'    n{abs(hash(node["id"])) % 10**8}["{label}"]')
    index = {node["id"]: f"n{abs(hash(node['id'])) % 10**8}" for node in nodes}
    for edge in graph.get("edges", []):
        if edge["source"] in ids and edge["target"] in ids:
            lines.append(f'    {index[edge["source"]]} -->|{edge["relation"]}| {index[edge["target"]]}')
    return "\n".join(lines)
