"""Extract storage-shape facts from a specification with one LLM call.

The regex scanner only understands a couple of English phrasings.  Specs also
say "each lane owns a 32-bit accumulator", "a 8-entry table of {addr, valid}",
or "one register per warp, cleared by reset".  This module asks the model for
a structured inventory instead, then compares it with the IR's own facts.

The result is cached per case so a dry-run only pays for one call per spec.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

SYSTEM = """你是硬件规格解析器。请从给定的设计规格中抽取**所有存储对象**，只输出 JSON。

对每个存储对象输出：
{
  "name": "规格里出现的名字（大写，如 WARP_SCRATCH / MSHR entry table）",
  "shape": "table | per_selector | scalar",
  "element_width": "元素位宽（参数名或数字，如 XLEN、32；不确定写 unknown）",
  "count": "元素个数（参数名或数字；单个写 1）",
  "index": "按索引选择时使用的信号名（仅 per_selector；否则 null）",
  "fields": ["table 的字段名，如 addr/valid/next；非 table 写 []"],
  "reset": "clear | startup_arg | none | unknown",
  "ir_name": "IR 中对应的状态名（必须来自给出的状态列表；没有就 null）",
  "evidence": "规格原文中最能说明该对象形状的一句话（<=120 字符）"
}

规则：
- 只抽取**存储**：寄存器文件、表、按 warp/lane/entry 索引的数组、scratch、FIFO、CAM。
- 单个标量寄存器也算一个对象（shape=scalar, count=1）。
- 同一张表只报一次，字段放 fields，不要按字段拆成多条。
- 不要臆造规格里没有的对象；evidence 必须能在规格里找到。
- 只输出 {"storage": [...]}，不要解释、不要 Markdown。"""


def extract_spec_facts(spec_text: str, llm, *, cache: Path | str | None = None,
                       label: str = "spec_facts",
                       ir_names: list[str] | None = None) -> list[dict[str, Any]]:
    """Return the spec's storage inventory (cached when ``cache`` is given)."""
    if cache is not None and Path(cache).is_file():
        try:
            data = json.loads(Path(cache).read_text(encoding="utf-8"))
            if isinstance(data, dict) and isinstance(data.get("storage"), list):
                return data["storage"]
        except (OSError, json.JSONDecodeError):
            pass
    context = ""
    if ir_names:
        context = ("\n\n## IR 中已有的状态名\n" + json.dumps(ir_names, ensure_ascii=False) +
                   "\n请把每个存储对象映射到其中一个名字（ir_name），映射不上写 null。")
    messages = [{"role": "system", "content": SYSTEM},
                {"role": "user", "content": "## 规格\n" + spec_text + context}]
    try:
        raw = llm.json(messages, label=label, temperature=0.0, max_tokens=None)
    except Exception:  # noqa: BLE001 - dry-run must survive a bad answer
        return []
    rows = raw.get("storage") if isinstance(raw, dict) else raw
    if not isinstance(rows, list):
        return []
    cleaned: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict) or not row.get("name"):
            continue
        cleaned.append({
            "name": str(row.get("name"))[:80],
            "shape": str(row.get("shape") or "scalar").lower(),
            "element_width": str(row.get("element_width") or "unknown"),
            "count": str(row.get("count") or "1"),
            "index": (str(row["index"]) if row.get("index") not in (None, "", "null")
                      else None),
            "fields": [str(field) for field in (row.get("fields") or [])][:12],
            "reset": str(row.get("reset") or "unknown").lower(),
            "ir_name": (str(row["ir_name"]) if row.get("ir_name") not in (None, "", "null")
                        else None),
            "evidence": str(row.get("evidence") or "")[:200],
        })
    if cache is not None:
        target = Path(cache)
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(json.dumps({"storage": cleaned}, ensure_ascii=False, indent=2),
                          encoding="utf-8")
    return cleaned