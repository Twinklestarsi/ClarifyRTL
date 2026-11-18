#!/usr/bin/env python3
"""Incremental report for the running experiment matrices."""
from __future__ import annotations
import json, time
from collections import defaultdict
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
TAG = "run1"
OUT = RUNS / "REPORT.md"
JSON = RUNS / "report.json"

LABELS = {"RTLLM": "RTLLM", "RTLLM_AMBIG": "RTLLM_AMBIG",
          "VERILOGEVAL": "VerilogEval", "VERILOGEVAL_AMBIG": "VerilogEval_AMBIG",
          "TEMPO": "Tempo", "TEMPO_AMBIG": "Tempo_AMBIG"}


def tool_rows() -> list[dict]:
    path = RUNS / "tools_matrix" / TAG / "progress.jsonl"
    latest = {}
    if path.is_file():
        for line in path.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                row = json.loads(line)
            except Exception:
                continue
            key = (row.get("tool"), row.get("model"), row.get("label"),
                   row.get("short"), row.get("repeat"))
            latest[key] = row
    rows = []
    for row in latest.values():
        usage = {}
        result = row.get("result")
        if result and Path(result).is_file():
            try:
                data = json.loads(Path(result).read_text(encoding="utf-8"))
                usage = data.get("llm_usage") or {}
                if not usage:
                    log_path = (data.get("autochip") or {}).get("log")
                    if log_path and Path(log_path).is_file():
                        chars = len(Path(log_path).read_text(
                            encoding="utf-8", errors="replace"))
                        usage = {"total_tokens": max(1, chars // 4),
                                 "estimated_from": "autochip_log_chars/4"}
            except Exception:
                usage = {}
        rows.append({"system": row.get("tool"), "model": row.get("model"),
                     "label": row.get("label"), "short": row.get("short"),
                     "repeat": row.get("repeat"), "status": row.get("status"),
                     "elapsed_s": row.get("elapsed_s"), "llm_usage": usage,
                     "asked": 0, "kind": "tool"})
    return rows


def ours_rows() -> list[dict]:
    latest: dict[tuple, tuple[float, dict]] = {}
    root = RUNS / "ours_matrix" / TAG
    if not root.is_dir():
        return []
    for path in root.glob("*/*/*/r*/*/*/result.json"):
        parts = path.parts
        try:
            system, model = parts[-7], parts[-6]
            repeat = parts[-4]
            label, short = path.parents[1].name.split("__", 1)
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        key = (system, model, label, short, repeat)
        mtime = path.stat().st_mtime
        if key not in latest or mtime > latest[key][0]:
            latest[key] = (mtime, {"system": system, "model": model,
                                   "label": label, "short": short,
                                   "repeat": repeat,
                                   "status": data.get("status"),
                                   "elapsed_s": data.get("duration_s"),
                                   "llm_usage": data.get("llm_usage") or {},
                                   "asked": int(((data.get("dialogue") or {})
                                                 .get("asked", 0)) or 0),
                                   "kind": "ours", "path": str(path)})
    return [row for _, row in latest.values()]


def passed(kind: str, status: str | None) -> bool:
    s = str(status or "")
    if kind == "ours":
        return s.startswith("equivalent")
    return s == "passed"


def main() -> None:
    rows = tool_rows() + ours_rows()
    grouped = defaultdict(lambda: {"total": 0, "pass": 0, "tokens": 0,
                                   "asked": 0})
    for row in rows:
        key = (row["system"], row["model"], LABELS.get(row["label"], row["label"]))
        g = grouped[key]
        g["total"] += 1
        g["pass"] += 1 if passed(row["kind"], row["status"]) else 0
        g["asked"] += int(row.get("asked", 0) or 0)
        usage = row.get("llm_usage") or {}
        if isinstance(usage, dict):
            total = usage.get("total_tokens")
            if total is None:
                total = (int(usage.get("input_tokens_estimate", 0) or 0)
                         + int(usage.get("output_tokens_estimate", 0) or 0))
            g["tokens"] += int(total or 0)
    target = 13320  # 6 systems * 2 models * 222 paired cases * 5 repeats
    done = len(rows)
    lines = [f"# Experiment live report ({TAG})", "",
             f"Updated: {datetime.now().isoformat(timespec='seconds')}", "",
             f"Progress: {done} / {target} unique case-runs "
             f"({100.0 * done / target:.1f}%)", "",
             "| system | model | dataset | pass | total | rate | avg Q | tokens |",
             "|---|---|---|---:|---:|---:|---:|---:|"]
    for (system, model, label), g in sorted(grouped.items()):
        rate = 100.0 * g["pass"] / max(g["total"], 1)
        avg_q = g["asked"] / max(g["total"], 1)
        lines.append(f"| {system} | {model} | {label} | {g['pass']} | {g['total']} | "
                     f"{rate:.1f}% | {avg_q:.1f} | {g['tokens']} |")
    OUT.write_text("\n".join(lines) + "\n", encoding="utf-8")
    payload = {"updated": time.time(),
               "groups": {f"{k[0]}/{k[1]}/{k[2]}": v for k, v in grouped.items()},
               "rows": len(rows)}
    JSON.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")
    print(f"rows={len(rows)} groups={len(grouped)} -> {OUT}")


if __name__ == "__main__":
    main()
