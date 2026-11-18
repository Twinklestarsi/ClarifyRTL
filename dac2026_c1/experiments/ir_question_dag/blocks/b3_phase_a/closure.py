"""Phase A closure: every question must own a site before the IR is frozen.

Order: detect -> (optional, behaviour-preserving) restructure -> rebuild IR ->
re-run checks -> detect again.  The frozen artifact is copied *after* the
checkers have rewritten their input.
"""

from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

from blocks.b2_site_model.capability import (assess, load_matrix,
                                             option_signature)
from blocks.b2_site_model.detect import detect_site, summarise
from blocks.b2_site_model.patches import dump_ir, load_ir
from blocks.b2_site_model.schema import Option, Question, S0
from blocks.b4_phase_b_graph.graph import build_ir_graph, structural_observability

from .checks import check_ir
from .inventory import validate_question

RESTRUCTURE_SYSTEM = """你是硬件 IR 的重构工具。目标：让一个问题在这份 IR 里有“可指的位置”。

规则：
- 只改变表达方式，不改变当前行为；重构前后编译出的 Verilog 必须逐字相同。
- 允许：把一条 transition 里的三元表达式拆成多条带 guard 的 transition 并补 priority；
  把隐式的“保持”写成显式自赋值；把并列的 guard 拆成独立 transition。
- 不允许：改端口、改位宽、改状态数量、改表达式语义、加新字段。
- 只输出完整的新 IR YAML，不要解释。"""

MATERIALIZE_SYSTEM = """你是硬件 IR 问题物化工具。IR 已经过行为保持的结构重构。
请为给定问题在这份 IR 中重新填写 anchors 和各选项的 RFC6902 patch。
不得修改问题文字、类别、模板、选项 value 或选项数量；必须恰有一个空 patch 表示当前行为。
所有 patch 的差异必须位于 anchors 下。只输出 JSON：
{"anchors":["/..."],"current":"option_value",
 "options":[{"value":"...","patch":[...]}]}"""


def _restructure(llm, ir_text: str, question: Question, verdict, ir: dict) -> str | None:
    prompt = [
        "## 当前 IR", "```yaml\n" + ir_text + "\n```",
        "## 需要获得位置的问题", question.text,
        "## 当前锚点", ", ".join(question.anchors) or "(无)",
        "## 为什么现在没有位置", str(verdict.detail.get("reason", ""))[:600],
        "## 诊断", str(verdict.detail.get("outside_anchor") or "")[:600],
        "请输出重构后的完整 IR YAML。",
    ]
    messages = [{"role": "system", "content": RESTRUCTURE_SYSTEM},
                {"role": "user", "content": "\n\n".join(prompt)}]
    # Was 8000; a full restructured IR could exceed it.
    text = llm.chat(messages, label=f"restructure:{question.id}", max_tokens=None)
    from IRgeneration.ir_generate.gen_petrinet_ir import extract_yaml_block

    body = extract_yaml_block(text)
    return body if body else text


def _materialize(llm, ir_text: str, question: Question) -> Question | None:
    payload = {
        "id": question.id, "category": question.category,
        "template": question.template, "text": question.text,
        "required_construct": question.required_construct,
        "options": [{"value": option.value, "label": option.label}
                    for option in question.options],
    }
    messages = [
        {"role": "system", "content": MATERIALIZE_SYSTEM},
        {"role": "user", "content": "## IR\n```yaml\n" + ir_text +
         "\n```\n\n## 固定问题\n" + json.dumps(payload, ensure_ascii=False, indent=2)},
    ]
    try:
        raw = llm.json(messages, label=f"materialize:{question.id}",
                       temperature=0.0,
                       # Was 3000; long option sets used to truncate.
                       max_tokens=None)
    except Exception:  # noqa: BLE001 - failed materialization remains a gap
        return None
    if not isinstance(raw, dict) or not isinstance(raw.get("options"), list):
        return None
    by_value = {str(item.get("value")): item for item in raw["options"]
                if isinstance(item, dict)}
    if set(by_value) != set(option_signature(question)):
        return None
    candidate = copy.deepcopy(question)
    candidate.anchors = [str(anchor) for anchor in raw.get("anchors", [])]
    candidate.current = str(raw.get("current", ""))
    candidate.options = [Option(value=option.value, label=option.label,
                                patch=list(by_value[option.value].get("patch") or []),
                                note=option.note)
                         for option in question.options]
    if validate_question(load_ir_text(ir_text), candidate):
        return None
    return candidate


def load_ir_text(text: str) -> dict[str, Any]:
    """Parse YAML text without creating an artifact."""
    import yaml

    data = yaml.safe_load(text)
    if not isinstance(data, dict):
        raise ValueError("IR root must be a mapping")
    return data


def _stale_pointers(ir: dict[str, Any], question: Question) -> bool:
    """True when this question's anchors/patches no longer address ``ir``.

    An accepted rebuild can move or renumber nodes; every other question then
    carries pointers into the previous document.  Those questions are not
    unrepresentable, only stale, and must be re-materialized instead of being
    reported as a site gap.
    """
    from blocks.b2_site_model.patches import apply_patch, pointer_exists

    if not question.anchors:
        return False
    if any(not pointer_exists(ir, anchor) for anchor in question.anchors):
        return True
    for option in question.options:
        if not option.patch:
            continue
        try:
            apply_patch(ir, option.patch)
        except Exception:  # noqa: BLE001 - an inapplicable patch is stale
            return True
    return False


def _refresh_stale_questions(llm, ir_path: Path, questions: list[Question],
                             signatures: dict[str, tuple], skip_id: str) -> list[str]:
    """Re-materialize questions whose pointers the rebuild invalidated."""
    new_ir = load_ir(ir_path)
    text = ir_path.read_text(encoding="utf-8")
    refreshed: list[str] = []
    for question in questions:
        if question.id == skip_id or question.reason:
            continue
        if not _stale_pointers(new_ir, question):
            continue
        materialized = _materialize(llm, text, question)
        if materialized is None:
            continue
        expected_identity, expected_options = signatures.get(question.id, (None, None))
        if (materialized.inventory_fingerprint != expected_identity
                or option_signature(materialized) != expected_options):
            continue
        if _stale_pointers(new_ir, materialized):
            continue
        question.anchors = materialized.anchors
        question.options = materialized.options
        question.current = materialized.current
        refreshed.append(question.id)
    return refreshed


def close_sites(ir_path: Path, questions: list[Question], ctx, config: dict,
                llm=None) -> dict[str, Any]:
    workdir = ctx.directory / "phase_a" / "sites"
    matrix = load_matrix(config.get("matrix"))
    allow_restructure = bool(config.get("allow_restructure", False))
    allow_schema = bool(config.get("allow_schema_extension", False))
    max_rounds = max(0, int(config.get("max_rebuild_rounds", 0)))
    compile_options = bool(config.get("compile_options", True))
    # Observability is a ranking/diagnostic feature, never a gate: a found
    # difference is a question and its patch is checked by compile/lint.
    require_observable = False
    observable_horizon = max(1, int(config.get("observable_horizon", 16)))
    verilator = config.get("verilator")

    def candidate_lint(rtl_path):
        from blocks.b7_phase_c.verilog import lint_rtl

        return lint_rtl(rtl_path, verilator=verilator)

    lint = candidate_lint if verilator is not None else None
    signatures = {question.id: (question.inventory_fingerprint, option_signature(question))
                  for question in questions}
    history: list[dict[str, Any]] = []
    final_rows: list[dict[str, Any]] = []

    for round_index in range(max_rounds + 1):
        ir = load_ir(ir_path)
        ir_text = ir_path.read_text(encoding="utf-8")
        ir_graph = build_ir_graph(ir_path) if require_observable else None
        evaluated = []
        final_rows = []
        for question in questions:
            observations: dict[str, Any] = {}

            def observable(current: Question) -> bool | None:
                result = structural_observability(ir_graph or {}, ir, current.anchors,
                                                  horizon=observable_horizon)
                observations.update(result)
                return result["observable"]

            verdict = detect_site(
                ir, question, workdir / f"round_{round_index}" / question.id,
                compile_rtl=compile_options, lint=lint,
                observable=observable if require_observable else None,
                require_observable=require_observable,
            )
            capability = assess(question, verdict, matrix,
                                allow_schema_extension=allow_schema)
            if capability["gap_type"] == "degenerate":
                question.status, question.reason = "D1", "options compile to identical RTL"
            else:
                question.status = verdict.status
                question.reason = verdict.detail.get("reason", "")
            row = {"id": question.id, "category": question.category,
                   "template": question.template, "text": question.text,
                   **summarise(verdict), "status": question.status,
                   "diff_paths": verdict.diff_paths,
                   "option_count": len(question.options),
                   "option_compiled": sum(1 for item in verdict.detail.get("candidates", [])
                                          if item.get("ok")),
                   "observability": observations if require_observable else None,
                   "capability": capability}
            final_rows.append(row)
            evaluated.append((question, verdict, capability))

        round_covered = sum(1 for _, _, capability in evaluated if capability["ok"])
        round_metrics = {
            "covered": round_covered, "questions": len(evaluated),
            "coverage": round(round_covered / len(evaluated), 4) if evaluated else 1.0,
            "option_compiled": sum(row["option_compiled"] for row in final_rows),
            "option_count": sum(row["option_count"] for row in final_rows),
        }
        if all(capability["ok"] for _, _, capability in evaluated):
            history.append({"round": round_index, "status": "closed",
                            "unresolved": [], **round_metrics})
            break
        unresolved = [question.id for question, _, capability in evaluated
                      if not capability["ok"]]
        record: dict[str, Any] = {"round": round_index, "status": "open",
                                  "unresolved": unresolved, "attempts": [],
                                  **round_metrics}
        history.append(record)
        if round_index >= max_rounds or not allow_restructure or llm is None:
            break

        changed = False
        for question, verdict, capability in evaluated:
            if capability["ok"] or capability["gap_type"] != "instance_restructure":
                continue
            attempt = {"question": question.id, "result": "not_produced"}
            record["attempts"].append(attempt)
            candidate_text = _restructure(llm, ir_text, question, verdict, ir)
            if not candidate_text:
                continue
            trial = workdir / f"rebuild_{round_index}_{question.id}"
            trial.mkdir(parents=True, exist_ok=True)
            trial_ir = trial / "ir.yaml"
            trial_ir.write_text(candidate_text, encoding="utf-8")
            check = check_ir(trial_ir, trial / "checks", compile_smoke=True,
                             emit_rtl=trial / "rtl.v")
            attempt["checks"] = check.get("status")
            if check.get("status") != "passed":
                attempt["result"] = "ir_check_failed"
                continue
            if not _same_rtl(ir_path, trial / "rtl.v", ctx):
                attempt["result"] = "behavior_changed"
                continue
            materialized = _materialize(llm, trial_ir.read_text(encoding="utf-8"), question)
            if materialized is None:
                attempt["result"] = "materialization_failed"
                continue
            expected_identity, expected_options = signatures[question.id]
            if (materialized.inventory_fingerprint != expected_identity
                    or option_signature(materialized) != expected_options):
                attempt["result"] = "inventory_changed"
                continue
            retry = detect_site(load_ir(trial_ir), materialized,
                                trial / "materialized", compile_rtl=compile_options,
                                lint=lint)
            if not retry.ok and retry.detail.get("reason") != "no_behavioural_difference":
                attempt["result"] = "still_unrepresentable"
                attempt["status"] = retry.status
                continue
            question.anchors = materialized.anchors
            question.options = materialized.options
            question.current = materialized.current
            question.reason = ""
            ir_path = trial_ir
            attempt["result"] = "accepted"
            attempt["refreshed_questions"] = _refresh_stale_questions(
                llm, trial_ir, questions, signatures, question.id)
            changed = True
            break  # indices may have changed; re-evaluate every question
        if not changed:
            break

    closed_ir = ctx.path("phase_a/closed_ir.yaml")
    closed_ir.write_text(ir_path.read_text(encoding="utf-8"), encoding="utf-8")
    ctx.record(closed_ir, schema_version=matrix.get("schema_version"))
    statuses = [row["status"] for row in final_rows]
    covered = sum(1 for row in final_rows if row["capability"]["ok"])
    total = len(final_rows)
    option_count = sum(row["option_count"] for row in final_rows)
    option_compiled = sum(row["option_compiled"] for row in final_rows)
    report = {
        "questions": total,
        "covered": covered,
        "S0": statuses.count(S0),
        "S1": statuses.count("S1"),
        "S2": statuses.count("S2"),
        "S3": statuses.count("S3"),
        "D1": statuses.count("D1"),
        "coverage": round(covered / total, 4) if total else 1.0,
        "initial_coverage": history[0]["coverage"] if history else 1.0,
        "option_count": option_count,
        "option_compiled": option_compiled,
        "option_compile_rate": round(option_compiled / option_count, 4)
        if option_count else 1.0,
        "closed": covered == total,
        "rebuild_rounds": sum(1 for row in history if row.get("attempts")),
        "accepted_rebuilds": sum(1 for row in history for attempt in row.get("attempts", [])
                                 if attempt.get("result") == "accepted"),
        "schema_version": matrix.get("schema_version"),
        "matrix_version": matrix.get("matrix_version"),
        "require_observable": require_observable,
        "observable_horizon": observable_horizon,
        "ir": str(closed_ir),
    }
    ctx.write_json("phase_a/site_verdicts.json", final_rows)
    ctx.write_json("phase_a/representability.json",
                   {"summary": report, "questions": final_rows, "history": history})
    ctx.write_json("phase_a/rebuild_history.json", history)
    ctx.write_json("phase_a/site_coverage.json", report)
    ctx.event("representability_closed" if report["closed"] else "representability_open",
              **report)
    return {"ir": closed_ir, "ir_text": closed_ir.read_text(encoding="utf-8"),
            "questions": questions, "verdicts": final_rows, "history": history,
            **report}


def _same_rtl(original_ir: Path, candidate_rtl: Path, ctx) -> bool:
    """Behaviour-preserving guard: identical compiled Verilog."""
    from Compiler.rtl_compiler import compile_ir as compile_plain

    reference = ctx.path("phase_a/restructure_baseline.v")
    try:
        compile_plain(original_ir, reference)
    except Exception:  # noqa: BLE001
        return False
    return reference.read_text(encoding="utf-8") == candidate_rtl.read_text(encoding="utf-8")
