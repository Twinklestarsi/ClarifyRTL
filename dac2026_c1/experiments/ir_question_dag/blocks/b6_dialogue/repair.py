"""Oracle-guided repair round used only when equivalence still fails.

This is a documented process extension: after the question loop, the design
owner (oracle) compares our RTL with the reference and returns local IR
patches.  Every patch must compile, and the round is accepted only if the
formal verdict improves.  It is counted separately from the question budget so
the two effects stay separable in the results.
"""

from __future__ import annotations

import json
from typing import Any

from blocks.b2_site_model.patches import apply_patch, dump_ir

SYSTEM = """你是硬件设计修复工具。下面给你：可见规格、参考实现（行为正确）、
我们由 IR 生成的实现（行为不正确）、以及形式化等价检查的结论。

请找出我们实现里**与参考实现行为不一致**的地方，并给出能修正它的 IR 补丁。

规则：
- 只改 IR 文档，用 RFC6902 JSON-Patch（op: add/replace/remove, path, value）。
- 尽量小、尽量局部；不要改端口名/方向/位宽定义，除非问题就出在索引范围上。
- 参考实现里的信号名不一定与 IR 相同，按语义对应，不要照抄名字。
- 允许改 derived_signals 的 expression、transitions 的 guard/action、
  states 的 reset_value、priority 顺序；允许新增 transition 或 state（用 add）。
- 如果差异是结构性的（少一级寄存器、多个计数器本应共享一个），也要用补丁表达；
  表达不了就在 reason 里说明，patches 返回空数组。
- 表达式必须是**单条 Verilog 表达式**：禁止 case/endcase、if/else、begin/end、always、分号；
- 端口只有 width，编译后是 [width-1:0]；所有位选索引必须 < width，参考实现的 [4:1] 要重编号成 0 起；
  也不要把整条赋值语句塞进 expression。
- 端口只有 `width`，编译后是 `[width-1:0]`。所有位选索引必须 `< width`；
  若参考实现用 `[4:1]`，要整体重编号成 0 起。
- 禁止对表达式做位选；先把表达式赋给 derived signal，再对信号做位选。
- transition 的 sequential target 必须是 states 中已声明的 name；缺状态先 add state，
  并给 state 写 clock_domain。always 块里赋值的输出寄存器应声明为 state，不要混用
  derived 与 sequential action。
只输出 JSON：
{"reason": "...", "patches": [ ... ], "confidence": "high|medium|low"}"""


def _payload(case, ir_text: str, rtl_text: str, verdict: dict,
             trace: str | None = None, history: list | None = None,
             testbench: str | None = None) -> str:
    parts = [
        "## 可见规格\n" + case.prompt_text(),
        "## 参考实现（正确）\n```verilog\n" + case.reference_text() + "\n```",
        "## 我们的 IR（当前）\n```yaml\n" + ir_text + "\n```",
        "## 我们的 RTL\n```verilog\n" + rtl_text + "\n```",
        ("## 仿真/测试台失败输出（最后若干行）\n" + trace + "\n") if trace else "",
        ("## 保存的 testbench（验收规则）\n```verilog\n" + testbench + "\n```") if testbench else "",
        "## 等价检查结论\n" + json.dumps(
            {k: verdict.get(k) for k in
             ("status", "reason", "properties", "errors", "simulation")},
            ensure_ascii=False),
    ]
    if history:
        parts.append("## 之前已经应用过的修复（当前 IR 已包含；不要重复、不要改回旧值）\n"
                     + json.dumps(history[-4:], ensure_ascii=False, indent=2))
    return "\n\n".join(parts)


def oracle_repair(case, llm, ir_text: str, rtl_text: str, verdict: dict,
                  *, label: str = "repair", trace: str | None = None,
                  history: list | None = None,
                  testbench: str | None = None) -> dict[str, Any]:
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": _payload(case, ir_text, rtl_text, verdict,
                                                     trace, history, testbench)}]
    try:
        # Was 2000; a multi-operation repair patch could be truncated.
        raw = llm.json(messages, label=label, temperature=0.0, max_tokens=None)
    except Exception as exc:  # noqa: BLE001
        return {"patches": [], "error": f"{type(exc).__name__}: {exc}"[:300]}
    if not isinstance(raw, dict):
        return {"patches": [], "error": "repair answer was not a JSON object"}
    patches = raw.get("patches")
    return {"patches": patches if isinstance(patches, list) else [],
            "reason": str(raw.get("reason", ""))[:800],
            "confidence": raw.get("confidence")}


def apply_repair(ir: dict, repair: dict) -> tuple[dict | None, str]:
    operations = repair.get("patches") or []
    if not operations:
        return None, "no patches proposed"
    try:
        candidate = apply_patch(ir, operations)
    except Exception as exc:  # noqa: BLE001
        return None, f"patch does not apply: {exc}"[:300]
    problem = _procedural_expression(candidate)
    if problem:
        return None, problem
    return candidate, ""


def _procedural_expression(ir: dict) -> str:
    from blocks.b7_phase_c.verilog import procedural_expression

    for index, signal in enumerate(ir.get("derived_signals") or []):
        expression = str(signal.get("expression", ""))
        if procedural_expression(expression):
            return f"derived_signals[{index}] is not a single Verilog expression"
    for index, transition in enumerate(ir.get("transitions") or []):
        actions = transition.get("action") or []
        if isinstance(actions, str):
            actions = [actions]
        for action in actions:
            text = str(action)
            if "=" in text:
                text = text.split("=", 1)[1]
            if procedural_expression(text):
                return f"transitions[{index}] action is not a single expression"
    return ""


def repair_text(candidate: dict) -> str:
    return dump_ir(candidate)