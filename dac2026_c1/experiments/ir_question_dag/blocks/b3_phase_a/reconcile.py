"""Close the phase-A/phase-B feedback loop before IR_exp is frozen."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable

from blocks.b2_site_model.patches import load_ir
from blocks.b2_site_model.schema import Question
from blocks.b4_phase_b_graph.graph import build_ir_graph
from blocks.b4_phase_b_graph.questions import (build_question_graph,
                                                expand_ir_question_templates,
                                                merge_deterministic_questions,
                                                scan_problem_points)

from .closure import close_sites


def close_with_problem_feedback(
        ir_path: Path, questions: list[Question], ctx, config: dict, *, llm=None,
        close_fn: Callable[..., dict[str, Any]] | None = None,
        graph_builder: Callable[[Path], dict[str, Any]] | None = None,
        scanner: Callable[[dict[str, Any], dict[str, Any]], list[dict[str, Any]]] | None = None,
        expander: Callable[[dict[str, Any]], list[Question]] | None = None,
        ) -> dict[str, Any]:
    """Repeat closure and structural scanning until no problem point is open.

    The function deliberately returns ``closed=False`` on an unmapped or G1
    point.  Its caller may freeze IR_exp only when both representability and
    structural coverage are closed.
    """
    close_fn = close_fn or close_sites
    graph_builder = graph_builder or build_ir_graph
    scanner = scanner or scan_problem_points
    expander = expander or expand_ir_question_templates
    max_cycles = max(0, int(config.get("problem_feedback_rounds", 3)))
    current_ir = Path(ir_path)
    cycles: list[dict[str, Any]] = []

    for cycle in range(max_cycles + 1):
        closure = close_fn(current_ir, questions, ctx, config, llm=llm)
        if not closure.get("closed"):
            g1 = [row for row in closure.get("verdicts", [])
                  if row.get("capability", {}).get("gap_type") == "schema_extension"]
            status = "blocked_g1" if g1 else "representability_open"
            result = {**closure, "closed": False, "feedback_status": status,
                      "feedback_cycles": cycles, "g1_blockers": g1,
                      "uncovered_problem_points": None}
            ctx.write_json("phase_a/problem_feedback.json", result_for_json(result))
            ctx.event("problem_feedback_blocked", status=status, blockers=len(g1))
            return result

        current_ir = Path(closure["ir"])
        ir = load_ir(current_ir)
        graph = graph_builder(current_ir)
        points = scanner(ir, graph)
        qgraph = build_question_graph(graph, ir, questions, closure.get("verdicts"), points)
        uncovered = [node for node in qgraph.get("problem_nodes", [])
                     if not node.get("question_ids")]
        record: dict[str, Any] = {
            "cycle": cycle, "problem_points": len(points),
            "uncovered_problem_points": len(uncovered),
            "added_questions": [],
        }
        cycles.append(record)
        ctx.write_json(f"phase_a/problem_feedback_{cycle}.json",
                       {"record": record, "problem_points": points,
                        "question_graph": qgraph})
        if not uncovered:
            result = {**closure, "closed": True, "feedback_status": "closed",
                      "feedback_cycles": cycles, "g1_blockers": [],
                      "uncovered_problem_points": 0, "questions": questions}
            ctx.write_json("phase_a/problem_feedback.json", result_for_json(result))
            ctx.event("problem_feedback_closed", cycles=cycle + 1,
                      questions=len(questions))
            return result

        try:
            candidates = expander(ir, problem_points=points)
        except TypeError:
            # Test doubles and older local expanders may only accept the IR.
            candidates = expander(ir)
        trial = merge_deterministic_questions(questions, candidates, include_g1=True)
        new_questions = trial[len(questions):]
        trial_graph = build_question_graph(graph, ir, trial, closure.get("verdicts"), points)
        uncovered_ids = {node["id"] for node in uncovered}
        useful_ids = {qid for node in trial_graph.get("problem_nodes", [])
                      if node.get("id") in uncovered_ids
                      for qid in node.get("question_ids", [])}
        useful = [question for question in new_questions if question.id in useful_ids]
        if not useful:
            # A structural point with no actionable "modify" option is not a
            # question: the question tree only carries keep/change pairs.  The
            # point stays in the diagnostic scan but cannot block the freeze.
            record["block_reason"] = "no_actionable_question"
            record["unactionable"] = [node.get("id") or node.get("kind")
                                      for node in uncovered]
            result = {**closure, "closed": True,
                      "feedback_status": "closed_with_unactionable",
                      "feedback_cycles": cycles, "g1_blockers": [],
                      "uncovered_problem_points": 0,
                      "unactionable_problem_points": len(uncovered),
                      "questions": questions}
            ctx.write_json("phase_a/problem_feedback.json", result_for_json(result))
            ctx.event("problem_feedback_closed_with_unactionable",
                      points=len(uncovered))
            return result

        questions.extend(useful)
        record["added_questions"] = [question.id for question in useful]
        record["added_gap_types"] = [question.gap_hypothesis for question in useful]

    result = {**closure, "closed": False, "feedback_status": "round_limit",
              "feedback_cycles": cycles, "g1_blockers": [],
              "uncovered_problem_points": cycles[-1]["uncovered_problem_points"],
              "questions": questions}
    ctx.write_json("phase_a/problem_feedback.json", result_for_json(result))
    ctx.event("problem_feedback_blocked", status="round_limit")
    return result


def result_for_json(result: dict[str, Any]) -> dict[str, Any]:
    """Strip runtime-only values before writing a diagnostic artifact."""
    clean = {key: value for key, value in result.items()
             if key not in {"ir_text", "questions"}}
    if isinstance(clean.get("ir"), Path):
        clean["ir"] = str(clean["ir"])
    return clean
