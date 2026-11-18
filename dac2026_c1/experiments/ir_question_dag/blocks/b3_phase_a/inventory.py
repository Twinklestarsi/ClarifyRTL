"""LLM question inventory: spec + IR -> anchored, patch-based questions.

The model is asked for questions only, never answers.  Every option is a
JSON-Patch over the raw IR document, so an option is directly executable and
directly checkable by block 2.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pathlib
from blocks.b2_site_model.patches import (apply_patch,
                                          normalise_question_pointers,
                                          pointer_exists)
from blocks.b2_site_model.schema import Option, Question

CATEGORY_HINTS = {
    "C1": "并发优先级：多个条件同拍有效时谁覆盖谁",
    "C2": "时序关系：同拍/下一拍/固定几拍返回",
    "C3": "边界条件：满/空/上溢/下溢时的行为",
    "C4": "复位语义：同步/异步、电平、复位值、复位期输出",
    "C5": "握手协议：valid/ready 的保持与撤销、反压期间数据",
    "C6": "状态更新条件：暂停/空闲/异常时保持还是清零",
    "C7": "冲突处理：同址读写、多写端口同时写",
    "C8": "数值与参数：位宽、最大值、溢出/饱和、参数范围",
}

SYSTEM = """你是硬件架构师，正在审阅从规格自动生成的一份编译器 IR。
你的任务是**逐条对照可见规格与当前 IR**：只要发现 IR 与规格不一致、或规格有要求而 IR 没有体现，
就必须提出问题；不要因为“规格已经写清楚”而跳过，也不要只找规格的歧义。
每个问题都要给出规格依据、当前 IR 的读法（空 patch 的当前选项），以及至少一个按规格修正 IR 的候选补丁。

硬性规则：
1. 逐条对照规格与 IR。发现任何语义差异都要提问，包括规格已经明确写出、但 IR 实现不同的地方；
   不要只问“规格没写清”的歧义。若当前 IR 与规格一致，则不必提问。
2. 当前 IR 有承载位置时，用 JSON Pointer 写 anchors，并把每个选项写成 RFC6902 JSON-Patch。
   IR 里的列表元素必须用下标（例如 /derived_signals/3/expression），不得用对象名代替下标。
   补丁里的表达式只能引用 IR 中已声明的端口、状态、派生信号或参数，不得虚构标识符。
3. 当前 IR 没有承载位置时仍要保留问题：anchors 写 []，各选项 patch 写 []，并填写
   required_construct 与 gap_hypothesis（G2=实例重构可获得位置，G1=需要扩 schema）。
4. 已有承载位置时必须恰有一个选项的 patch 为空数组 []，代表当前 IR 的暂定读法。
5. 每个问题 2-5 个选项，选项之间必须互斥且语义可区分。
6. 不要提出需要新增端口或改变模块接口的问题；需要新增 IR 语法的问题必须保留并标 G1。
7. 纯写法/元数据差异（运算符写法 || 与 |、dependencies 列表、resources 列表、
   width 冗余标注、data_model 描述、timing 元数据）不影响行为，不要为此提问；
   但任何会改变编译后 Verilog 行为的 IR/规格差异都必须提问（表达式、guard、复位值、
   优先级、寄存器级数、位宽/符号扩展、响应时序等）。
8. 如果一个问题的所有候选答案都会编出完全相同的 RTL，就不要提这个问题。
9. 修正算术语义时，候选补丁必须显式处理 Verilog 的符号扩展：signed 与 unsigned 混用
   时不要依赖隐式转换；需要时先扩位（例如 $signed({1'b0, x})）再乘/除，或先声明
   一个足够宽的中间信号。
10. 只输出 JSON，不要解释。

输出格式：
{"questions": [
  {"id": "Q1", "category": "C1", "template": "priority_pick",
   "text": "...", "anchors": ["/priority/0"],
   "required_construct": "priority[]", "gap_hypothesis": "direct",
   "depends_on": [],
   "active_when": [{"question": "Q0", "values": ["option_a"]}],
   "evidence": ["规格中的相关原文片段"],
   "options": [
      {"value": "a_first", "label": "...", "patch": []},
      {"value": "b_first", "label": "...", "patch": [{"op": "replace", "path": "/priority/0", "value": {"higher": "b", "lower": "a"}}]}
   ],
   "current": "a_first"}
]}

模板取值：priority_pick, stage_count, boundary_guard, reset_semantics, handshake_hold,
hold_on_pause, mem_conflict, width_overflow, expression_choice。
"""


def build_messages(spec_text: str, ir_text: str, *,
                   max_questions: int | None = None,
                   cex: dict[str, Any] | None = None,
                   identifiers: list[str] | None = None) -> list[dict[str, str]]:
    categories = "\n".join(f"- {key}: {value}" for key, value in CATEGORY_HINTS.items())
    user = [
        "## 可见规格", spec_text.strip(),
        "## 当前 IR（YAML）", "```yaml\n" + ir_text.strip() + "\n```",
        "## 八类检查方向", categories,
        ("## 要求\n逐条对照规格与当前 IR；发现任何差异都要提出问题，不要因为规格已经写明而跳过。"
         if max_questions is None else
         f"## 要求\n最多提出 {max_questions} 个问题；不得因为当前 IR 没有位置而漏掉问题。"),
    ]
    if identifiers:
        user.append(
            "## 已声明标识符（补丁里的 Verilog 表达式只能引用这些名字）\n"
            + ", ".join(identifiers))
    if cex:
        user += ["## 形式化反例（等价性检查发现当前 IR 编出的 RTL 与参考实现不一致）",
                 json.dumps(cex, ensure_ascii=False),
                 "请优先提出能解释这个反例的问题，例如某个表达式、guard、复位值或优先级是否读错。"]
    return [{"role": "system", "content": SYSTEM},
            {"role": "user", "content": "\n\n".join(user)}]


def _normalise(raw: dict[str, Any]) -> list[Question]:
    questions: list[Question] = []
    for index, item in enumerate(raw.get("questions", [])):
        options = []
        for option in item.get("options", []):
            options.append(Option(
                value=str(option.get("value", f"opt{len(options)}")),
                label=str(option.get("label", "")),
                patch=list(option.get("patch") or []),
                note=str(option.get("note", "")),
            ))
        questions.append(Question(
            id=str(item.get("id") or f"Q{index + 1}"),
            category=str(item.get("category", "C1")),
            template=str(item.get("template", "expression_choice")),
            text=str(item.get("text", "")).strip(),
            anchors=[str(a) for a in item.get("anchors", [])],
            options=options,
            evidence=[str(e) for e in item.get("evidence", [])],
            current=str(item.get("current", "")),
            required_construct=str(item.get("required_construct", "")),
            gap_hypothesis=str(item.get("gap_hypothesis", "")),
            depends_on=[str(q) for q in item.get("depends_on", [])],
            active_when=[{"question": str(rule.get("question", "")),
                          "values": [str(value) for value in rule.get("values", [])]}
                         for rule in item.get("active_when", [])
                         if isinstance(rule, dict) and rule.get("question")],
        ))
    return questions


def validate_question(ir: dict[str, Any], question: Question) -> list[str]:
    """Return human-readable problems; empty list means structurally usable."""
    problems: list[str] = []
    if len(question.options) < 2:
        problems.append("fewer than two options")
    if len({option.value for option in question.options}) != len(question.options):
        problems.append("duplicate option values")
    for anchor in question.anchors:
        if not pointer_exists(ir, anchor, allow_prefix=True):
            problems.append(f"anchor does not resolve: {anchor}")
    empty = [option.value for option in question.options if not option.patch]
    if len(empty) != 1:
        problems.append(f"exactly one option must be the current IR (found {len(empty)})")
    for option in question.options:
        if not option.patch:
            continue
        try:
            apply_patch(ir, option.patch)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"option {option.value}: patch does not apply ({exc})"[:300])
    return problems


def validate_inventory_question(question: Question) -> list[str]:
    """Validate semantic inventory identity without requiring an existing slot."""
    problems: list[str] = []
    if not question.text:
        problems.append("empty question text")
    if question.category not in CATEGORY_HINTS:
        problems.append(f"unknown category: {question.category}")
    if not 2 <= len(question.options) <= 5:
        problems.append("question needs 2-5 options")
    if len({option.value for option in question.options}) != len(question.options):
        problems.append("duplicate option values")
    if any(not option.value for option in question.options):
        problems.append("empty option value")
    return problems


def declared_identifiers(ir: dict[str, Any]) -> list[str]:
    """Identifiers a patch expression may legally reference."""
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
    return sorted(names)


def _default_lint(rtl_path: Path) -> dict[str, Any]:
    from blocks.b7_phase_c.verilog import lint_rtl

    return lint_rtl(rtl_path)


def option_compile_problems(ir: dict[str, Any], question: Question,
                            workdir: Path, *,
                            lint: Any = None) -> list[str]:
    """Run the full candidate battery on every non-current option.

    "Candidate compiles" means checker1, checker2, parser, validate_ir and
    compile_ir all pass *and* the emitted RTL passes Verilator lint.  A lint
    that cannot run does not count as a pass: the candidate is then unproven,
    not accepted.  This happens where the model can still repair the patch, so
    an invented identifier is a candidate failure rather than an unproven G1.
    """
    from blocks.b2_site_model.detect import check_candidate_text
    from blocks.b2_site_model.patches import (apply_patch, dump_ir,
                                              structural_diff, within_site)

    problems: list[str] = []
    for option in question.options:
        if not option.patch:
            continue
        try:
            candidate = apply_patch(ir, option.patch)
        except Exception as exc:  # noqa: BLE001
            problems.append(f"option {option.value}: patch does not apply ({exc})"[:300])
            continue
        safe = "".join(char if char.isalnum() or char in "-_" else "_"
                       for char in option.value)[:40]
        option_dir = workdir / f"{question.id}_{safe}"
        result = check_candidate_text(dump_ir(candidate), option_dir,
                                      compile_rtl=True)
        if not result.get("ok"):
            stage = result.get("stage", "checker")
            error = str(result.get("error", ""))[:400]
            problems.append(f"option {option.value}: {stage} failed: {error}")
            continue
        if lint is None:
            continue
        try:
            lint_result = lint(option_dir / "rtl.v")
            if lint_result is None:
                continue
        except Exception as exc:  # noqa: BLE001 - unavailable lint is unproven
            lint_result = {"ok": False, "status": "tool_error",
                           "errors": [f"{type(exc).__name__}: {exc}"[:200]]}
        if not lint_result.get("ok") or lint_result.get("status") != "passed":
            errors = "; ".join(str(item) for item in lint_result.get("errors") or [])[:400]
            problems.append(
                f"option {option.value}: lint {lint_result.get('status', 'failed')}: {errors}")
    return problems


def plan_questions(llm, spec_text: str, ir_text: str, ir: dict[str, Any], *,
                   max_questions: int | None = None,
                   cex: dict[str, Any] | None = None,
                   rounds: int = 1, max_tokens: int | None = None,
                   lint: Any = None) -> tuple[list[Question], list[str]]:
    """Ask for a question tree; if it is invalid, rewrite the whole tree.

    There is exactly one repair branch: a failed tree is replaced wholesale by
    ``rewrite_question_tree``.  Questions are never partially patched and an
    invalid question is never kept as an "unmaterialized" placeholder.
    """
    from blocks.b3_phase_a.question_tree import (question_tree_problems,
                                                 rewrite_question_tree)

    messages = build_messages(spec_text, ir_text, max_questions=max_questions, cex=cex,
                              identifiers=declared_identifiers(ir))
    notes: list[str] = []
    questions: list[Question] = []
    attempts = max(1, int(rounds))
    for attempt in range(attempts):
        try:
            raw = llm.json(messages, label=f"inventory:{attempt}", max_tokens=max_tokens)
        except Exception as exc:  # noqa: BLE001
            notes.append(f"inventory attempt {attempt} failed: {type(exc).__name__}")
            continue
        questions = _normalise(raw if isinstance(raw, dict) else {"questions": raw})
        for question in questions:
            pointer_problems = normalise_question_pointers(ir, question)
            if pointer_problems:
                question.reason = "; ".join(pointer_problems)[:600]
        rows = question_tree_problems(ir, questions, lint=lint)
        notes.append(f"attempt {attempt}: {len(questions)} questions, "
                     f"{len(rows)} invalid")
        if not rows:
            return questions, notes
        notes.append("question tree invalid: "
                     + json.dumps(rows, ensure_ascii=False)[:4000])
        if attempt == attempts - 1:
            break
        rewritten = rewrite_question_tree(
            llm, spec_text, ir_text, ir, questions, rows, max_tokens=max_tokens)
        if rewritten is None:
            notes.append("question tree rewrite failed")
            break
        questions = rewritten
    rows = question_tree_problems(ir, questions, lint=lint)
    invalid_ids = {row["id"] for row in rows}
    if invalid_ids:
        notes.append("dropped invalid questions: " + ", ".join(sorted(invalid_ids)))
    return [question for question in questions if question.id not in invalid_ids], notes
