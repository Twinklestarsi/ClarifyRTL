#!/usr/bin/env python3
"""Rerun RTLLM cases whose latest result is still JasperGold."""
import glob, json, os, re, subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

HERE = Path(__file__).resolve().parent
DAG = HERE.parent / "dac2026_c1/experiments/ir_question_dag"
PYTHON = "/nas2nfs/homes/2531930/ONE-TURN/dac2026_c1/.venv_c1/bin/python"
ENV = {k: v for k, v in os.environ.items()}
for k in list(ENV):
    if k.lower() in {"http_proxy", "https_proxy", "all_proxy", "no_proxy",
                     "socks_proxy", "socks5_proxy"}:
        ENV.pop(k, None)


def enver(model):
    e = dict(ENV)
    e["OPENAI_API_KEY"] = next(
        line.split("=", 1)[1].strip()
        for line in Path("/nas2nfs/homes/2531930/ONE-TURN/DAG/.env").read_text().splitlines()
        if line.startswith("DAG_API_KEY="))
    e["OPENAI_BASE_URL"] = "https://aic.tongjieda.cn/v1"
    e["OPENAI_MODEL"] = ("deepseek-v4.1-flash-1017" if model == "deepseek"
                         else "qwen3.8-27b")
    return e


def main():
    rows = []
    for p in glob.glob(str(HERE / "runs/ours_matrix/run1/*/*/rtllm/r[01]/*/2*/result.json")):
        if "/formal/" in p:
            continue
        try:
            d = json.load(open(p))
        except Exception:
            continue
        if (d.get("formal") or {}).get("method") != "jaspergold":
            continue
        parts = Path(p).parts
        system, model, repeat = parts[-7], parts[-6], parts[-4]
        label, short = Path(p).parents[1].name.split("__", 1)
        cfg = HERE / f"runs/ours_matrix/run1/configs/{system}_{model}_rtllm_{repeat}.yaml"
        rows.append((system, model, repeat, label, short, cfg))
    print("targets", len(rows))
    def task(row):
        system, model, repeat, label, short, cfg = row
        cmd = [PYTHON, "-u", "src/run_case.py", "--all", "--dataset", label,
               "--only", short, "--arm", "A2_ranked_prune", "--config", str(cfg)]
        log = HERE / "runs" / f"rerun_{system}_{model}_{repeat}_{label}_{short}.log"
        with log.open("w") as fh:
            r = subprocess.run(cmd, cwd=str(DAG), env=enver(model),
                               stdout=fh, stderr=subprocess.STDOUT, timeout=3600)
        print(system, model, repeat, label, short, "rc", r.returncode, flush=True)
        return row
    with ThreadPoolExecutor(max_workers=5) as pool:
        list(pool.map(task, rows))


if __name__ == "__main__":
    main()
