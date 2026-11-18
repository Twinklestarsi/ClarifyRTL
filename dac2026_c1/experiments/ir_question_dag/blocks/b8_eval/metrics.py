"""Aggregate per-run results into tables and a markdown report."""

from __future__ import annotations

import csv
import json
from collections import Counter
from pathlib import Path
from typing import Any

SUCCESS = "equivalent"


def load_results(output_root: Path | str) -> list[dict[str, Any]]:
    rows: list[dict[str, Any]] = []
    for result in sorted(Path(output_root).glob("*/2*/result.json")):
        try:
            data = json.loads(result.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        data["_mtime"] = result.stat().st_mtime
        rows.append(data)
    return rows


def latest_per_case(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in rows:
        key = row.get("case")
        if key and (key not in latest or row["_mtime"] > latest[key]["_mtime"]):
            latest[key] = row
    return sorted(latest.values(), key=lambda row: row["case"])


def summarise(rows: list[dict[str, Any]], arm: str | None = None) -> dict[str, Any]:
    selected = [row for row in rows if arm is None or row.get("arm") == arm]
    total = len(selected)
    passed = sum(1 for row in selected if row.get("status") == SUCCESS)
    statuses = Counter(str(row.get("status")) for row in selected)
    asked = [row.get("dialogue", {}).get("asked") or 0 for row in selected if row.get("dialogue")]
    repairs = [len(row.get("repairs") or []) for row in selected]
    tokens = [row.get("llm_usage", {}).get("total_tokens") or 0 for row in selected
              if isinstance(row.get("llm_usage"), dict)]
    baseline_passed = sum(1 for row in selected
                          if (row.get("baseline_formal") or {}).get("status") == SUCCESS)
    improved = sum(1 for row in selected
                   if (row.get("baseline_formal") or {}).get("status") != SUCCESS
                   and (row.get("formal") or {}).get("status") == SUCCESS)
    regressed = sum(1 for row in selected
                    if (row.get("baseline_formal") or {}).get("status") == SUCCESS
                    and (row.get("formal") or {}).get("status") != SUCCESS)
    represented = [row.get("representability") or {} for row in selected
                   if isinstance(row.get("representability"), dict)]
    option_total = sum(item.get("option_count") or 0 for item in represented)
    option_compiled = sum(item.get("option_compiled") or 0 for item in represented)
    terminal = Counter()
    for row in selected:
        terminal.update(((row.get("dialogue") or {}).get("states") or {}).values())
    return {
        "arm": arm or "mixed",
        "cases": total,
        "equivalent": passed,
        "rate": round(passed / total, 4) if total else 0.0,
        "status_counts": dict(statuses),
        "baseline_equivalent": baseline_passed,
        "improved": improved,
        "regressed": regressed,
        "representability_initial_mean": round(
            sum(item.get("initial_coverage") or 0 for item in represented) / len(represented), 4)
        if represented else 0.0,
        "representability_final_mean": round(
            sum(item.get("coverage") or 0 for item in represented) / len(represented), 4)
        if represented else 0.0,
        "option_compile_rate": round(option_compiled / option_total, 4)
        if option_total else 0.0,
        "accepted_rebuilds": sum(item.get("accepted_rebuilds") or 0 for item in represented),
        "terminal_states": dict(terminal),
        "questions_asked": sum(asked),
        "cases_with_questions": len(asked),
        "repair_rounds": sum(repairs),
        "cases_with_repair": sum(1 for value in repairs if value),
        "tokens": sum(tokens),
        "mean_duration_s": round(sum(row.get("duration_s") or 0 for row in selected) / total, 1)
        if total else 0.0,
    }


def write_csv(rows: list[dict[str, Any]], path: Path | str) -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    fields = ["case", "arm", "status", "baseline_formal", "formal", "questions",
              "coverage_before", "coverage_after", "option_compile_rate", "rebuilds",
              "dialogue_asked", "resolved_without_ask", "derived_residual",
              "constraints", "constraints_covered", "requires_new_baseline",
              "repairs", "duration_s", "tokens_per_question", "llm_tokens", "run_dir"]
    with target.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields)
        writer.writeheader()
        for row in rows:
            represented = row.get("representability") or {}
            dialogue = row.get("dialogue") or {}
            derived = row.get("derived") or {}
            asked = dialogue.get("asked") or 0
            tokens = ((row.get("llm_usage") or {}).get("total_tokens")
                      if isinstance(row.get("llm_usage"), dict) else None)
            writer.writerow({
                "case": row.get("case"), "arm": row.get("arm"),
                "status": row.get("status"),
                "baseline_formal": (row.get("baseline_formal") or {}).get("status"),
                "formal": (row.get("formal") or {}).get("status"),
                "questions": row.get("questions"),
                "coverage_before": represented.get("initial_coverage"),
                "coverage_after": represented.get("coverage", row.get("coverage")),
                "option_compile_rate": represented.get("option_compile_rate"),
                "rebuilds": represented.get("accepted_rebuilds"),
                "dialogue_asked": asked,
                "resolved_without_ask": dialogue.get("resolved_without_ask"),
                "derived_residual": derived.get("residual"),
                "constraints": derived.get("constraints"),
                "constraints_covered": derived.get("constraints_covered"),
                "requires_new_baseline": dialogue.get("requires_new_baseline"),
                "repairs": len(row.get("repairs") or []),
                "duration_s": row.get("duration_s"),
                "tokens_per_question": round(tokens / asked, 1) if tokens is not None and asked else None,
                "llm_tokens": tokens,
                "run_dir": row.get("run_dir") or row.get("directory"),
            })
    return target


def write_report(rows: list[dict[str, Any]], path: Path | str, *, title: str = "批量结果") -> Path:
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    lines = [f"# {title}", ""]
    arms = sorted({str(row.get("arm")) for row in rows})
    lines += ["| 臂 | 例数 | 基线等价 | 最终等价 | 改善/退化 | 表示覆盖前→后 | 选项可编译率 | 问询总数 |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for arm in arms:
        data = summarise(rows, arm)
        lines.append(f"| {arm} | {data['cases']} | {data['baseline_equivalent']} | "
                     f"{data['equivalent']} ({data['rate']:.1%}) | "
                     f"{data['improved']}/{data['regressed']} | "
                     f"{data['representability_initial_mean']:.1%}→"
                     f"{data['representability_final_mean']:.1%} | "
                     f"{data['option_compile_rate']:.1%} | {data['questions_asked']} |")
    lines += ["", "## 逐例", "", "| 案例 | 臂 | 结果 | 题数 | 已问 | 残留 | 修复轮 | 用时(s) |", "| --- | --- | --- | --- | --- | --- | --- | --- |"]
    for row in rows:
        lines.append("| {case} | {arm} | {status} | {questions} | {asked} | {residual} | {repairs} | {duration} |".format(
            case=row.get("case"), arm=row.get("arm"), status=row.get("status"),
            questions=row.get("questions"), asked=(row.get("dialogue") or {}).get("asked"),
            residual=(row.get("derived") or {}).get("residual"),
            repairs=len(row.get("repairs") or []), duration=row.get("duration_s")))
    failures = [row for row in rows if row.get("status") != SUCCESS]
    if failures:
        lines += ["", "## 失败清单", ""]
        for row in failures:
            lines.append(f"- `{row.get('case')}` — {row.get('status')} "
                         f"({(row.get('formal') or {}).get('status')})")
    target.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return target
