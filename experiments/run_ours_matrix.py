#!/usr/bin/env python3
"""Run the 'ours' variants over paired clean/ambiguous datasets.

Each job = one (system, model, family, repeat) combination.  Clean and
ambiguous labels are run back-to-back inside the same job so the pair is
always evaluated together.

Example:
  python run_ours_matrix.py --families tempo --systems ours,ours_no_dag,ours_no_feedback \
      --models deepseek --repeats 5 --workers 4 --max-cases 2
"""
from __future__ import annotations

import argparse
import json
import os
import random
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

HERE = Path(__file__).resolve().parent
WORKSPACE = HERE.parents[1]
DAG_ROOT = HERE.parent / "dac2026_c1/experiments/ir_question_dag"
PYTHON = Path("/nas2nfs/homes/2531930/ONE-TURN/dac2026_c1/.venv_c1/bin/python")
BASE_CONFIG = HERE.parent / "config_version2.yaml"
import yaml

FAMILIES = {
    "rtllm": ("group1/pairs.json", "rtllm", "RTLLM", "RTLLM_AMBIG"),
    "verilogeval": ("group1/pairs.json", "verilogeval", "VERILOGEVAL", "VERILOGEVAL_AMBIG"),
    "tempo": ("group2/pairs.json", "tempo", "TEMPO", "TEMPO_AMBIG"),
}

SYSTEMS = {
    "ours": {"arm": "A2_ranked_prune", "repair": 4, "rewrite": 4},
    "ours_no_dag": {"arm": "A0_no_question", "repair": 4, "rewrite": 4},
    "ours_no_feedback": {"arm": "A2_ranked_prune", "repair": 0, "rewrite": 0},
}

MODELS = {
    "deepseek": {"model": "deepseek-flash", "base": "https://api.deepseek.com/v1",
                 "key_env": "DEEPSEEK_API_KEY"},
    "qwen": {"model": "qwen3.8-27b", "base": "https://aic.tongjieda.cn/v1",
             "key_env": "DAG_API_KEY"},
    "mimo": {"model": "MiMo-V2.6-Flash", "base": "https://agi.tongji.edu.cn/v1",
             "key_env": "AGI_API_KEY"},
    "glm": {"model": "GLM-5.3-Flash", "base": "https://agi.tongji.edu.cn/v1",
            "key_env": "AGI_API_KEY_2"},
}


def dotenv(path: Path) -> dict[str, str]:
    out = {}
    for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
        line = raw.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            out[k.strip()] = v.strip().strip('"').strip("'")
    return out


def pair_rows(family: str, max_cases: int, round_idx: int = 0,
              round_size: int = 0) -> tuple[str, str, list[str]]:
    pair_file, ds, clean, ambig = FAMILIES[family]
    data = json.loads((HERE / "data" / pair_file).read_text(encoding="utf-8"))
    entry = data["datasets"][ds]
    shorts = sorted({row["short"] for row in entry["cases"]})
    if max_cases:
        rng = random.Random(20261115)
        shorts = sorted(rng.sample(shorts, min(max_cases, len(shorts))))
    if round_size:
        shorts = shorts[round_idx * round_size:(round_idx + 1) * round_size]
    return clean, ambig, shorts


def run_job(system: str, model: str, family: str, repeat: int,
            shorts: list[str], workers: int, tag: str,
            force: bool = False) -> dict:
    _, _, clean, ambig = FAMILIES[family]
    out_root = HERE / "runs" / "ours_matrix" / tag / system / model / family / f"r{repeat}"
    cfg_dir = HERE / "runs" / "ours_matrix" / tag / "configs"
    cfg_dir.mkdir(parents=True, exist_ok=True)
    cfg_path = cfg_dir / f"{system}_{model}_{family}_r{repeat}.yaml"
    config = yaml.safe_load(BASE_CONFIG.read_text(encoding="utf-8"))
    config["run"]["case_root"] = str(HERE / "runner_cases")
    config["run"]["output_root"] = str(out_root)
    config["run"]["case_attempts"] = 1
    config["run"]["arms"] = [SYSTEMS[system]["arm"]]
    config["phase_c"]["repair_rounds"] = SYSTEMS[system]["repair"]
    config["phase_c"]["rewrite_rounds"] = SYSTEMS[system]["rewrite"]
    config["phase_c"]["repair_library"] = None
    config["phase_c"]["repair_library_disabled"] = True
    config["phase_c"]["truth_repair"] = False
    cfg_path.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")

    env = os.environ.copy()
    spec = MODELS[model]
    env.update({"OPENAI_API_KEY": os.environ[spec["key_env"]],
                "OPENAI_BASE_URL": spec["base"],
                "OPENAI_MODEL": spec["model"],
                "PYTHONUNBUFFERED": "1"})
    logs = out_root.parent / "logs"
    logs.mkdir(parents=True, exist_ok=True)
    results = {}
    # Interleave clean and ambiguous chunks so both sides of the pair make
    # progress together (and a killed run still leaves paired evidence).
    log = logs / "paired.log"
    with log.open("a", encoding="utf-8") as fh:
        for start in range(0, len(shorts), 2):
            chunk = shorts[start:start + 2]
            for label in (clean, ambig):
                cmd = [str(PYTHON), "-u", "src/run_case.py", "--all",
                       "--dataset", label, "--arm", SYSTEMS[system]["arm"],
                       "--config", str(cfg_path), "--workers", str(workers),
                       "--only", ",".join(chunk)]
                if not force:
                    cmd.append("--skip-existing")
                proc = subprocess.run(cmd, cwd=str(DAG_ROOT), env=env,
                                      stdout=fh, stderr=subprocess.STDOUT)
                results[f"{label}:{chunk[0]}"] = {
                    "returncode": proc.returncode, "log": str(log)}
            print(f"  {system}/{model}/{family}/r{repeat} "
                  f"paired chunk {chunk[0]}..{chunk[-1]} done", flush=True)
    batch = {}
    for label in (clean, ambig):
        files = sorted((out_root / "_batch").glob(f"{SYSTEMS[system]['arm']}_*.json"),
                       key=lambda p: p.stat().st_mtime)
        if files:
            batch[label] = json.loads(files[-1].read_text(encoding="utf-8"))
    record = {"system": system, "model": model, "family": family,
              "repeat": repeat, "shorts": shorts, "out_root": str(out_root),
              "config": str(cfg_path), "runs": results, "batch": batch}
    summary = out_root / "job.json"
    summary.write_text(json.dumps(record, ensure_ascii=False, indent=2) + "\n",
                       encoding="utf-8")
    return record


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--families", default="tempo")
    ap.add_argument("--systems", default="ours")
    ap.add_argument("--models", default="deepseek")
    ap.add_argument("--repeats", type=int, default=1)
    ap.add_argument("--workers", type=int, default=1,
                    help="per-job case workers passed to run_batch")
    ap.add_argument("--job-workers", type=int, default=6,
                    help="number of (system, model, family, repeat) jobs in parallel")
    ap.add_argument("--max-cases", type=int, default=0)
    ap.add_argument("--round", type=int, default=0,
                    help="0-based batch index; used with --round-size")
    ap.add_argument("--round-size", type=int, default=0,
                    help="cases per dataset per round (0 = no batching)")
    ap.add_argument("--tag", default=None)
    ap.add_argument("--force-families", default="",
                    help="comma-separated families to rerun even if results exist")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()
    env = dotenv(WORKSPACE / "DAG/.env")
    for key, value in env.items():
        os.environ.setdefault(key, value)
    for model in [x for x in args.models.split(",") if x]:
        key_env = MODELS[model]["key_env"]
        if not os.environ.get(key_env):
            raise SystemExit(f"{key_env} missing")

    tag = args.tag or datetime.now().strftime("%Y%m%dT%H%M%S")
    jobs = []
    for family in [x for x in args.families.split(",") if x]:
        clean, ambig, shorts = pair_rows(family, args.max_cases,
                                         args.round, args.round_size)
        for system in [x for x in args.systems.split(",") if x]:
            for model in [x for x in args.models.split(",") if x]:
                for repeat in range(args.repeats):
                    jobs.append((system, model, family, repeat, shorts))
    random.Random(20261115).shuffle(jobs)
    print(f"jobs={len(jobs)} tag={tag}", flush=True)
    if args.dry_run:
        for job in jobs:
            print(job[:4], len(job[4]))
        return
    records = []
    with ThreadPoolExecutor(max_workers=max(1, args.job_workers)) as pool:
        futures = {
            pool.submit(run_job, system, model, family, repeat, shorts,
                        args.workers, tag,
                        family in {f.strip() for f in
                                   args.force_families.split(",") if f.strip()}):
            (system, model, family, repeat)
            for system, model, family, repeat, shorts in jobs
        }
        for future in as_completed(futures):
            key = futures[future]
            try:
                records.append(future.result())
            except Exception as exc:  # noqa: BLE001
                records.append({"system": key[0], "model": key[1],
                                "family": key[2], "repeat": key[3],
                                "error": f"{type(exc).__name__}: {exc}"})
            print(f"DONE {key}", flush=True)
    records.sort(key=lambda r: (r.get("system", ""), r.get("model", ""),
                                r.get("family", ""), r.get("repeat", 0)))
    out = HERE / "runs" / "ours_matrix" / tag / "matrix.json"
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(records, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
