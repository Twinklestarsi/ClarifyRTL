#!/usr/bin/env python3
"""Summarise finished runs: python src/report.py [arm]"""
import sys
from pathlib import Path
HERE = Path(__file__).resolve(); ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(HERE.parents[3]))
from blocks.b1_scaffold import load_config
from blocks.b8_eval.metrics import latest_per_case, load_results, summarise, write_csv, write_report

config = load_config()
rows = latest_per_case(load_results(config["run"]["output_root"]))
arm = sys.argv[1] if len(sys.argv) > 1 else None
if arm:
    rows = [row for row in rows if row.get("arm") == arm]
out = Path(config["run"]["output_root"]) / "_batch"
write_csv(rows, out / f"summary_{arm or 'all'}.csv")
write_report(rows, out / f"report_{arm or 'all'}.md", title=f"批量结果（{arm or '全部臂'}）")
data = summarise(rows, arm)
print({k: v for k, v in data.items() if k != "status_counts"})
print("status_counts:", data["status_counts"])
print(out / f"report_{arm or 'all'}.md")
