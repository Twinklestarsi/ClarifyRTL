"""The questioning loop: rank one question, answer it, apply, prune, repeat."""

from __future__ import annotations

import copy
from typing import Any

from blocks.b2_site_model.detect import check_candidate_text
from blocks.b2_site_model.patches import apply_patch, dump_ir, resolve_pointer
from blocks.b2_site_model.schema import Question
from blocks.b4_phase_b_graph.questions import dependency_map, dependency_rules
from blocks.b5_ranking.rank import rank

READY, BLOCKED = "ready", "blocked"
ANSWERED = "answered"
RESOLVED = "resolved_without_ask"
FREE = "free_choice"
INACTIVE = "pruned_inapplicable"
DEFERRED = "deferred"
UNSUPPORTED = "unsupported"
NEW_BASELINE = "requires_new_baseline"
OPEN = {READY, BLOCKED}
SATISFIED_DEPENDENCY = {ANSWERED, RESOLVED, FREE}


def _patch_for(question: Question, answer: dict[str, Any]) -> list[dict] | None:
    value = answer.get("value")
    for option in question.options:
        if option.value == value:
            return option.patch
    if value == "other" and isinstance(answer.get("replacement"), dict):
        replacement = answer["replacement"]
        pointer, new_value = replacement.get("pointer"), replacement.get("value")
        if isinstance(pointer, str) and pointer and new_value is not None:
            return [{"op": "replace", "path": pointer, "value": new_value}]
    return None


def _refresh_dependencies(states: dict[str, str], dependencies: dict[str, set[str]],
                          rules: dict[str, list[dict[str, Any]]] | None = None,
                          selections: dict[str, str] | None = None,
                          reasons: dict[str, str] | None = None) -> None:
    """Propagate prerequisite states to a fixed point.

    A deferred or unsupported prerequisite must never make its dependent ready.
    The graph currently expresses ordering, not answer-specific predicates, so
    ``pruned_inapplicable`` is only propagated when an upstream node is already
    known to be inapplicable.
    """
    rules = rules or {qid: [{"question": dep, "values": None} for dep in deps]
                      for qid, deps in dependencies.items()}
    selections = selections or {}
    reasons = reasons if reasons is not None else {}
    for _ in range(max(1, len(states))):
        changed = False
        for question_id, state in list(states.items()):
            if state not in OPEN:
                continue
            question_rules = rules.get(question_id, [])
            upstream = [states.get(str(rule.get("question")), UNSUPPORTED)
                        for rule in question_rules]
            rejected_rule = next((rule for rule in question_rules
                                  if rule.get("values") is not None
                                  and states.get(str(rule.get("question")))
                                  in SATISFIED_DEPENDENCY
                                  and selections.get(str(rule.get("question")))
                                  not in {str(value) for value in rule.get("values", [])}), None)
            unresolved_choice = any(
                rule.get("values") is not None
                and states.get(str(rule.get("question"))) in SATISFIED_DEPENDENCY
                and str(rule.get("question")) not in selections
                for rule in question_rules)
            if rejected_rule is not None:
                new_state = INACTIVE
                parent = str(rejected_rule.get("question"))
                reasons[question_id] = (
                    f"conditional dependency {parent}={selections.get(parent)!r} "
                    f"not in {list(rejected_rule.get('values') or [])!r}")
            elif unresolved_choice:
                new_state = BLOCKED
            if not upstream or all(item in SATISFIED_DEPENDENCY for item in upstream):
                if rejected_rule is None and not unresolved_choice:
                    new_state = READY
            elif NEW_BASELINE in upstream:
                new_state = NEW_BASELINE
                reasons[question_id] = "upstream dependency requires a new baseline"
            elif UNSUPPORTED in upstream:
                new_state = UNSUPPORTED
                reasons[question_id] = "upstream dependency is unsupported"
            elif DEFERRED in upstream:
                new_state = DEFERRED
                reasons[question_id] = "upstream dependency was deferred"
            elif INACTIVE in upstream:
                new_state = INACTIVE
                reasons[question_id] = "upstream dependency is inapplicable"
            else:
                new_state = BLOCKED
                reasons[question_id] = "waiting for upstream dependency"
            if new_state != state:
                states[question_id] = new_state
                changed = True
        if not changed:
            break


def _bindings(ir: dict[str, Any], question: Question) -> list[dict[str, Any]]:
    result = []
    for pointer in question.anchors:
        exists, value = resolve_pointer(ir, pointer)
        result.append({"pointer": pointer, "exists": exists, "value": value})
    return result


def _preserves(candidate: dict[str, Any], bindings: dict[str, tuple[bool, Any]]) -> bool:
    for pointer, expected in bindings.items():
        if resolve_pointer(candidate, pointer) != expected:
            return False
    return True


def _propagate_unique_options(derived: dict[str, Any], questions: list[Question],
                              states: dict[str, str], constraints: list[dict[str, Any]],
                              committed_patches: list[dict[str, Any]], ctx,
                              round_index: int, record: dict[str, Any],
                              selections: dict[str, str] | None = None,
                              reasons: dict[str, str] | None = None) -> dict[str, Any]:
    """Resolve a question only when committed bindings leave one viable option.

    Anchor overlap by itself is not enough: two questions can share a parent IR
    object while still changing independent fields.  Every surviving option is
    therefore applied and checked against all committed anchor bindings.
    """
    changed = True
    while changed:
        changed = False
        bound: dict[str, tuple[bool, Any]] = {}
        for constraint in constraints:
            for item in constraint.get("bindings") or []:
                bound[str(item["pointer"])] = (bool(item.get("exists")), item.get("value"))
        for question in questions:
            if states.get(question.id) != READY:
                continue
            # A conformance question is mandated by the spec, not negotiated:
            # other bindings must never prune the spec option away.
            if getattr(question, "generated_by", "") in {"spec_audit", "spec_conformance"}:
                continue
            viable: list[tuple[Any, dict[str, Any]]] = []
            rejected: list[str] = []
            for option in question.options:
                try:
                    candidate = apply_patch(derived, option.patch)
                except Exception:  # noqa: BLE001 - an inapplicable option is pruned
                    rejected.append(option.value)
                    continue
                if _preserves(candidate, bound):
                    viable.append((option, candidate))
                else:
                    rejected.append(option.value)
            if rejected:
                record.setdefault("pruned_by_constraint", {})[question.id] = rejected
            if len(viable) != 1:
                continue
            option, candidate = viable[0]
            check = check_candidate_text(
                dump_ir(candidate),
                ctx.directory / "rounds" / f"round_{round_index:03d}" /
                f"propagated_{question.id}")
            if not check.get("ok"):
                record.setdefault("propagation_failures", {})[question.id] = {
                    "stage": check.get("stage"), "error": check.get("error")}
                continue
            derived = candidate
            states[question.id] = RESOLVED
            if selections is not None:
                selections[question.id] = option.value
            if reasons is not None:
                reasons[question.id] = "only one option remains after constraint propagation"
            committed_patches.extend(copy.deepcopy(option.patch))
            constraint = {
                "id": f"C:{question.id}", "question": question.id,
                "value": option.value, "reason": "unique option after constraint propagation",
                "source": "determined_by_constraint", "patch": option.patch,
                "bindings": _bindings(derived, question),
            }
            constraints.append(constraint)
            record.setdefault("resolved_without_ask", []).append(question.id)
            changed = True
            break
    return derived


def run_dialogue(ir: dict[str, Any], questions: list[Question], graph: dict,
                 config: dict, answerer, ctx, *,
                 question_graph: dict[str, Any] | None = None) -> dict[str, Any]:
    horizon = int(config.get("horizon", 16))
    states = {q.id: (READY if q.status == "S0" else FREE if q.status == "D1" else UNSUPPORTED)
              for q in questions}
    dependencies = dependency_map(question_graph or {})
    rules = dependency_rules(question_graph or {})
    selections = {q.id: q.current for q in questions if states[q.id] == FREE and q.current}
    state_reasons = {q.id: q.reason for q in questions if q.reason}
    _refresh_dependencies(states, dependencies, rules, selections, state_reasons)
    derived = copy.deepcopy(ir)
    transcript: list[dict[str, Any]] = []
    constraints: list[dict[str, Any]] = []
    committed_patches: list[dict[str, Any]] = []
    asked = 0
    stop_reason = "no_open_questions"

    # No question budget: every ready question is asked.  A non-positive
    # max_questions/max_rounds means "unlimited" (the old defaults were 8/24).
    raw_limit = config.get("max_questions")
    question_limit = None if raw_limit is None else int(raw_limit)
    if question_limit is not None and question_limit <= 0:
        question_limit = None
    raw_rounds = config.get("max_rounds")
    round_limit = len(questions) + 8 if raw_rounds is None else int(raw_rounds)
    if round_limit <= 0:
        round_limit = len(questions) + 8
    for round_index in range(round_limit):
        _refresh_dependencies(states, dependencies, rules, selections, state_reasons)
        ready = [q for q in questions if states.get(q.id) == READY]
        if not ready:
            break
        if question_limit is not None and asked >= question_limit:
            stop_reason = "question_budget_exhausted"
            break
        # Spec-mandated conformance fixes go first: they are prerequisites, and
# running them before other answers keeps their patch anchored on the IR the
# question was built from.
        conformance_ready = [q for q in ready
                             if getattr(q, "generated_by", "") in
                             {"spec_audit", "spec_conformance"}]
        if conformance_ready:
            ranking = {"selected": conformance_ready[0].id,
                       "effective_method": "spec_conformance_first", "rows": []}
        else:
            ranking = rank(questions, graph, derived, config, round_index, states=states,
                           question_graph=question_graph)
        ctx.write_json(f"rounds/round_{round_index:03d}/ranking.json", ranking)
        selected_id = ranking["selected"]
        question = next(q for q in questions if q.id == selected_id)
        ctx.write_json(f"rounds/round_{round_index:03d}/question.json",
                       question.to_dict())
        answer = answerer.answer(question)
        asked += 1
        ctx.write_json(f"rounds/round_{round_index:03d}/answer.json", answer)
        value = str(answer.get("value") or "").strip()
        # ``other`` is allowed to carry a replacement; it is applied directly,
        # with the same candidate check as a frozen option.  No extra
        # rebaseline machinery is introduced here.
        patch = _patch_for(question, answer)
        record = {"round": round_index, "question": question.id, "answer": answer,
                  "applied": False, "ranking_method": ranking["effective_method"]}
        must_stop = False
        if value in {"", "undecided"}:
            states[question.id] = DEFERRED
            record["error"] = "answer deferred"
        elif value == "other" and patch is None:
            # No concrete replacement was supplied: skip this question instead
            # of terminating the whole dialogue.
            states[question.id] = DEFERRED
            record["error"] = "other answer without a replacement; question deferred"
        elif patch is None:
            states[question.id] = NEW_BASELINE
            record["error"] = "answer did not map to a frozen option"
            stop_reason = NEW_BASELINE
            must_stop = True
        else:
            try:
                candidate = apply_patch(derived, patch)
            except Exception as exc:  # noqa: BLE001 - a bad answer must not kill the run
                states[question.id] = DEFERRED
                record["error"] = f"patch does not apply: {exc}"[:300]
                candidate = None
                check = {"ok": False, "stage": "patch"}
            if candidate is not None:
                check = check_candidate_text(dump_ir(candidate),
                                             ctx.directory / "rounds" /
                                             f"round_{round_index:03d}" / "candidate")
            if candidate is not None and not check.get("ok"):
                states[question.id] = DEFERRED
                record["error"] = f"candidate failed {check.get('stage')}: {check.get('error', '')[:300]}"
            elif candidate is not None:
                derived = candidate
                states[question.id] = ANSWERED
                selections[question.id] = value
                state_reasons[question.id] = "answered with a compiled option"
                record["applied"] = True
                record["option"] = answer.get("value")
                constraint = {"id": f"C:{question.id}", "question": question.id,
                              "value": value, "reason": answer.get("reason"),
                              "source": answer.get("source"), "patch": patch,
                              "bindings": _bindings(derived, question)}
                constraints.append(constraint)
                committed_patches.extend(copy.deepcopy(patch))
                try:
                    from blocks.b4_phase_b_graph.graph import build_ir_graph

                    candidate_path = (ctx.directory / "rounds" /
                                      f"round_{round_index:03d}" / "candidate" / "ir.yaml")
                    graph = build_ir_graph(candidate_path)
                except Exception:  # noqa: BLE001 - ranking can retain the frozen graph
                    record["graph_refresh"] = "failed"
        # Answer predicates must take effect before constraint propagation, or
        # a conditionally inapplicable blocked question could be resolved by
        # anchor overlap before it is pruned.
        _refresh_dependencies(states, dependencies, rules, selections, state_reasons)
        if record["applied"] and bool(config.get("enable_pruning", True)):
            derived = _propagate_unique_options(
                derived, questions, states, constraints, committed_patches,
                ctx, round_index, record, selections, state_reasons)
        _refresh_dependencies(states, dependencies, rules, selections, state_reasons)
        record["states"] = dict(states)
        record["state_reasons"] = dict(state_reasons)
        transcript.append(record)
        ctx.write_json(f"rounds/round_{round_index:03d}/propagation.json", record)
        ctx.event("answer_applied", question=question.id,
                  option=answer.get("value"), applied=record["applied"],
                  pruned=record.get("resolved_without_ask", []))
        if must_stop:
            break

    if any(state in OPEN for state in states.values()) and stop_reason == "no_open_questions":
        stop_reason = "max_rounds_exhausted"
    for question_id, state in list(states.items()):
        if state in OPEN:
            states[question_id] = DEFERRED
    residual_states = {DEFERRED, UNSUPPORTED, NEW_BASELINE}
    residual = [question_id for question_id, state in states.items()
                if state in residual_states]

    summary = {
        "asked": asked,
        "answered": sum(1 for value in states.values() if value == ANSWERED),
        "resolved_without_ask": sum(1 for value in states.values() if value == RESOLVED),
        "free_choice": sum(1 for value in states.values() if value == FREE),
        "pruned_inapplicable": sum(1 for value in states.values() if value == INACTIVE),
        "deferred": sum(1 for value in states.values() if value == DEFERRED),
        "unsupported": sum(1 for value in states.values() if value == UNSUPPORTED),
        "requires_new_baseline": sum(1 for value in states.values() if value == NEW_BASELINE),
        "open": sum(1 for value in states.values() if value in OPEN),
        "residual": residual,
        "stop_reason": stop_reason,
        "states": states,
        "state_reasons": state_reasons,
        "selected_values": selections,
        "transcript": transcript,
    }
    ctx.write_json("rounds/summary.json", {k: v for k, v in summary.items() if k != "transcript"})
    ctx.write_json("rounds/terminal_states.json",
                   {"states": states, "state_reasons": state_reasons,
                    "selected_values": selections, "residual": residual,
                    "stop_reason": stop_reason})
    ctx.write_json("rounds/constraints.json", {"constraints": constraints})
    return {"ir": derived, "summary": summary, "constraints": constraints,
            "committed_patches": committed_patches, "graph": graph}
