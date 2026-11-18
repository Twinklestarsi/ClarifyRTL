"""Question value features and the round ranking.

Scores are auditable structural proxies with fixed denominators.  A missing
feature is recorded and forces the whole round to a seeded random order; it is
never treated as zero.
"""

from __future__ import annotations

import random
import tempfile
from pathlib import Path
from typing import Any

from blocks.b2_site_model.patches import apply_patch, dump_ir
from blocks.b4_phase_b_graph.graph import influence_reachable, resolve_anchor_nodes

# Explicit fallback profile used only when a caller omits the configured
# weights.  D and C stay disabled until their corresponding checks are enabled.
DEFAULT_WEIGHTS = {"O": 0.70, "D": 0.00, "U": 0.30, "C": 0.00}


def structural_reach(question, graph, ir, horizon: int = 16) -> dict[str, Any]:
    anchors = resolve_anchor_nodes(graph, ir, question.anchors)
    outputs = {node["id"] for node in graph.get("nodes", [])
               if "output" in (node.get("attributes", {}).get("roles") or [])}
    if not anchors or not outputs:
        return {"value": None, "status": "unknown",
                "reason": "anchor did not resolve to graph nodes"}
    distance = influence_reachable(graph, anchors, horizon)
    reached = sorted(node for node in outputs if node in distance)
    return {"value": len(reached) / len(outputs), "status": "checked_structural_reachability",
            "anchors": anchors, "outputs_reached": reached,
            "denominator": len(outputs)}


def unlock(question, questions, states: dict[str, str], *, denominator: int = 8) -> dict[str, Any]:
    """Potential downstream questions unlocked by at least one answer option."""
    affected = []
    for other in questions:
        if other.id == question.id or states.get(other.id) != "blocked":
            continue
        conditional = {str(rule.get("question")) for rule in other.active_when
                       if isinstance(rule, dict)}
        dependencies = set(other.depends_on) | conditional
        if (question.id in dependencies
                and all(item == question.id
                        or states.get(item) in {"answered", "resolved_without_ask", "free_choice"}
                        for item in dependencies)):
            affected.append(other.id)
    denominator = max(1, int(denominator))
    return {"value": min(1.0, len(affected) / denominator),
            "status": "checked_dependencies",
            "count": len(affected), "affected": affected, "denominator": denominator}


def cost(question, *, denominator: int = 40) -> dict[str, Any]:
    operations = sum(len(_option_patch(question, value)) for value in _values(question))
    denominator = max(1, int(denominator))
    return {"value": min(1.0, operations / denominator),
            "status": "checked_patch_size", "operations": operations,
            "denominator": denominator}


def _values(question) -> list[str]:
    return [option.value for option in question.options]


def _option_patch(question, value: str) -> list[dict]:
    for option in question.options:
        if option.value == value:
            return option.patch or []
    return []


def bounded_behavior(ir: dict[str, Any], question, *, horizon: int,
                     budget_ms: int) -> dict[str, Any]:
    """Compare every non-current option with the current IR within K cycles."""
    from Compiler.rtl_compiler.parser import parse_ir
    from assumption_modeling.witness import bounded_impact

    alternatives = []
    try:
        with tempfile.TemporaryDirectory(prefix="irqdag_rank_") as temporary:
            directory = Path(temporary)
            current_path = directory / "current.yaml"
            current_path.write_text(dump_ir(ir), encoding="utf-8")
            current = parse_ir(current_path)
            for index, option in enumerate(question.options):
                candidate = apply_patch(ir, option.patch)
                if candidate == ir:
                    continue
                candidate_path = directory / f"option_{index}.yaml"
                candidate_path.write_text(dump_ir(candidate), encoding="utf-8")
                alternative = parse_ir(candidate_path)
                outcome = bounded_impact(
                    current, alternative, question.id, option.value,
                    horizon=horizon, budget_ms=budget_ms)
                output_queries = [item for item in outcome.get("queries", [])
                                  if str(item.get("observable", "")).startswith(
                                      ("raw_output:", "output:"))]
                distinguishable = any(item.get("status") == "sat" for item in output_queries)
                completed = bool(output_queries) and outcome.get("status") == "completed" and all(
                    item.get("status") in {"sat", "unsat"} for item in output_queries)
                alternatives.append({"value": option.value, "status": outcome.get("status"),
                                     "distinguishable": distinguishable,
                                     "completed": completed,
                                     "output_queries": len(output_queries),
                                     "reason": outcome.get("reason")})
    except Exception as exc:  # noqa: BLE001 - missing solver support is an explicit feature gap
        return {"value": None, "status": "unknown",
                "reason": f"{type(exc).__name__}: {exc}"[:300],
                "horizon": horizon, "budget_ms": budget_ms}
    if not alternatives:
        return {"value": 0.0, "status": "checked_no_alternative_change",
                "alternatives": [], "denominator": 0,
                "horizon": horizon, "budget_ms": budget_ms}
    if any(not item["completed"] for item in alternatives):
        return {"value": None, "status": "unknown",
                "reason": "at least one alternative was not decided within the solver budget",
                "alternatives": alternatives, "denominator": len(alternatives),
                "horizon": horizon, "budget_ms": budget_ms}
    distinct = sum(1 for item in alternatives if item["distinguishable"])
    return {"value": distinct / len(alternatives), "status": "checked_bounded_impact",
            "distinguishable": distinct, "alternatives": alternatives,
            "denominator": len(alternatives), "horizon": horizon,
            "budget_ms": budget_ms,
            "meaning": "fraction of alternatives observable at an output within K cycles"}


def rank(questions, graph, ir, config, round_index: int, *,
         states: dict[str, str] | None = None,
         question_graph: dict[str, Any] | None = None) -> dict[str, Any]:
    states = states or {}
    raw_weights = config.get("weights", {})
    weights = {name: float(raw_weights.get(name, default))
               for name, default in DEFAULT_WEIGHTS.items()}
    if any(value < 0 for value in weights.values()) or not any(weights.values()):
        raise ValueError("ranking weights must be non-negative and not all zero")
    ready = [q for q in questions if states.get(q.id, "ready") == "ready"]
    rows = []
    missing: list[str] = []
    for question in ready:
        reach = structural_reach(question, graph, ir,
                                 int(config.get("horizon", 16)))
        share = unlock(question, questions, states,
                       denominator=int(config.get("unlock_denominator", 8)))
        size = cost(question, denominator=int(config.get("cost_operation_denominator", 40)))
        behavior = (bounded_behavior(
            ir, question, horizon=int(config.get("horizon", 16)),
            budget_ms=int(config.get("solver_budget_ms", 2000)))
                    if float(weights.get("D", 0.0)) != 0 else
                    {"value": None, "status": "disabled", "reason": "weight is zero"})
        features = {"O": reach, "D": behavior, "U": share, "C": size}
        for name, feature in features.items():
            if weights[name] != 0 and feature.get("value") is None:
                missing.append(f"{question.id}:{name}")
        row = {"id": question.id, "category": question.category,
               "template": question.template, **features}
        required_missing = any(weights[name] != 0
                               and feature.get("value") is None
                               for name, feature in features.items())
        numeric = {name: (0.0 if weights[name] == 0 else float(feature["value"]))
                   for name, feature in features.items()
                   if not required_missing}
        row["score"] = (None if required_missing else
                        weights["O"] * numeric["O"]
                        + weights["D"] * numeric["D"]
                        + weights["U"] * numeric["U"]
                        - weights["C"] * numeric["C"])
        rows.append(row)

    method = config.get("method", "ir_structural_proxy")
    fallback_reason = ""
    if method not in {"random", "fixed"} and missing:
        method = "random"
        fallback_reason = "missing required features: " + ", ".join(missing[:6])
    if method == "fixed":
        rows.sort(key=lambda row: row["id"])
        field, sign = "id", 1
    elif method == "random":
        random.Random(f"{config.get('fallback', 'seeded_random')}:{round_index}").shuffle(rows)
        field, sign = None, 1
    else:
        field, sign = "score", -1
        rows.sort(key=lambda row: (sign * (row["score"] if row["score"] is not None else 1.0),
                                   row["id"]))
    scores = [row["score"] for row in rows if row.get("score") is not None]
    counts = {score: scores.count(score) for score in set(scores)}
    tied = sum(count for count in counts.values() if count > 1)
    return {"requested_method": config.get("method"), "effective_method": method,
            "round": round_index, "rows": rows, "fallback_reason": fallback_reason,
            "weights": weights, "selected": rows[0]["id"] if rows else None,
            "tied_candidates": tied,
            "tie_rate": tied / len(rows) if rows else 0.0,
            "tie_break": "question id",
            "question_graph": bool(question_graph),
            "normalization": {
                "O": "reachable outputs / all module outputs",
                "D": "distinguishable changed options / all changed options",
                "U": f"unlockable downstream questions / {int(config.get('unlock_denominator', 8))}",
                "C": f"patch operations / {int(config.get('cost_operation_denominator', 40))}",
            },
            "score_meaning": "auditable weighted proxy; unavailable weighted features force round fallback"}
