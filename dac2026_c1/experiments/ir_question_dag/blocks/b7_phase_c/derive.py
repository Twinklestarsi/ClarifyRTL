"""Derive IR_final from the frozen IR_exp plus committed answers."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from blocks.b2_site_model.patches import (apply_patch, dump_ir, load_ir,
                                          resolve_pointer)
from blocks.b3_phase_a.checks import check_ir


def _priority_contract(questions, dialogue: dict[str, Any],
                       base_ir: dict[str, Any]) -> list[dict[str, Any]]:
    constraints = {str(item.get("question")): item
                   for item in dialogue.get("constraints") or []}
    states = dialogue.get("summary", {}).get("states", {})
    contract = []
    for question in questions:
        constraint = constraints.get(question.id)
        if question.category != "C1" or (
                constraint is None
                and states.get(question.id) not in {"answered", "resolved_without_ask"}):
            continue
        value = str(constraint.get("value", "")) if constraint else ""
        option = next((item for item in question.options
                       if item.value == value), None)
        expected_ir = base_ir
        build_error = ""
        if option is not None:
            try:
                expected_ir = apply_patch(base_ir, option.patch)
            except Exception as exc:  # noqa: BLE001 - recorded as a hard mismatch
                build_error = f"{type(exc).__name__}: {exc}"[:500]
        expected_anchors = {}
        for anchor in question.anchors:
            exists, anchor_value = resolve_pointer(expected_ir, anchor)
            expected_anchors[anchor] = {"exists": exists, "value": anchor_value}
        contract.append({"question": question.id, "value": value,
                         "anchors": question.anchors,
                         "option_exists": option is not None,
                         "option_patch": option.patch if option is not None else None,
                         "constraint_patch": constraint.get("patch") if constraint else None,
                         "expected_anchors": expected_anchors,
                         "build_error": build_error})
    return contract


def _call_check_conformance(ir_path: Path, contract: list[dict[str, Any]]) -> dict[str, Any]:
    """Deterministically verify selected C1 options against ``IR_final``."""
    if not contract:
        return {"ok": True, "status": "not_applicable",
                "reason": "no answered priority constraint"}
    final_ir = load_ir(ir_path)
    rows = []
    violations: list[str] = []
    for item in contract:
        question_id = str(item.get("question", ""))
        checks = []

        def record(name: str, ok: bool, detail: str = "") -> None:
            checks.append({"check": name, "ok": ok, "detail": detail})
            if not ok:
                violations.append(f"{question_id}:{name}:{detail}"[:800])

        option_exists = item.get("option_exists") is True
        record("selected_option_exists", option_exists,
               f"selected value={item.get('value')!r}")
        option_patch = item.get("option_patch")
        constraint_patch = item.get("constraint_patch")
        record("constraint_patch_matches_option",
               option_exists and isinstance(option_patch, list)
               and constraint_patch == option_patch,
               "constraint patch differs from the frozen option")
        record("option_patch_builds", not bool(item.get("build_error")),
               str(item.get("build_error", "")))

        for operation in option_patch if isinstance(option_patch, list) else []:
            matched, detail = _operation_is_in_final(final_ir, operation)
            record("option_patch_replayed", matched, detail)

        anchors = [str(anchor) for anchor in item.get("anchors") or []]
        option_paths = [str(operation.get("path", ""))
                        for operation in (option_patch or [])
                        if isinstance(operation, dict)]
        priority_paths = anchors + option_paths
        # C1 priority can be carried by the explicit /priority list, by the
        # order/action of a writer transition, or by an expression that encodes
        # the arbitration.  All three are real evidence; requiring /priority
        # alone rejected valid C1 answers produced by the audit.
        def is_priority_evidence(path: str) -> bool:
            return bool(
                path == "/priority" or path.startswith("/priority/")
                or re.match(r"^/transitions/\d+/(action|guard|else_action)", path)
                or re.match(r"^/derived_signals/\d+/(expression|expr)$", path)
                or re.match(r"^/sequential_processes/\d+/transitions", path))
        has_priority_path = any(is_priority_evidence(path) for path in priority_paths)
        has_priority_list = any(path == "/priority" or path.startswith("/priority/")
                                for path in priority_paths)
        record("priority_anchor_present", has_priority_path,
               "no C1 anchor or option patch path points into a priority-bearing object")
        for anchor in anchors:
            expected = (item.get("expected_anchors") or {}).get(anchor, {})
            exists, actual = resolve_pointer(final_ir, anchor)
            matched = (exists == bool(expected.get("exists"))
                       and (not exists or actual == expected.get("value")))
            record("priority_anchor_value", matched,
                   f"{anchor}: expected={expected.get('value')!r}, actual={actual!r}")

        if has_priority_list:
            priority = final_ir.get("priority")
            shape_ok = isinstance(priority, list) and all(
                isinstance(edge, dict) and edge.get("higher") and edge.get("lower")
                and edge.get("higher") != edge.get("lower") for edge in priority)
            record("priority_structure", shape_ok,
                   "priority must be a list of distinct higher/lower edges")
        rows.append({"question": question_id, "value": item.get("value"),
                     "checks": checks, "ok": all(check["ok"] for check in checks)})
    ok = not violations
    return {"ok": ok, "status": "conformant" if ok else "violated",
            "checker": "local_c1_priority_v1", "questions": rows,
            "violations": violations}


def _operation_is_in_final(ir: dict[str, Any], operation: dict[str, Any]) -> tuple[bool, str]:
    op = str(operation.get("op", ""))
    path = str(operation.get("path", ""))
    expected = operation.get("value")
    if not path:
        return False, "patch operation has no path"
    if op == "remove":
        exists, actual = resolve_pointer(ir, path)
        return not exists, f"{path}: expected removed, actual={actual!r}"
    if op not in {"add", "replace"}:
        return False, f"unsupported patch operation {op!r} at {path}"
    if op == "add" and path.endswith("/-"):
        exists, parent = resolve_pointer(ir, path[:-2] or "/")
        matched = exists and isinstance(parent, list) and expected in parent
        return matched, f"{path}: expected appended value={expected!r}"
    exists, actual = resolve_pointer(ir, path)
    return exists and actual == expected, (
        f"{path}: expected={expected!r}, actual={actual!r}")


def derive_final(ir_exp: Path, dialogue: dict[str, Any], questions, ctx) -> dict[str, Any]:
    from blocks.b1_scaffold.runctx import sha256_file

    expected_sha = str(dialogue.get("ir_exp_sha256") or "")
    if expected_sha and sha256_file(ir_exp) != expected_sha:
        raise RuntimeError(
            "IR_exp changed between answering and derivation; patches no longer "
            "point at the file they were materialized against")
    base = load_ir(ir_exp)
    committed = list(dialogue.get("committed_patches") or [])
    derived = apply_patch(base, committed)
    final_path = ctx.path("derived/ir_final.yaml")
    final_path.write_text(dump_ir(derived), encoding="utf-8")
    # Checkers may normalize their input in place.  They must not rewrite the
    # file the answers were applied to, or a patch can silently move to a
    # different object.  Validate a copy and keep IR_final as the exact replay
    # of IR_exp plus the committed patches.
    checks_input = ctx.path("derived/checks/ir_final_input.yaml")
    checks_input.write_text(final_path.read_text(encoding="utf-8"), encoding="utf-8")
    checks = check_ir(checks_input, ctx.directory / "derived" / "checks",
                      compile_smoke=False)
    checks["checked_file"] = str(checks_input.relative_to(ctx.directory))
    checks["final_file"] = str(final_path.relative_to(ctx.directory))
    checks["normalization_kept_out_of_final"] = (
        checks_input.read_text(encoding="utf-8") != final_path.read_text(encoding="utf-8"))
    ctx.write_json("derived/checks.json", checks)
    derived = load_ir(final_path)

    states = dialogue["summary"].get("states", {})
    coverage = []
    for question in questions:
        entry = {"id": question.id, "category": question.category,
                 "template": question.template, "state": states.get(question.id, "unknown"),
                 "anchors": question.anchors}
        if entry["state"] in {"answered", "resolved_without_ask"}:
            values = {}
            for anchor in question.anchors:
                ok, value = resolve_pointer(derived, anchor)
                values[anchor] = value if ok else None
            entry["anchor_values_in_final"] = values
            entry["implemented"] = all(value is not None for value in values.values())
        coverage.append(entry)
    ctx.write_json("derived/coverage.json", coverage)

    terminal = {"answered", "determined_by_spec", "free_choice",
                "resolved_without_ask", "pruned_inapplicable", "deferred",
                "unsupported", "requires_new_baseline"}
    residual = [entry for entry in coverage
                if entry["state"] in {"deferred", "unsupported", "requires_new_baseline"}]
    open_questions = [entry for entry in coverage if entry["state"] not in terminal]
    constraint_coverage = []
    for constraint in dialogue.get("constraints") or []:
        checks_for_constraint = []
        for operation in constraint.get("patch") or []:
            path = str(operation.get("path", ""))
            exists, value = resolve_pointer(derived, path)
            op = operation.get("op")
            expected = operation.get("value")
            matched = (not exists) if op == "remove" else (exists and value == expected)
            checks_for_constraint.append({"pointer": path, "operation": op,
                                          "expected": expected, "actual": value,
                                          "matched": matched})
        for binding in constraint.get("bindings") or []:
            path = str(binding.get("pointer", ""))
            exists, value = resolve_pointer(derived, path)
            expected_exists = bool(binding.get("exists"))
            expected = binding.get("value")
            matched = exists == expected_exists and (not exists or value == expected)
            checks_for_constraint.append({"pointer": path, "operation": "binding",
                                          "expected": expected, "actual": value,
                                          "matched": matched})
        constraint_coverage.append({"constraint": constraint.get("id"),
                                    "question": constraint.get("question"),
                                    "checks": checks_for_constraint,
                                    "covered": all(item["matched"] for item in checks_for_constraint)})
    ctx.write_json("derived/constraint_coverage.json",
                   {"constraints": constraint_coverage,
                    "covered": sum(1 for item in constraint_coverage if item["covered"]),
                    "total": len(constraint_coverage)})
    contract = _priority_contract(questions, dialogue, base)
    conformance = _call_check_conformance(final_path, contract)
    conformance["contract"] = contract
    ctx.write_json("derived/conformance.json", conformance)
    final_points: list[dict[str, Any]] = []
    new_points: list[dict[str, Any]] = []
    point_scan_status = "checked"
    try:
        from blocks.b4_phase_b_graph.graph import build_ir_graph
        from blocks.b4_phase_b_graph.questions import scan_problem_points

        final_graph = build_ir_graph(final_path)
        final_points = scan_problem_points(derived, final_graph)
        baseline_path = ctx.directory / "dag" / "problem_points.json"
        baseline = json.loads(baseline_path.read_text(encoding="utf-8")).get("points", []) \
            if baseline_path.is_file() else []
        old = {json.dumps(item, sort_keys=True, ensure_ascii=False) for item in baseline}
        new_points = [item for item in final_points
                      if json.dumps(item, sort_keys=True, ensure_ascii=False) not in old]
    except Exception as exc:  # noqa: BLE001 - recorded as an explicit unavailable check
        point_scan_status = f"unavailable:{type(exc).__name__}"
    ctx.write_json("derived/problem_points.json",
                   {"status": point_scan_status, "points": final_points,
                    "new_points": new_points, "new_count": len(new_points)})
    summary = {"questions": len(coverage),
               "answered": sum(1 for e in coverage if e["state"] == "answered"),
               "resolved_without_ask": sum(1 for e in coverage if e["state"] == "resolved_without_ask"),
               "residual": len(residual), "open": len(open_questions),
               "constraints": len(constraint_coverage),
               "constraints_covered": sum(1 for item in constraint_coverage if item["covered"]),
               "point_scan": point_scan_status,
               "new_problem_points": len(new_points),
               "conformance": conformance.get("status"),
               "conformance_ok": conformance.get("ok") is True,
               "checks": checks.get("status")}
    ctx.write_json("derived/residual.json", {"residual": residual,
                                              "open": open_questions,
                                              "summary": summary})
    ctx.event("ir_final_derived", **summary)
    return {"ir": final_path, "checks": checks, "summary": summary, "coverage": coverage}
