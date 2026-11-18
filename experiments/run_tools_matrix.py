#!/usr/bin/env python3
"""Run CPPL / AutoChip / RTLCoder over all paired datasets.

* every case gets its own result.json immediately;
* progress is appended to progress.jsonl as soon as a case finishes;
* failed adapter calls are retried (LLM/network failures);
* rerunning the same tag skips cases that already have a result.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
ADAPTERS = HERE / "adapters"
RUNNER = HERE / "runner_cases"
PY = Path("/nas2nfs/homes/2531930/ONE-TURN/dac2026_c1/.venv_c1/bin/python")
CPPL_PY = ADAPTERS / ".venv_cppl/bin/python"
LOCK = threading.Lock()

LABELS = ["RTLLM", "RTLLM_AMBIG", "VERILOGEVAL", "VERILOGEVAL_AMBIG",
          "TEMPO", "TEMPO_AMBIG"]


def cases(label: str, max_cases: int = 0, round_idx: int = 0,
          round_size: int = 0) -> list[str]:
    data = json.loads((RUNNER / "manifest.json").read_text(encoding="utf-8"))
    rows = [r for r in data["cases"] if r["dataset"] == label]
    shorts = sorted({r["short"] for r in rows})
    if max_cases:
        shorts = shorts[:max_cases]
    if round_size:
        shorts = shorts[round_idx * round_size:(round_idx + 1) * round_size]
    return shorts


def env() -> dict[str, str]:
    e = os.environ.copy()
    for key in list(e):
        if key.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy",
                           "socks_proxy", "socks5_proxy"}:
            e.pop(key, None)
    e["PATH"] = "/home/2531930/nas/2531930-2/eda_tools/conda/bin:" + e.get("PATH", "")
    return e


def command(tool: str, case: str, model: str, out: Path) -> tuple[list[str], dict]:
    if tool == "cppl":
        cmd = [str(CPPL_PY), "run_cppl.py", "--case", case, "--model", model,
               "--out", str(out), "--quiet"]
    elif tool == "autochip":
        cmd = [str(PY), "run_autochip.py", "--case", case, "--model", model,
               "--out", str(out), "--iterations", "3"]
    elif tool == "rtlcoder":
        cmd = [str(PY), "run_rtlcoder.py", "--case", case, "--model", model,
               "--mode", "api_prompt", "--out", str(out)]
    elif tool == "spec_llm":
        cmd = [str(PY), "run_spec_llm.py", "--case", case, "--model", model,
               "--out", str(out)]
    else:
        raise ValueError(tool)
    return cmd, env()


def run_one(tool: str, model: str, repeat: int, label: str, short: str,
            tag: str, max_retries: int = 2) -> dict:
    out = HERE / "runs" / "tools_matrix" / tag / tool / model / label / f"r{repeat}" / short
    result_path = out / "result.json"
    if result_path.is_file():
        try:
            prev = json.loads(result_path.read_text(encoding="utf-8"))
            if prev.get("status") in ("passed", "failed", "compile_failed",
                                      "timeout"):
                return {"tool": tool, "model": model, "label": label,
                        "short": short, "repeat": repeat, "status": "cached",
                        "result": str(result_path)}
        except Exception:
            pass
    cmd, e = command(tool, f"{label}/{short}", model, out)
    started = time.time()
    last = None
    for attempt in range(max_retries + 1):
        out.mkdir(parents=True, exist_ok=True)
        log = out / f"adapter_{attempt}.log"
        with log.open("w", encoding="utf-8") as fh:
            proc = subprocess.run(cmd, cwd=str(ADAPTERS), env=e,
                                  stdout=fh, stderr=subprocess.STDOUT,
                                  timeout=3600)
        if result_path.is_file():
            data = json.loads(result_path.read_text(encoding="utf-8"))
            status = data.get("status", "error")
            last = {"status": status, "error": data.get("error")}
            if status in ("passed", "failed", "compile_failed", "timeout"):
                return {"tool": tool, "model": model, "label": label,
                        "short": short, "repeat": repeat, "status": status,
                        "elapsed_s": round(time.time() - started, 2),
                        "result": str(result_path)}
        last = {"status": "error", "returncode": proc.returncode,
                "log": str(log)}
        time.sleep(2 + attempt * 3)
    return {"tool": tool, "model": model, "label": label, "short": short,
            "repeat": repeat, "status": "error", "elapsed_s":
            round(time.time() - started, 2), **(last or {})}


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--tools", default="cppl,autochip,rtlcoder,spec_llm")
    ap.add_argument("--models", default="deepseek,qwen")
    ap.add_argument("--repeats", type=int, default=2)
    ap.add_argument("--workers", type=int, default=6)
    ap.add_argument("--max-cases", type=int, default=0)
    ap.add_argument("--round", type=int, default=0)
    ap.add_argument("--round-size", type=int, default=0)
    ap.add_argument("--labels", default=",".join(LABELS))
    ap.add_argument("--tag", default=None)
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    tag = args.tag or datetime.now().strftime("%Y%m%dT%H%M%S")
    labels = [x for x in args.labels.split(",") if x]
    tasks = []
    for tool in args.tools.split(","):
        for model in args.models.split(","):
            for repeat in range(args.repeats):
                for label in labels:
                    for short in cases(label, args.max_cases,
                                       args.round, args.round_size):
                        tasks.append((tool, model, repeat, label, short))
    random.Random(20261115).shuffle(tasks)
    print(f"tasks={len(tasks)} tag={tag} workers={args.workers}", flush=True)
    if args.dry_run:
        return
    progress = HERE / "runs" / "tools_matrix" / tag / "progress.jsonl"
    progress.parent.mkdir(parents=True, exist_ok=True)
    with progress.open("a", encoding="utf-8") as fh:
        pass
    done = 0
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {pool.submit(run_one, *task, tag): task for task in tasks}
        for future in as_completed(futures):
            try:
                row = future.result()
            except Exception as exc:  # noqa: BLE001
                task = futures[future]
                row = {"tool": task[0], "model": task[1], "repeat": task[2],
                       "label": task[3], "short": task[4],
                       "status": "error", "error": f"{type(exc).__name__}: {exc}"}
            row["ts"] = datetime.now().isoformat()
            with LOCK:
                done += 1
                with progress.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(row, ensure_ascii=False) + "\n")
                print(f"[{done}/{len(tasks)}] {row['tool']}/{row['model']}/"
                      f"{row['label']}/{row['short']} r{row['repeat']} "
                      f"{row['status']}", flush=True)


if __name__ == "__main__":
    main()
