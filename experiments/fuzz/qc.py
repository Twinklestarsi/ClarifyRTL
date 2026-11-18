#!/usr/bin/env python3
"""Quick QC report for generated ambiguity sites."""

from __future__ import annotations

import json
from pathlib import Path

HERE = Path(__file__).resolve().parent
OUT = HERE / "out"

HEDGES = ["not necessarily", "may or may not", "unclear",
          "depends on interpretation", "open to interpretation"]


def main() -> None:
    rows = []
    for path in sorted(OUT.glob("*/*/result.json")):
        data = json.loads(path.read_text(encoding="utf-8"))
        if data.get("status") != "passed":
            continue
        for site in data.get("sites", []):
            flags = []
            original = site.get("original_text") or ""
            replacement = site.get("replacement_text") or ""
            question = site.get("clarification_question") or ""
            if float(site.get("confidence", 1.0)) < 0.7:
                flags.append("low_confidence")
            if len(original) < 15:
                flags.append("short_original")
            if len(replacement) > len(original):
                flags.append("replacement_longer")
            lowered = replacement.lower()
            if any(h in lowered for h in HEDGES):
                flags.append("hedge_phrase")
            if len(question) < 20:
                flags.append("short_question")
            rows.append({
                "dataset": data["dataset"],
                "short": data["short"],
                "site_id": site.get("id"),
                "category": site.get("category"),
                "mutation": site.get("mutation"),
                "confidence": site.get("confidence"),
                "question": question,
                "flags": flags,
            })

    flagged = [r for r in rows if r["flags"]]
    print(f"sites={len(rows)} flagged={len(flagged)}")
    by_flag: dict[str, int] = {}
    for row in flagged:
        for flag in row["flags"]:
            by_flag[flag] = by_flag.get(flag, 0) + 1
    print("flags:", dict(sorted(by_flag.items())))

    with (OUT / "qc.jsonl").open("w", encoding="utf-8") as fh:
        for row in rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")

    lines = ["# Ambiguity QC", "", f"- sites: {len(rows)}",
             f"- flagged: {len(flagged)}", "",
             "| dataset | case | site | category | flags | question |",
             "|---|---|---|---|---|---|"]
    for row in flagged:
        lines.append(f"| {row['dataset']} | {row['short']} | {row['site_id']} | "
                     f"{row['category']} | {', '.join(row['flags'])} | "
                     f"{row['question'][:100]} |")
    (OUT / "QC.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


if __name__ == "__main__":
    main()