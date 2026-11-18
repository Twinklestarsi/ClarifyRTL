#!/usr/bin/env python3
"""Build paired clean/ambiguous manifests for the comparison experiments."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE

PAIRS = {
    "rtllm": (DATA / "group1/rtllm", DATA / "group1/rtllm_ambig",
              DATA / "group1/pairs.json"),
    "verilogeval": (DATA / "group1/verilogeval",
                    DATA / "group1/verilogeval_ambig",
                    DATA / "group1/pairs.json"),
    "tempo": (DATA / "group2/tempo", DATA / "group2/tempo_ambig",
              DATA / "group2/pairs.json"),
}


def load(root: Path) -> dict[str, dict]:
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    return {row["short"]: row for row in manifest["cases"]}


def main() -> None:
    grouped: dict[Path, dict] = {}
    for dataset, (clean_root, ambig_root, out_path) in PAIRS.items():
        if not (ambig_root / "manifest.json").is_file():
            print(f"skip {dataset}: no ambiguous manifest")
            continue
        clean = load(clean_root)
        ambig = load(ambig_root)
        common = sorted(set(clean) & set(ambig))
        rows = []
        for short in common:
            c = clean[short]
            a = ambig[short]
            rows.append({
                "dataset": c["dataset"],
                "short": short,
                "top_module": c["top_module"],
                "n_sites": a.get("n_sites"),
                "categories": a.get("categories"),
                "clean_prompt": str(clean_root / c["prompt"]),
                "ambig_prompt": str(ambig_root / a["prompt"]),
                "reference": str(clean_root / c["reference"]),
                "testbench": str(clean_root / c["testbench"]),
                "ambiguity": str(ambig_root / a["ambiguity"]),
            })
        entry = {
            "clean_root": str(clean_root),
            "ambig_root": str(ambig_root),
            "clean_cases": len(clean),
            "ambig_cases": len(ambig),
            "paired_cases": len(rows),
            "cases": rows,
        }
        grouped.setdefault(out_path, {})[dataset] = entry

    for out_path, datasets in grouped.items():
        payload = {
            "schema_version": 1,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "datasets": datasets,
            "counts": {k: v["paired_cases"] for k, v in datasets.items()},
        }
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2)
                            + "\n", encoding="utf-8")
        print(out_path, payload["counts"])


if __name__ == "__main__":
    main()