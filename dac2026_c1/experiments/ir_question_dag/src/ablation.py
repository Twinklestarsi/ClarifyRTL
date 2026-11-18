#!/usr/bin/env python3
"""Run the two ablation groups and write a compact comparison table.

Group 1: question-phase ablation (A0-A4, repair=rewrite=0)
Group 2: repair/rewrite ablation (A2, several round pairs)

Each combo gets its own output_root so batch summaries and run directories do
not collide.  Results are written under runs/_ablation/<tag>/.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve()
ROOT = HERE.parents[1]
PROJECT = HERE.parents[3]
for path in (str(ROOT), str(PROJECT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from blocks.b1_scaffold import (load_config, load_dotenv, provider_env,
                                sanitize_network_env, patch_library_clients)
from blocks.b8_eval.batch import run_batch
from run_case import run_case


GROUP1_ARMS = [
    "A0_no_question",
    "A1_fixed",
    "A2_ranked_prune",
    "A3_ranked_no_prune",
    "A4_random",
]

GROUP2_COMBOS = [
    ("repair0_rewrite0", 0, 0),
    ("repair4_rewrite0", 4, 0),
    ("repair4_rewrite4", 4, 4),
    ("repair8_rewrite12", 8, 12),
]


def _cases(group: str, arm: str) -> list[dict]:
    if group == "1":
        return [{"name": f"g1_{arm}", "group": group, "arm": arm,
                 "repair": 0, "rewrite": 0} for arm in GROUP1_ARMS]
    return [{"name": f"g2_{name}", "group": group, "arm": arm,
             "repair": repair, "rewrite": rewrite}
            for name, repair, rewrite in GROUP2_COMBOS]


def _latest_batch(output_root: Path, arm: str) -> dict | None:
    files = sorted((output_root / "_batch").glob(f"{arm}_*.json"),
                   key=lambda path: path.stat().st_mtime)
    if not files:
        return None
    return json.loads(files[-1].read_text(encoding="utf-8"))


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--group", choices=("1", "2", "all"), default="all")
    parser.add_argument("--dataset", default="Tempo26")
    parser.add_argument("--arm", default="A2_ranked_prune",
                        help="arm used by group 2")
    parser.add_argument("--workers", type=int, default=4)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--only", default="")
    parser.add_argument("--config", default=None)
    parser.add_argument("--tag", default=None)
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args(argv)

    groups = ("1", "2") if args.group == "all" else (args.group,)
    tag = args.tag or time.strftime("%Y%m%dT%H%M%S")
    config = load_config(args.config)
    base_output = Path(config["run"]["output_root"]) / "_ablation" / tag
    only = [name for name in args.only.split(",") if name] or None
    matrix = []
    for group in groups:
        matrix.extend(_cases(group, args.arm))

    if args.dry_run:
        for item in matrix:
            print(f"{item['name']:28s} arm={item['arm']:22s} "
                  f"repair={item['repair']} rewrite={item['rewrite']}")
        return 0

    sanitize_network_env()
    load_dotenv()
    patch_library_clients()
    env = provider_env()
    base_output.mkdir(parents=True, exist_ok=True)

    rows: list[dict] = []
    for item in matrix:
        output_root = base_output / item["name"]
        combo_config = copy.deepcopy(config)
        combo_config["run"]["output_root"] = str(output_root)
        phase_c = combo_config.setdefault("phase_c", {})
        phase_c["repair_rounds"] = item["repair"]
        phase_c["rewrite_rounds"] = item["rewrite"]
        print("\n" + "=" * 78)
        print(f"ABLATION {item['name']} | arm={item['arm']} | "
              f"repair={item['repair']} rewrite={item['rewrite']} | "
              f"dataset={args.dataset} workers={args.workers}")
        print("=" * 78, flush=True)
        run_batch(combo_config, env, arm=item["arm"], runner=run_case,
                  dataset=args.dataset, workers=args.workers, only=only,
                  limit=args.limit)
        batch = _latest_batch(output_root, item["arm"])
        if batch is None:
            row = {"name": item["name"], "group": item["group"],
                   "arm": item["arm"], "repair": item["repair"],
                   "rewrite": item["rewrite"], "cases": 0,
                   "equivalent": 0, "rate": 0.0, "bounded": 0,
                   "elapsed_s": 0.0, "batch": None}
        else:
            row = {"name": item["name"], "group": item["group"],
                   "arm": item["arm"], "repair": item["repair"],
                   "rewrite": item["rewrite"],
                   "cases": batch.get("cases"),
                   "equivalent": batch.get("equivalent"),
                   "rate": batch.get("rate"),
                   "bounded": batch.get("bounded_equivalent"),
                   "elapsed_s": batch.get("elapsed_s"),
                   "batch": str((output_root / "_batch").relative_to(base_output))}
        rows.append(row)
        (base_output / "summary.json").write_text(
            json.dumps({"tag": tag, "dataset": args.dataset,
                        "rows": rows}, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")

    base_output.mkdir(parents=True, exist_ok=True)
    with (base_output / "summary.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=list(rows[0]) if rows else [])
        if rows:
            writer.writeheader()
            writer.writerows(rows)
    lines = [f"# Ablation {tag}", "",
             f"Dataset: `{args.dataset}`; workers: {args.workers}", "",
             "| Name | Group | Arm | repair | rewrite | equivalent | rate | elapsed |",
             "|---|---|---|---:|---:|---:|---:|---:|"]
    for row in rows:
        lines.append(
            f"| {row['name']} | {row['group']} | {row['arm']} | "
            f"{row['repair']} | {row['rewrite']} | {row['equivalent']}/{row['cases']} | "
            f"{row['rate']} | {row['elapsed_s']}s |")
    (base_output / "report.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\nWrote {base_output/'summary.json'}, summary.csv, report.md")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
