#!/usr/bin/env python3
"""Summarize fuzz_specs.py results."""

from __future__ import annotations

import json
from collections import Counter, defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"


def main() -> None:
    rows = []
    for dataset_dir in sorted(OUT.iterdir()):
        if not dataset_dir.is_dir():
            continue
        for case_dir in sorted(dataset_dir.iterdir()):
            path = case_dir / "result.json"
            if path.is_file():
                rows.append(json.loads(path.read_text(encoding="utf-8")))

    by_dataset = defaultdict(list)
    for row in rows:
        by_dataset[row["dataset"]].append(row)

    print("| dataset | cases | passed | failed | pass rate | sites | avg sites/case | tokens |")
    print("|---|---:|---:|---:|---:|---:|---:|---:|")
    total = Counter()
    for dataset, items in sorted(by_dataset.items()):
        passed = [r for r in items if r.get("status") == "passed"]
        sites = sum(len(r.get("sites", [])) for r in passed)
        tokens = sum(int(r.get("usage", {}).get("total_tokens", 0))
                     for r in items)
        total["cases"] += len(items)
        total["passed"] += len(passed)
        total["failed"] += len(items) - len(passed)
        total["sites"] += sites
        total["tokens"] += tokens
        rate = 100 * len(passed) / max(len(items), 1)
        avg = sites / max(len(passed), 1)
        print(f"| {dataset} | {len(items)} | {len(passed)} | "
              f"{len(items) - len(passed)} | {rate:.1f}% | {sites} | "
              f"{avg:.1f} | {tokens} |")
    rate = 100 * total["passed"] / max(total["cases"], 1)
    print(f"| **all** | {total['cases']} | {total['passed']} | "
          f"{total['failed']} | {rate:.1f}% | {total['sites']} | "
          f"{total['sites'] / max(total['passed'], 1):.1f} | "
          f"{total['tokens']} |")

    print()
    print("site count distribution:")
    for dataset, items in sorted(by_dataset.items()):
        passed = [r for r in items if r.get("status") == "passed"]
        print(f"- {dataset}: "
              f"{dict(sorted(Counter(len(r.get('sites', [])) for r in passed).items()))}")

    print()
    print("category distribution:")
    cats = Counter()
    for row in rows:
        for site in row.get("sites", []):
            cats[site.get("category")] += 1
    print(dict(sorted(cats.items())))

    summary = {
        "datasets": {
            dataset: {
                "cases": len(items),
                "passed": len([r for r in items if r.get("status") == "passed"]),
                "sites": sum(len(r.get("sites", [])) for r in items
                             if r.get("status") == "passed"),
                "tokens": sum(int(r.get("usage", {}).get("total_tokens", 0))
                              for r in items),
            }
            for dataset, items in sorted(by_dataset.items())
        },
        "totals": dict(total),
        "categories": dict(sorted(cats.items())),
        "failures": [
            {"dataset": r["dataset"], "short": r["short"],
             "status": r.get("status"),
             "errors": r.get("errors", [])}
            for r in rows if r.get("status") != "passed"
        ],
    }
    (HERE / "out" / "summary.json").write_text(
        json.dumps(summary, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    failures = [r for r in rows if r.get("status") != "passed"]
    if failures:
        print()
        print("failed cases:")
        for row in sorted(failures, key=lambda r: (r["dataset"], r["short"])):
            reason = (row.get("errors") or [""])[0]
            print(f"- {row['dataset']}/{row['short']}: {str(reason)[:180]}")


if __name__ == "__main__":
    main()