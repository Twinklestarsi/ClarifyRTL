"""Answer sources.  The oracle reads the reference RTL; nothing leaks back
into the solver's context except the selected option value and its reason.
"""

from __future__ import annotations

import json
from typing import Any

from blocks.b2_site_model.patches import apply_patch, dump_ir, resolve_pointer
from blocks.b2_site_model.schema import Question

SYSTEM = """你是这个硬件的设计负责人。下面给你：可见规格、参考实现（Verilog）、
以及一个关于当前实现的设计问题与若干候选答案。
请只根据参考实现的实际行为回答：哪个候选答案与参考实现一致。

规则：
- 只针对 anchors 指向的那个对象回答；不得改用别的状态或信号来回答。
- 每个候选都附有它在 anchors 上的取值。若“保持当前实现”这一候选的取值与参考实现
  一致，就回答它；只有该值确实与参考实现不同、且其它候选也都不对时才回答 other。
- other 的 replacement.pointer 必须就是 anchors 中的一个指针，不得指向别的对象。
- reason 引用参考实现里的具体信号/表达式。

输出 JSON：
{"value": "<某个候选的 value，或 other>",
 "reason": "一句话依据（引用参考实现里的具体信号/表达式）",
 "replacement": {"pointer": "/...", "value": "..."}   // 仅当 value=other
}
"""


def describe_option(ir: dict[str, Any], question: Question, option) -> dict[str, Any]:
    """Show what the anchors look like after applying this option's patch."""
    try:
        patched = apply_patch(ir, option.patch)
    except Exception:  # noqa: BLE001
        return {"value": option.value, "label": option.label, "patch_error": True}
    anchors = {}
    for anchor in question.anchors:
        ok, value = resolve_pointer(patched, anchor)
        anchors[anchor] = value if ok else "<missing>"
    return {"value": option.value, "label": option.label, "anchors": anchors}


class OracleAnswerer:
    def __init__(self, case, llm, ir: dict[str, Any], *, allow_other: bool = True):
        self.case = case
        self.llm = llm
        self.ir = ir
        self.allow_other = allow_other

    def answer(self, question: Question) -> dict[str, Any]:
        options = [describe_option(self.ir, question, option) for option in question.options]
        payload = {
            "question": question.text,
            "anchors": question.anchors,
            "options": options,
            "allow_other": self.allow_other,
        }
        messages = [
            {"role": "system", "content": SYSTEM},
            {"role": "user", "content":
             "## 可见规格\n" + self.case.prompt_text() +
             "\n\n## 参考实现\n```verilog\n" + self.case.reference_text() + "\n```" +
             "\n\n## 问题与候选\n" + json.dumps(payload, ensure_ascii=False, indent=2)},
        ]
        try:
            raw = self.llm.json(messages, label=f"answer:{question.id}",
                                temperature=0.0,
                                # Was 1200; answer JSON is small but reasons can be long.
                                max_tokens=None)
        except Exception as exc:  # noqa: BLE001
            return {"value": None, "error": f"{type(exc).__name__}: {exc}"[:300]}
        if not isinstance(raw, dict):
            return {"value": None, "error": "answer was not a JSON object"}
        value = str(raw.get("value", "")).strip()
        result = {"value": value, "reason": str(raw.get("reason", ""))[:600],
                  "source": "oracle_reference_rtl"}
        if value == "other" and isinstance(raw.get("replacement"), dict):
            # Use the answerer's replacement directly.  The dialogue applies it
            # with the same candidate check as a frozen option, so an invalid
            # replacement fails there instead of being discarded here.
            result["replacement"] = raw["replacement"]
        valid = {option.value for option in question.options}
        if value not in valid and value != "other":
            result["error"] = f"answer {value!r} is not one of {sorted(valid)}"
        return result


class FileAnswerer:
    """Replay answers recorded in a JSON file: {"Q1": {"value": ...}}."""

    def __init__(self, path):
        self.answers = json.loads(open(path, encoding="utf-8").read())

    def answer(self, question: Question) -> dict[str, Any]:
        return dict(self.answers.get(question.id, {}), source="file")


class SpecConformanceAnswerer:
    """Answer spec-conformance questions from the spec, delegate the rest.

    A conformance question asks whether the IR should adopt the shape the spec
    already states, so it must not depend on the reference RTL: the visible
    spec is authoritative.  Every other question keeps its original answerer.
    """

    def __init__(self, inner):
        self.inner = inner

    def answer(self, question: Question) -> dict[str, Any]:
        if getattr(question, "generated_by", "") in {"spec_conformance", "spec_audit"}:
            mandated = next((option for option in question.options
                             if option.patch), None)
            if mandated is not None:
                return {"value": mandated.value,
                        "reason": "可见规格写明了该存储形状；按 spec 判定",
                        "source": "visible_spec"}
        return self.inner.answer(question)


class HumanAnswerer:
    """Minimal interactive terminal answer source for single-case runs."""

    def answer(self, question: Question) -> dict[str, Any]:
        print(f"\n[{question.id}] {question.text}")
        for option in question.options:
            print(f"  {option.value}: {option.label}")
        print("  undecided: 暂不决定")
        print("  other: 当前选项集不完整，停止并重建 baseline")
        value = input("answer> ").strip()
        reason = input("reason> ").strip()
        return {"value": value, "reason": reason, "source": "human"}
