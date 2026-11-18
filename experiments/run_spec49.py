#!/usr/bin/env python3
"""Run the direct spec-LLM-RTL baseline on the 49 RTLLM designs, streaming results."""
from __future__ import annotations
import argparse, json, os, subprocess, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNNER = HERE / "runner_cases"
ADAPTERS = HERE / "adapters"
PY = "/nas2nfs/homes/2531930/ONE-TURN/dac2026_c1/.venv_c1/bin/python"
OUT = HERE / "runs" / "spec49"
LABEL = "RTLLM_49"
LABEL_NAME = {"deepseek_aic": "DeepSeek-V4.1-Flash (AIC)",
              "qwen": "Qwen3.8-27B (AIC)"}

def cases():
    man = json.loads((RUNNER / "manifest.json").read_text())
    rows = [c for c in man["cases"] if c["dataset"] == LABEL]
    return sorted(rows, key=lambda c: c["short"])

def run_one(model: str, case: dict) -> dict:
    short = case["short"]
    out = OUT / model / short
    res = out / "result.json"
    if res.is_file():
        try:
            d = json.loads(res.read_text())
            if d.get("status") in ("passed", "failed", "compile_failed", "error"):
                return {**d, "cached": True, "short": short}
        except Exception:
            pass
    out.mkdir(parents=True, exist_ok=True)
    log = out / "run.log"
    cmd = [PY, "run_spec_llm.py", "--case", f"{LABEL}/{short}",
           "--model", model, "--out", str(out)]
    started = time.time()
    with log.open("w") as fh:
        proc = subprocess.run(cmd, cwd=str(ADAPTERS), stdout=fh,
                              stderr=subprocess.STDOUT, timeout=1800)
    d = {}
    if res.is_file():
        try:
            d = json.loads(res.read_text())
        except Exception:
            pass
    d.setdefault("status", "error")
    d["short"] = short
    d["returncode"] = proc.returncode
    d["wall_s"] = round(time.time() - started, 1)
    return d

def refresh_report(all_rows: list[dict], models: list[str]) -> None:
    OUT.mkdir(parents=True, exist_ok=True)
    lines = ["# spec-LLM-RTL on 49 RTLLM designs", "",
             f"Updated: {time.strftime('%Y-%m-%d %H:%M:%S')}", ""]
    lines += ["## Summary", "", "| model | done | passed | failed | compile_failed | error | pass@1 | avg tokens |",
              "|---|---:|---:|---:|---:|---:|---:|---:|"]
    for m in models:
        rows = [r for r in all_rows if r.get("_model") == m]
        c = {k: sum(1 for r in rows if r.get("status") == k)
             for k in ("passed", "failed", "compile_failed", "error")}
        n = len(rows); p = c["passed"]
        toks = [int((r.get("llm_usage") or {}).get("total_tokens") or 0) for r in rows]
        avg = sum(toks) / len(toks) if toks else 0
        lines.append(f"| {LABEL_NAME.get(m,m)} | {n}/49 | {p} | {c['failed']} | "
                     f"{c['compile_failed']} | {c['error']} | {100*p/n if n else 0:.1f}% | {avg:,.0f} |")
    lines += ["", "## Per-case", "", "| case | " + " | ".join(LABEL_NAME.get(m,m) for m in models) + " |",
              "|---|" + "---|" * len(models)]
    by = {(r.get("_model"), r.get("short")): r for r in all_rows}
    for case in cases():
        cells = []
        for m in models:
            r = by.get((m, case["short"]))
            cells.append("—" if not r else (r.get("status") or "error"))
        lines.append(f"| {case['short']} | " + " | ".join(cells) + " |")
    (OUT / "RESULTS.md").write_text("\n".join(lines) + "\n", encoding="utf-8")

def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--models", default="deepseek_aic,qwen")
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()
    models = [m for m in args.models.split(",") if m]
    cs = cases()[:args.limit or None]
    tasks = [(m, c) for m in models for c in cs]
    OUT.mkdir(parents=True, exist_ok=True)
    progress = OUT / "progress.jsonl"
    rows = []
    print(f"tasks={len(tasks)} models={models} workers={args.workers}", flush=True)
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futs = {pool.submit(run_one, m, c): (m, c) for m, c in tasks}
        for i, fut in enumerate(as_completed(futs), 1):
            m, c = futs[fut]
            try:
                d = fut.result()
            except Exception as exc:  # noqa: BLE001
                d = {"status": "error", "error": f"{type(exc).__name__}: {exc}"}
            d["_model"] = m
            rows.append(d)
            with progress.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps({k: v for k, v in d.items() if not k.startswith("_")}, ensure_ascii=False) + "\n")
            tok = (d.get("llm_usage") or {}).get("total_tokens")
            print(f"[{i}/{len(tasks)}] {m}/{c['short']} {d.get('status')} "
                  f"{str(tok or '')} tok {d.get('wall_s','')}s", flush=True)
            if i % 2 == 0 or i == len(tasks):
                refresh_report(rows, models)
    for m in models:
        for c in cs:
            p = OUT / m / c["short"] / "result.json"
            if p.is_file() and not any(r.get("_model") == m and r.get("short") == c["short"] for r in rows):
                d = json.loads(p.read_text()); d["_model"] = m; rows.append(d)
    refresh_report(rows, models)
    print("wrote", OUT / "RESULTS.md", flush=True)

if __name__ == "__main__":
    main()
