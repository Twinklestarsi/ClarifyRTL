"""Escalation: rewrite the whole IR against the reference implementation.

Used only when the question loop and local repair rounds still leave a
counterexample.  The model receives the standard IR-generation contract plus
the reference RTL, so it can correct structural mistakes that local patches
cannot express (shared counters, missing pipeline stages, port ranges).

This is an oracle-strong arm (A4) and is reported separately from question-only
results; it exists to measure the ceiling of the pipeline, not to stand in for
the clarification effect.
"""

from __future__ import annotations

import json
from typing import Any

SYSTEM_EXTRA = """你现在拿到的是同一份规格的参考实现（行为正确）。
请输出一份**完整的** IR YAML，使它能被确定性编译器生成与参考实现行为等价的 RTL。

要求：
- 遵守上面给出的 IR 语法与完整性检查（端口、时钟域、状态、转移、优先级、表达式）。
- 端口名、方向、位宽必须与参考实现的模块接口一致。
- 本 IR 的端口只能声明 `width`，编译后固定是 `[width-1:0]`，**不支持 msb/lsb**。
  如果参考实现写成 `input [4:1] x`，请把端口写成 `width: 4`，并把所有 `x[4]..x[1]`
  重编号为 `x[3]..x[0]`；任何位选索引都必须 `< width`，否则 JasperGold 会 elaborate 失败。
- 表达式只能是单条 Verilog 表达式（允许 ?:、位运算、拼接），禁止 case/if/begin/end/分号。
- **禁止对表达式做位选/部分选择**（如 `(a >> 1)[0]`、`mem[i][1]`）。先把该表达式
  赋给一个 derived signal，再对新信号做位选。
- transitions 的 sequential target 必须是 `states` 中已经声明的 name；需要新状态就先
  在 states 里 add 它，再写 transition。state 必须带 `clock_domain`（与 clock_domains 的
  name 一致）。
- 参考实现里在 always @(posedge clk) 中赋值的输出寄存器，要在 IR 中声明为 state，
  不要既把它当 output derived、又写 `fr3 = ...` 的 sequential action。
- 需要输出 x 时写 `1'bx` 或 `{W{1'bx}}`，不要写超过位宽个数的 x。
- 不要输出解释、注释或 Markdown 以外的内容，只输出完整 YAML。"""


def build_rewrite_messages(spec_text: str, ir_text: str, rtl_text: str,
                           our_rtl: str, verdict: dict[str, Any],
                           contract: dict | None = None,
                           trace: str | None = None,
                           testbench: str | None = None,
                           history: list | None = None) -> list[dict[str, str]]:
    from IRgeneration.ir_generate.gen_petrinet_ir import build_messages

    messages = build_messages(spec_text, contract)
    payload = [
        "## 参考实现（行为正确，端口签名以它为准）",
        "```verilog\n" + rtl_text + "\n```",
        "## 当前 IR（有错误）",
        "```yaml\n" + ir_text + "\n```",
        "## 当前 IR 生成的 RTL（有错误）",
        "```verilog\n" + our_rtl + "\n```",
        ("## 仿真/测试台失败输出（最后若干行）\n" + trace + "\n") if trace else "",
        ("## 保存的 testbench（验收规则）\n```verilog\n" + testbench + "\n```") if testbench else "",
        "## 等价检查结论",
        json.dumps({k: verdict.get(k) for k in ("status", "reason", "properties", "errors")},
                   ensure_ascii=False),
        ("## 之前的修复/重写历史（不要重复或回退）\n"
         + json.dumps(history[-6:], ensure_ascii=False, indent=2)) if history else "",
        SYSTEM_EXTRA,
    ]
    return messages + [{"role": "user", "content": "\n\n".join(payload)}]


def rewrite_ir(case, llm, ir_text: str, rtl_text: str, verdict: dict[str, Any],
               contract: dict | None = None, *, label: str = "rewrite",
               trace: str | None = None, testbench: str | None = None,
               history: list | None = None) -> str | None:
    from IRgeneration.ir_generate.gen_petrinet_ir import extract_yaml_block, validate_yaml

    messages = build_rewrite_messages(case.prompt_text(), ir_text,
                                      case.reference_text(), rtl_text, verdict, contract,
                                      trace, testbench, history)
    try:
        # Was 8000; a complete rewritten IR must not be truncated.
        text = llm.chat(messages, label=label, temperature=0.1, max_tokens=None)
    except Exception:  # noqa: BLE001
        return None
    body = extract_yaml_block(text)
    if not body:
        # Some providers answer with bare YAML and no fence.
        raw = text.strip().strip("`").lstrip("yaml").lstrip()
        index = raw.find("module:")
        if index != -1:
            body = raw[index:]
            if body.rstrip().endswith("```"):
                body = body.rstrip()[:-3]
    if not body:
        # The model sometimes answers with a JSON patch list even when asked
        # for a full IR.  Apply it to the current IR instead of discarding a
        # potentially useful repair.
        try:
            import yaml as _yaml
            from blocks.b1_scaffold.llm import extract_json
            from blocks.b2_site_model.patches import apply_patch, dump_ir

            payload = extract_json(text)
            operations = payload.get("patches") if isinstance(payload, dict) else payload
            if isinstance(operations, list) and operations:
                base = _yaml.safe_load(ir_text)
                return dump_ir(apply_patch(base, operations))
        except Exception:  # noqa: BLE001
            pass
        return None
    # Validation is advisory: the deterministic checker decides, and a block
    # that only fails YAML lint can still be repaired by it.
    return body