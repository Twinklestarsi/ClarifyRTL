"""Question-tree validation and the single rewrite branch.

A question is binary: keep the current IR, or apply one concrete change.  A tree
that contains an unmaterialized alternative, a duplicate id, a broken pointer or
a candidate that does not compile/lint is invalid as a whole.  There is exactly
one repair action: rewrite the complete tree.  Per-question patch repair and
"unmaterialized" placeholders are intentionally not part of this module.
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path
from typing import Any


def question_tree_problems(ir: dict[str, Any], questions: list[Any], *,
                           lint: Any = None, workdir: Path | None = None) -> list[dict[str, Any]]:
    """Return one ``{"id", "problems"}`` row per invalid question."""
    from blocks.b3_phase_a import inventory

    rows: list[dict[str, Any]] = []
    seen: set[str] = set()
    with tempfile.TemporaryDirectory(prefix="question_tree_") as temporary:
        root = Path(workdir or temporary)
        for question in questions:
            problems: list[str] = []
            if question.id in seen:
                problems.append(f"duplicate question id: {question.id}")
            seen.add(question.id)
            problems.extend(inventory.validate_inventory_question(question))
            problems.extend(inventory.validate_question(ir, question))
            if not any(option.patch for option in question.options):
                problems.append("no modify option: every question needs a concrete patch")
            if not problems:
                problems.extend(inventory.option_compile_problems(
                    ir, question, root, lint=lint))
            if problems:
                rows.append({"id": question.id, "problems": problems})
    return rows


def _normalise(raw: dict[str, Any]) -> list[Any]:
    from blocks.b3_phase_a.inventory import _normalise as normalise

    return normalise(raw)


def rewrite_question_tree(llm, spec_text: str, ir_text: str,
                          ir: dict[str, Any], previous: list[Any],
                          problems: list[dict[str, Any]], *,
                          max_tokens: int | None = None) -> list[Any] | None:
    """Rewrite the complete question tree when validation fails.

    This is the only repair branch.  The model may fix anchors, options and
    patches, but it must not drop a genuine specification/IR difference just to
    pass validation; the prompt says so explicitly.
    """
    system = """你是硬件 IR 问题树的重写工具。上一版问题树没有通过确定性校验。
请**重写整棵问题树**，而不是只修某几道题。

硬性规则：
- 逐条对照可见规格与当前 IR；上一版已经指出的 spec/IR 差异必须保留，不得为了通过校验而删掉。
- 每道题必须恰好有一个“保持当前 IR”的空 patch 选项，以及至少一个带非空 patch 的“按规格修改”选项。
  不允许出现两个空 patch、也不允许出现“需要修改但没有修改方案”的占位题。
- anchors 必须是当前 IR 内可解析的 JSON Pointer；列表用下标；补丁必须落在 anchors 内。
- 每个候选补丁都必须通过 checker1、checker2、parser、validate_ir、compile_ir 和 Verilog lint。
- 问题文字、类别、模板、选项 value、选项数量和规格证据必须可追溯；不要合并不同差异。
- 只输出 JSON：{"questions": [...]}，不要解释。"""
    payload = {
        "previous_questions": [question.to_dict() for question in previous],
        "validation_problems": problems,
    }
    user = [
        "## 可见规格", spec_text.strip(),
        "## 当前 IR（YAML）", "```yaml\n" + ir_text.strip() + "\n```",
        "## 上一版问题树（整棵）", json.dumps(payload, ensure_ascii=False),
        "## 要求", "重写整棵问题树并只输出 JSON；保留所有已指出的 spec/IR 差异。",
    ]
    try:
        raw = llm.json([{"role": "system", "content": system},
                        {"role": "user", "content": "\n\n".join(user)}],
                       label="question_tree_rewrite", temperature=0.0,
                       max_tokens=max_tokens)
    except Exception:  # noqa: BLE001 - an unavailable rewrite is an explicit empty result
        return None
    if not isinstance(raw, dict):
        raw = {"questions": raw}
    questions = _normalise(raw)
    for question in questions:
        from blocks.b2_site_model.patches import normalise_question_pointers

        pointer_problems = normalise_question_pointers(ir, question)
        if pointer_problems:
            question.reason = "; ".join(pointer_problems)[:600]
    return questions
