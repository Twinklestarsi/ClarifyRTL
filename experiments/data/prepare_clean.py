#!/usr/bin/env python3
"""Prepare the clean benchmark cases for the version2 comparison experiments.

Copies (never moves) the source specs / reference RTL / testbenches into a
uniform case layout:

    experiments/data/group1/rtllm/cases/<short>/{prompt.md,reference.v,testbench.v,meta.json}
    experiments/data/group1/verilogeval/cases/<short>/{prompt.md,reference.sv,testbench.sv,interface.txt,meta.json}
    experiments/data/group2/tempo/cases/<short>/{prompt.md,reference.sv,testbench.sv,meta.json}

VerilogEval selection: the 23 cases already used by the DAG error set plus a
deterministic random sample of 46 from the remaining 133 (seed 20261115).

Run from anywhere:

    python3 version2/experiments/data/prepare_clean.py

The script refuses to overwrite non-empty dataset directories.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import random
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path

HERE = Path(__file__).resolve().parent
VERSION2 = HERE.parents[1]
WORKSPACE = HERE.parents[2]

RTLLM_SRC = WORKSPACE / "dac2026_c1/data/RTLLM"
VE_SRC = WORKSPACE / "case_in_VE_RM/VerilogEval"
TEMPO_SRC = VERSION2 / "tempo26/vortex"
DAG_CASES = VERSION2 / "DAG/experiment/llm_generation_errors/cases"

OUT = HERE
SEED = 20261115

FIXED_VE = [
    "Prob057", "Prob070", "Prob082", "Prob086", "Prob089", "Prob091",
    "Prob093", "Prob099", "Prob101", "Prob102", "Prob112", "Prob116",
    "Prob125", "Prob133", "Prob135", "Prob137", "Prob139", "Prob141",
    "Prob146", "Prob149", "Prob151", "Prob153", "Prob155",
]


def sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def rel(path: Path) -> str:
    return str(path.relative_to(WORKSPACE))


def write_json(path: Path, payload: object) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                    encoding="utf-8")


def copy_case(out_root: Path, dataset: str, short: str, *, spec: Path,
              reference: Path, testbench: Path, top_module: str,
              case_id: str, extra: dict | None = None,
              interface: Path | None = None) -> dict:
    target = out_root / "cases" / short
    target.mkdir(parents=True, exist_ok=False)
    shutil.copyfile(spec, target / "prompt.md")
    ref_name = "reference" + reference.suffix
    shutil.copyfile(reference, target / ref_name)
    tb_name = "testbench" + testbench.suffix
    shutil.copyfile(testbench, target / tb_name)
    extra_files = {}
    if interface is not None:
        shutil.copyfile(interface, target / "interface.txt")
        extra_files["interface"] = "interface.txt"
    meta = {
        "case_id": case_id,
        "dataset": dataset,
        "short": short,
        "top_module": top_module,
        "source": {
            "spec": rel(spec),
            "reference": rel(reference),
            "testbench": rel(testbench),
        },
        "sha256": {
            "spec": sha256(spec),
            "reference": sha256(reference),
            "testbench": sha256(testbench),
        },
        **(extra or {}),
    }
    write_json(target / "meta.json", meta)
    row = {
        "case_id": case_id,
        "dataset": dataset,
        "short": short,
        "top_module": top_module,
        "prompt": f"cases/{short}/prompt.md",
        "reference": f"cases/{short}/{ref_name}",
        "testbench": f"cases/{short}/{tb_name}",
        "meta": f"cases/{short}/meta.json",
    }
    row.update(extra_files)
    return row


def rtllm_cases() -> list[dict]:
    rows = []
    for spec in sorted(RTLLM_SRC.rglob("design_description.txt")):
        folder = spec.parent
        refs = sorted(folder.glob("verified_*.v"))
        tbs = sorted(folder.glob("testbench.v"))
        if len(refs) != 1 or len(tbs) != 1:
            raise RuntimeError(f"RTLLM case incomplete: {folder}")
        text = spec.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"Module name:\s*\n?\s*([A-Za-z_]\w*)", text)
        top = m.group(1) if m else None
        if not top:
            m = re.search(r"^\s*([A-Za-z_]\w*)\s+\w+\s*\(", tbs[0].read_text(
                encoding="utf-8", errors="replace"), re.M)
            top = m.group(1) if m else None
        if not top:
            raise RuntimeError(f"RTLLM top module not found: {folder}")
        rows.append(dict(
            short=folder.name,
            case_id=str(folder.relative_to(RTLLM_SRC)),
            spec=spec, reference=refs[0], testbench=tbs[0], top=top,
        ))
    return rows


def verilogeval_cases() -> tuple[list[dict], list[str]]:
    dirs = {d.name: d for d in VE_SRC.iterdir()
            if d.is_dir() and re.fullmatch(r"Prob\d{3}", d.name)}
    all_names = sorted(dirs)
    missing = [n for n in FIXED_VE if n not in dirs]
    if missing:
        raise RuntimeError(f"fixed VerilogEval cases missing: {missing}")
    remaining = [n for n in all_names if n not in set(FIXED_VE)]
    rng = random.Random(SEED)
    sampled = sorted(rng.sample(remaining, 46), key=lambda s: int(s[4:]))
    rows = []
    for name in FIXED_VE + sampled:
        folder = dirs[name]
        prompts = sorted(folder.glob("*prompt.txt"))
        refs = sorted(folder.glob("*_ref.sv"))
        tbs = sorted(folder.glob("testbench.sv"))
        ifcs = sorted(folder.glob("*_ifc.txt"))
        if len(prompts) != 1 or len(refs) != 1 or len(tbs) != 1:
            raise RuntimeError(f"VerilogEval case incomplete: {folder}")
        text = prompts[0].read_text(encoding="utf-8", errors="replace")
        mods = re.findall(r"^\s*module\s+([A-Za-z_]\w*)", text, re.M)
        top = mods[-1] if mods else "TopModule"
        rows.append(dict(
            short=name, case_id=name, spec=prompts[0], reference=refs[0],
            testbench=tbs[0], top=top,
            interface=ifcs[0] if ifcs else None,
            selected_by="fixed" if name in set(FIXED_VE) else "random46",
        ))
    return rows, sampled


def tempo_cases() -> list[dict]:
    rows = []
    for folder in sorted(d for d in TEMPO_SRC.iterdir() if d.is_dir()):
        spec = folder / "design_description.txt"
        refs = sorted(p for p in folder.glob("*.sv") if p.name != "testbench.sv")
        tbs = sorted(folder.glob("testbench.sv"))
        if not spec.exists() or len(refs) != 1 or len(tbs) != 1:
            raise RuntimeError(f"Tempo case incomplete: {folder}")
        text = spec.read_text(encoding="utf-8", errors="replace")
        m = re.search(r"Module name:\s*\n?\s*([A-Za-z_]\w*)", text)
        top = m.group(1) if m else None
        if not top:
            raise RuntimeError(f"Tempo top module not found: {folder}")
        rows.append(dict(
            short=folder.name, case_id=f"Tempo26/{folder.name}",
            spec=spec, reference=refs[0], testbench=tbs[0], top=top,
        ))
    return rows


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--out", default=str(OUT))
    args = parser.parse_args()
    out = Path(args.out)
    targets = [out / "group1/rtllm", out / "group1/verilogeval",
               out / "group2/tempo"]
    for target in targets:
        if (target / "cases").exists() and any((target / "cases").iterdir()):
            raise SystemExit(f"refusing to overwrite non-empty: {target}")

    all_manifest = {
        "schema_version": 1,
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "source_roots": {
            "rtllm": rel(RTLLM_SRC),
            "verilogeval": rel(VE_SRC),
            "tempo": rel(TEMPO_SRC),
        },
        "selection": {
            "rtllm": "all 48",
            "verilogeval": {
                "fixed": 23,
                "random": 46,
                "random_seed": SEED,
                "fixed_cases": FIXED_VE,
            },
            "tempo": "all 10",
        },
        "datasets": {},
    }

    # RTLLM
    rtllm_root = out / "group1/rtllm"
    rows = []
    for c in rtllm_cases():
        rows.append(copy_case(
            rtllm_root, "RTLLM", c["short"], spec=c["spec"],
            reference=c["reference"], testbench=c["testbench"],
            top_module=c["top"], case_id=c["case_id"]))
    rtllm_manifest = dict(schema_version=1, dataset="RTLLM",
                          generated_at=all_manifest["generated_at"],
                          counts={"cases": len(rows)}, cases=rows)
    write_json(rtllm_root / "manifest.json", rtllm_manifest)
    all_manifest["datasets"]["rtllm"] = {"count": len(rows),
                                         "root": rel(rtllm_root)}

    # VerilogEval
    ve_root = out / "group1/verilogeval"
    ve_rows, sampled = verilogeval_cases()
    rows = []
    for c in ve_rows:
        rows.append(copy_case(
            ve_root, "VerilogEval", c["short"], spec=c["spec"],
            reference=c["reference"], testbench=c["testbench"],
            top_module=c["top"], case_id=c["case_id"],
            interface=c["interface"], extra={"selected_by": c["selected_by"]}))
    ve_manifest = dict(
        schema_version=1, dataset="VerilogEval",
        generated_at=all_manifest["generated_at"],
        selection={"fixed": FIXED_VE, "random_seed": SEED,
                   "random_46": sampled},
        counts={"cases": len(rows)}, cases=rows)
    write_json(ve_root / "manifest.json", ve_manifest)
    all_manifest["datasets"]["verilogeval"] = {
        "count": len(rows), "root": rel(ve_root), "random_46": sampled}

    # Tempo
    tempo_root = out / "group2/tempo"
    rows = []
    for c in tempo_cases():
        rows.append(copy_case(
            tempo_root, "Tempo26", c["short"], spec=c["spec"],
            reference=c["reference"], testbench=c["testbench"],
            top_module=c["top"], case_id=c["case_id"]))
    tempo_manifest = dict(schema_version=1, dataset="Tempo26",
                          generated_at=all_manifest["generated_at"],
                          counts={"cases": len(rows)}, cases=rows)
    write_json(tempo_root / "manifest.json", tempo_manifest)
    all_manifest["datasets"]["tempo"] = {"count": len(rows),
                                         "root": rel(tempo_root)}

    write_json(out / "manifest.json", all_manifest)
    print(json.dumps({k: v["count"] for k, v in
                      all_manifest["datasets"].items()}, ensure_ascii=False))


if __name__ == "__main__":
    main()