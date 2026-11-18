#!/usr/bin/env python3
"""Build the case root consumed by ours and by the tool adapters.

Labels:
  RTLLM / RTLLM_AMBIG / VERILOGEVAL / VERILOGEVAL_AMBIG / TEMPO / TEMPO_AMBIG
Only paired cases (present in both clean and ambiguous) are included.
"""
from __future__ import annotations
import json, shutil
from pathlib import Path

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
OUT = HERE / "runner_cases"

GROUPS = [
    ("group1/pairs.json", {
        "rtllm": ("RTLLM", "RTLLM_AMBIG"),
        "verilogeval": ("VERILOGEVAL", "VERILOGEVAL_AMBIG"),
    }),
    ("group2/pairs.json", {"tempo": ("TEMPO", "TEMPO_AMBIG")}),
]


def main() -> None:
    cases_root = OUT / "cases"
    if cases_root.exists() and any(cases_root.iterdir()):
        raise SystemExit(f"refusing to overwrite non-empty: {cases_root}")
    rows = []
    for pair_file, mapping in GROUPS:
        pair = json.loads((DATA / pair_file).read_text(encoding="utf-8"))
        for ds, entry in pair["datasets"].items():
            clean_label, ambig_label = mapping[ds]
            for row in entry["cases"]:
                for label, prompt_key in ((clean_label, "clean_prompt"),
                                          (ambig_label, "ambig_prompt")):
                    short = row["short"]
                    target = cases_root / label / short
                    target.mkdir(parents=True, exist_ok=True)
                    shutil.copyfile(row[prompt_key], target / "prompt.md")
                    shutil.copyfile(row["reference"], target / Path(row["reference"]).name)
                    shutil.copyfile(row["testbench"], target / Path(row["testbench"]).name)
                    (target / "evidence.json").write_text("{}\n", encoding="utf-8")
                    name_ref = Path(row["reference"]).name
                    name_tb = Path(row["testbench"]).name
                    rows.append({
                        "case_id": f"{label}/{short}",
                        "dataset": label,
                        "short": short,
                        "top_module": row["top_module"],
                        "prompt": f"cases/{label}/{short}/prompt.md",
                        "reference": f"cases/{label}/{short}/{name_ref}",
                        "testbench": f"cases/{label}/{short}/{name_tb}",
                        "generated": None,
                        "evidence": f"cases/{label}/{short}/evidence.json",
                        "n_sites": row.get("n_sites"),
                    })
    manifest = {"schema_version": 1, "total": len(rows), "cases": rows}
    (OUT / "manifest.json").write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
                                       encoding="utf-8")
    print(json.dumps({k: sum(1 for r in rows if r["dataset"] == k)
                      for k in sorted({r["dataset"] for r in rows})}, ensure_ascii=False))


if __name__ == "__main__":
    main()
