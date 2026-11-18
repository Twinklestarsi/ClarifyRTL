"""Whole-IR spec audit, asked once before the decision DAG starts.

The decision DAG can only ask "which of these two answers is right", so it
never surfaces a divergence that is not a choice: a missing table, a wrong
width, a logic expression that contradicts the spec.  This module asks the
model for a complete divergence list *first*, so those findings become the
highest-priority nodes of the DAG and the rest of the dialogue only has to
look for remaining decision points.

A finding may carry a concrete RFC6902 patch.  It is accepted as a question
only after the same candidate check the DAG uses (apply -> checker -> compile
-> lint -> different RTL); otherwise it stays a blocker finding for phase C.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from blocks.b2_site_model.schema import Option, Question

SYSTEM = """你是硬件规格与 IR 的一致性审计器。给你一份**可见规格**和一份**待审计的 IR**。
请找出 IR 与规格不一致的**所有**地方，只输出 JSON。

对每一条差异输出：
{
  "id": "A1",
  "kind": "missing_storage | storage_shape | width | reset | init | logic | timing | interface",
  "ir_pointer": "IR 里的位置，如 /states/3 或 /transitions/2/action/0；找不到就写 null",
  "ir_object": "涉及的对象名",
  "expected": "规格要求的行为/形状（一句话）",
  "actual": "IR 当前实际的样子（一句话）",
  "spec_quote": "规格原文中最能证明该要求的句子（<=120 字符）",
  "severity": "high | medium | low",
  "fixable": true/false,
  "patch": [ RFC6902 操作 ]  // 能局部修就给出；结构性重写就写 []
}

编译器语义（审计时必须考虑，不要把这些当成差异）：
- 每个 state 的 `reset_value` 会在复位时自动写入该状态；memory 的 `reset_value` 会自动整块清空。
  因此“IR 里没有显式 reset transition”**不是**差异，除非 `reset_value` 的取值本身错。
- `initial_value` / `initial_enable` 会被自动生成为 `initial` 块。
- `write_mask` 会被自动展开成按字节写。
- `shape` / `depth` 会被 elaborator 展开成数组或 memory。

规则：
- 只报**规格明确写了、而 IR 没做到**的差异；规格没说的不要臆造。
- 覆盖：存储对象（表/数组/每 warp 寄存器）、位宽、复位与初始化、时序/延迟、优先级与握手、接口方向。
- patch 只能改 IR 文档；表达式必须是单条 Verilog 表达式，禁止 case/if/begin/end/分号。
- 若差异需要整体重写（例如缺一张表），fixable 写 false、patch 写 []，不要硬凑。
- **只报差异，不报“检查通过”的项**。如果某处 IR 与规格一致，不要输出。
- 最多输出 15 条，按严重度排序；没有差异就输出 {"findings": []}。
- 只输出 {"findings": [...]}，不要解释、不要 Markdown。"""


_CONFORMING = re.compile(
    r"(correct|matches|conforms|consistent|as specified|no difference|"
    r"符合|一致|正确|无差异|与规格相符)", re.I)


def _clean(items: Any) -> list[dict[str, Any]]:
    findings: list[dict[str, Any]] = []
    if not isinstance(items, list):
        return findings
    for index, row in enumerate(items):
        if not isinstance(row, dict):
            continue
        findings.append({
            "id": str(row.get("id") or f"A{index}"),
            "kind": str(row.get("kind") or "logic"),
            "ir_pointer": (str(row["ir_pointer"]) if row.get("ir_pointer") not in
                           (None, "", "null") else None),
            "ir_object": str(row.get("ir_object") or ""),
            "expected": str(row.get("expected") or "")[:300],
            "actual": str(row.get("actual") or "")[:300],
            "spec_quote": str(row.get("spec_quote") or "")[:200],
            "severity": str(row.get("severity") or "medium").lower(),
            "fixable": bool(row.get("fixable")),
            "patch": row.get("patch") if isinstance(row.get("patch"), list) else [],
        })
    # The compiler applies reset_value/initial_value/write_mask itself; an
    # audit that flags "no explicit reset transition" is a false positive.
    findings = [row for row in findings
                if not (row["kind"] == "reset"
                        and re.search(r"no (explicit )?reset|without (a )?reset",
                                      row["actual"], re.I))]
    kept = [row for row in findings
            if not _CONFORMING.search(f"{row['actual']} {row['expected']}")
            and "no finding" not in f"{row['actual']} {row['expected']}".lower()
            and row["spec_quote"]]
    # ``fixable`` without a patch is not actionable; treat it as a blocker.
    for row in kept:
        if row["fixable"] and not row["patch"]:
            row["fixable"] = False
    kept.sort(key=lambda row: _SEVERITY_ORDER.get(row["severity"], 1))
    return kept[:15]


def audit_ir(case, llm, ir_text: str, *, cache: Path | str | None = None,
             label: str = "spec_audit") -> list[dict[str, Any]]:
    """Return every spec-vs-IR divergence the model can see (cached)."""
    if cache is not None and Path(cache).is_file():
        try:
            data = json.loads(Path(cache).read_text(encoding="utf-8"))
            if (isinstance(data, dict) and isinstance(data.get("findings"), list)
                    and data["findings"]):
                return data["findings"]
        except (OSError, json.JSONDecodeError):
            pass
    messages = [
        {"role": "system", "content": SYSTEM},
        {"role": "user", "content":
         "## 可见规格\n" + case.prompt_text() +
         "\n\n## 待审计 IR\n```yaml\n" + ir_text + "\n```"},
    ]
    try:
        raw = llm.json(messages, label=label, temperature=0.0, max_tokens=None)
    except Exception:  # noqa: BLE001 - the audit must not break the run
        return []
    rows = raw.get("findings") if isinstance(raw, dict) else raw
    findings = _clean(rows)
    if cache is not None and findings:
        target = Path(cache)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"findings": findings}, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    return findings


_SEVERITY_ORDER = {"high": 0, "medium": 1, "low": 2}


PATCH_SYSTEM = """给你若干条“IR 与规格不一致”的审计结论和当前 IR。请为**能局部修**的每一条
生成一个 RFC6902 JSON Patch。无法局部修的写 null。

只输出 JSON：{"fixes": [{"id": "A1", "patch": [ ... ] 或 null, "reason": "..."}]}

规则：
- patch 只能改 IR 文档；表达式必须是单条 Verilog 表达式（禁止 case/if/begin/end/分号）。
- 不要改端口名/方向/位宽定义，除非问题就是它。
- 结构性缺失（缺一张表、缺一段存储）**也要修**：用
  {"op":"add","path":"/states/-","value":{"name":...,"type":"memory","width":...,"depth":...,"clock_domain":"main","reset_value":0}}
  新增 memory/state，再把引用它的动作补上；确实无法用 patch 表达的才写 null。
- 每条最多 6 个操作，尽量局部。"""


def request_patches(case, llm, ir_text: str, findings: list[dict[str, Any]],
                    *, limit: int = 6, label: str = "spec_audit_fix") -> list[dict[str, Any]]:
    """Second pass: ask for concrete patches for the findings that need one."""
    candidates = [row for row in findings if not row.get("patch")][:limit]
    if not candidates:
        return findings
    payload = [{k: row.get(k) for k in ("id", "kind", "ir_pointer", "expected", "actual")}
               for row in candidates]
    messages = [{"role": "system", "content": PATCH_SYSTEM},
                {"role": "user", "content":
                 "## 可见规格\n" + case.prompt_text() +
                 "\n\n## 当前 IR\n```yaml\n" + ir_text + "\n```" +
                 "\n\n## 待修差异\n" + json.dumps(payload, ensure_ascii=False, indent=2)}]
    try:
        raw = llm.json(messages, label=label, temperature=0.0, max_tokens=None)
    except Exception:  # noqa: BLE001
        return findings
    fixes = raw.get("fixes") if isinstance(raw, dict) else raw
    if not isinstance(fixes, list):
        return findings
    by_id = {str(row.get("id")): row for row in findings}
    for fix in fixes:
        if not isinstance(fix, dict):
            continue
        target = by_id.get(str(fix.get("id")))
        patch = fix.get("patch")
        if target is None or not isinstance(patch, list) or not patch:
            continue
        target = dict(target, patch=patch, fixable=True)
        by_id[str(fix.get("id"))] = target
    return [by_id[str(row.get("id"))] for row in findings]


def _request_one(case, llm, ir_text: str, finding: dict[str, Any],
                 error: str | None) -> list[dict[str, Any]] | None:
    payload = {k: finding.get(k) for k in ("id", "kind", "ir_pointer", "expected", "actual")}
    note = f"\n上一次的 patch 失败：{error}\n请换一种写法。" if error else ""
    messages = [{"role": "system", "content": PATCH_SYSTEM},
                {"role": "user", "content":
                 "## 可见规格\n" + case.prompt_text() +
                 "\n\n## 当前 IR\n```yaml\n" + ir_text + "\n```" +
                 "\n\n## 待修差异\n" + json.dumps(payload, ensure_ascii=False) + note}]
    try:
        raw = llm.json(messages, label=f"audit_fix:{finding.get('id')}",
                       temperature=0.0, max_tokens=None)
    except Exception:  # noqa: BLE001
        return None
    fixes = raw.get("fixes") if isinstance(raw, dict) else raw
    if isinstance(fixes, list) and fixes and isinstance(fixes[0], dict):
        patch = fixes[0].get("patch")
        if isinstance(patch, list) and patch:
            return patch
    return None


def apply_findings_with_retry(case, llm, ir_text: str, findings: list[dict[str, Any]],
                              *, check, attempts: int = 2,
                              max_fixes: int = 6) -> tuple[list[dict[str, Any]],
                                                           list[dict[str, Any]]]:
    """Apply findings one by one, retrying on failure instead of dropping them.

    ``check(candidate_ir)`` must run the full candidate battery plus the
    derived/consistency checks.  Accepted patches are applied cumulatively, so
    a combination that breaks the IR is caught here rather than at the final
    gate; a failing patch is regenerated (with the error text) up to
    ``attempts`` times.
    """
    from blocks.b2_site_model.patches import apply_patch

    accepted: list[dict[str, Any]] = []
    rejected: list[dict[str, Any]] = []
    working = None
    ordered = sorted(findings, key=lambda row: _SEVERITY_ORDER.get(row.get("severity"), 1))
    for finding in ordered:
        if len(accepted) >= max_fixes:
            rejected.append(finding)
            continue
        patch = finding.get("patch") or []
        error: str | None = None
        for attempt in range(attempts + 1):
            if not patch:
                patch = _request_one(case, llm, ir_text, finding, error)
            if not patch:
                error = error or "no patch proposed"
                break
            try:
                candidate = apply_patch(working if working is not None else ir_of(ir_text), patch)
            except Exception as exc:  # noqa: BLE001
                error = f"patch does not apply: {exc}"[:200]
                patch = []
                continue
            ok, detail = check(candidate)
            if ok:
                working = candidate
                accepted.append(dict(finding, patch=patch))
                error = None
                break
            error = detail or "candidate rejected"
            patch = []
        if error is not None or finding not in accepted:
            if not any(row.get("id") == finding.get("id") for row in accepted):
                rejected.append(dict(finding, last_error=error))
    return accepted, rejected


def ir_of(text: str) -> dict[str, Any]:
    import yaml

    return yaml.safe_load(text)


def _question_for(finding: dict[str, Any]) -> Question:
    return Question(
        id=f"audit_{finding['id']}",
        category="C7",
        template=f"spec_{finding['kind']}",
        text=(f"审计发现：{finding['actual']}；规格要求：{finding['expected']}。"
              "是否按规格修正？"),
        anchors=[finding["ir_pointer"]] if finding.get("ir_pointer") else [],
        options=[Option(value="current", label="保持当前 IR"),
                 Option(value="spec_fix", label="按规格修正", patch=finding["patch"])],
        current="current",
        required_construct=str(finding["kind"]),
        evidence=[finding.get("spec_quote", "")],
        evidence_source="visible_spec",
        generated_by="spec_audit",
        source_point=str(finding["id"]))


def questions_from_accepted(findings: list[dict[str, Any]]) -> list[Question]:
    """Questions for findings whose patches already passed the cumulative check."""
    return [_question_for(finding) for finding in findings if finding.get("patch")]


def questions_from_findings(ir: dict[str, Any], findings: list[dict[str, Any]],
                            *, validate) -> tuple[list[Question], list[dict[str, Any]]]:
    """Legacy path: validate each finding independently against ``ir``."""
    questions: list[Question] = []
    blockers: list[dict[str, Any]] = []
    ordered = sorted(findings, key=lambda row: _SEVERITY_ORDER.get(row.get("severity"), 1))
    for finding in ordered:
        patch = finding.get("patch") or []
        if not patch or not finding.get("fixable"):
            blockers.append(finding)
            continue
        ok, detail = validate(patch)
        if not ok:
            blockers.append(dict(finding, validation=detail))
            continue
        questions.append(_question_for(dict(finding, patch=patch)))
    return questions, blockers
