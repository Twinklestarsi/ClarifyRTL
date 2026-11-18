#!/usr/bin/env python3
"""Assemble the ambiguous datasets from fuzz_specs.py results.

Reads ``experiments/fuzz/out/<dataset>/<short>/result.json`` and writes:

    experiments/data/group1/rtllm_ambig/
    experiments/data/group1/verilogeval_ambig/
    experiments/data/group2/tempo_ambig/

Every ambiguous case keeps the clean reference and testbench, replaces
``prompt.md`` with the edited spec, and stores the full site annotation in
``ambiguity.json`` (author-side only; do not show it to the solver).

The clean dataset is never modified.  The script also writes a paired manifest
and a human review file.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import shutil
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
EXPERIMENTS = HERE.parent
DATA = EXPERIMENTS / "data"
OUT = HERE / "out"

TARGETS = {
    "rtllm": (DATA / "group1/rtllm", DATA / "group1/rtllm_ambig"),
    "verilogeval": (DATA / "group1/verilogeval",
                    DATA / "group1/verilogeval_ambig"),
    "tempo": (DATA / "group2/tempo", DATA / "group2/tempo_ambig"),
}


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def load_clean(clean_root: Path) -> dict[str, dict]:
    manifest = json.loads((clean_root / "manifest.json").read_text(
        encoding="utf-8"))
    return {row["short"]: row for row in manifest["cases"]}


def build(dataset: str, *, force: bool) -> dict:
    clean_root, ambig_root = TARGETS[dataset]
    clean = load_clean(clean_root)
    results = []
    for case_dir in sorted((OUT / dataset).iterdir()):
        if not case_dir.is_dir():
            continue
        result_path = case_dir / "result.json"
        if not result_path.is_file():
            continue
        result = json.loads(result_path.read_text(encoding="utf-8"))
        results.append(result)

    passed = [r for r in results if r.get("status") == "passed"]
    failed = [r for r in results if r.get("status") != "passed"]
    if not passed:
        return {"dataset": dataset, "passed": 0, "failed": len(failed),
                "skipped": True}

    if (ambig_root / "cases").exists() and any((ambig_root / "cases").iterdir()):
        if not force:
            raise SystemExit(f"refusing to overwrite non-empty: {ambig_root}")
    if force and (ambig_root / "cases").exists():
        shutil.rmtree(ambig_root / "cases")
    if force and (ambig_root / "annotations").exists():
        shutil.rmtree(ambig_root / "annotations")

    cases = []
    site_rows = []
    for result in passed:
        short = result["short"]
        clean_row = clean.get(short)
        if clean_row is None:
            raise RuntimeError(f"{dataset}/{short}: not in clean manifest")
        clean_case = clean_root / "cases" / short
        target = ambig_root / "cases" / short
        if target.exists():
            shutil.rmtree(target)
        target.mkdir(parents=True)
        edited = result["edited_spec"]
        (target / "prompt.md").write_text(edited, encoding="utf-8")
        shutil.copyfile(clean_case / Path(clean_row["reference"]).name,
                        target / Path(clean_row["reference"]).name)
        shutil.copyfile(clean_case / Path(clean_row["testbench"]).name,
                        target / Path(clean_row["testbench"]).name)
        if (clean_case / "interface.txt").is_file():
            shutil.copyfile(clean_case / "interface.txt",
                            target / "interface.txt")
        clean_prompt = (clean_case / "prompt.md").read_text(encoding="utf-8")
        ambiguity = {
            "case_id": result["case_id"],
            "dataset": result["label"],
            "short": short,
            "top_module": result["top_module"],
            "n_sites": len(result["sites"]),
            "target_sites": result.get("target_sites"),
            "salvaged": bool(result.get("salvaged")),
            "dropped_sites": result.get("dropped_sites", []),
            "original_prompt_sha256": sha256_text(clean_prompt),
            "edited_prompt_sha256": sha256_text(edited),
            "original_spec": clean_prompt,
            "sites": result["sites"],
            "audit": result.get("audit", {}),
            "generated_at": result.get("generated_at"),
            "usage": result.get("usage", {}),
        }
        # Author-side annotations live outside the solver-visible case dir.
        ann_dir = ambig_root / "annotations"
        write_json(ann_dir / f"{short}.json", ambiguity)
        meta = json.loads((clean_case / "meta.json").read_text(encoding="utf-8"))
        meta["ambiguous"] = True
        meta["n_sites"] = len(result["sites"])
        meta["categories"] = [s.get("category") for s in result["sites"]]
        meta["fuzz_attempts"] = result.get("attempts")
        write_json(ann_dir / f"{short}.meta.json", meta)
        categories = Counter(s.get("category") for s in result["sites"])
        cases.append({
            "case_id": result["case_id"],
            "dataset": result["label"],
            "short": short,
            "top_module": result["top_module"],
            "n_sites": len(result["sites"]),
            "categories": dict(categories),
            "prompt": f"cases/{short}/prompt.md",
            "reference": f"cases/{short}/{Path(clean_row['reference']).name}",
            "testbench": f"cases/{short}/{Path(clean_row['testbench']).name}",
            "ambiguity": f"annotations/{short}.json",
            "meta": f"annotations/{short}.meta.json",
        })
        for site in result["sites"]:
            site_rows.append({
                "dataset": result["label"],
                "short": short,
                "site_id": site.get("id"),
                "category": site.get("category"),
                "mutation": site.get("mutation"),
                "question": site.get("clarification_question"),
                "original_text": site.get("original_text"),
                "replacement_text": site.get("replacement_text"),
            })

    manifest = {
        "schema_version": 1,
        "dataset": cases[0]["dataset"] if cases else dataset,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "counts": {"cases": len(cases),
                   "sites": sum(c["n_sites"] for c in cases),
                   "failed_cases": len(failed)},
        "site_count_distribution": dict(Counter(c["n_sites"] for c in cases)),
        "category_distribution": dict(Counter(
            s.get("category") for r in passed for s in r["sites"])),
        "cases": cases,
    }
    write_json(ambig_root / "manifest.json", manifest)
    write_json(ambig_root / "failures.json", [
        {"case_id": r.get("case_id"), "short": r.get("short"),
         "status": r.get("status"), "errors": r.get("errors", [])}
        for r in failed
    ])

    # Human-readable review, one row per site.
    lines = [f"# {dataset} ambiguity review", "",
             f"- cases: {len(cases)}",
             f"- sites: {sum(c['n_sites'] for c in cases)}",
             f"- failed: {len(failed)}", "",
             "| case | sites | categories | prompt chars |",
             "|---|---:|---|---:|"]
    for case in cases:
        lines.append(f"| {case['short']} | {case['n_sites']} | "
                     f"{', '.join(f'{k}:{v}' for k, v in case['categories'].items())} | "
                     f"{len((ambig_root / case['prompt']).read_text(encoding='utf-8'))} |")
    (ambig_root / "REVIEW.md").write_text("\n".join(lines) + "\n",
                                          encoding="utf-8")
    with (ambig_root / "sites.jsonl").open("w", encoding="utf-8") as fh:
        for row in site_rows:
            fh.write(json.dumps(row, ensure_ascii=False) + "\n")
    return {"dataset": dataset, "passed": len(cases),
            "sites": manifest["counts"]["sites"], "failed": len(failed)}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", choices=["rtllm", "verilogeval", "tempo",
                                              "all"], default="all")
    parser.add_argument("--force", action="store_true")
    args = parser.parse_args()
    names = list(TARGETS) if args.dataset == "all" else [args.dataset]
    summary = [build(name, force=args.force) for name in names]
    print(json.dumps(summary, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()