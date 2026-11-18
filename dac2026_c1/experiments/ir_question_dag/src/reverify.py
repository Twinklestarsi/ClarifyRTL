#!/usr/bin/env python3
"""Re-run equivalence on existing RTL with the current miter semantics (no LLM)."""
import json, sys
from pathlib import Path
HERE = Path(__file__).resolve(); ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(HERE.parents[3]))
from blocks.b1_scaffold import get_case, load_config, sanitize_network_env, patch_library_clients
from blocks.b7_phase_c.equivalence import check_equivalence, formal_summary

sanitize_network_env(); patch_library_clients()
cfg = load_config()
names = [n for n in sys.argv[1:]] or None
from blocks.b1_scaffold import load_cases
cases = load_cases(cfg["run"]["case_root"], names=names) if names else load_cases(cfg["run"]["case_root"])
rows = []
for case in cases:
    runs = sorted(Path(cfg["run"]["output_root"]).glob(f"{case.key}/2*/rtl/generated_rtl.v"))
    if not runs:
        print(f"{case.key}: no rtl"); continue
    rtl = runs[-1]
    out = rtl.parents[1] / "formal_reverify"
    verdict = check_equivalence(case, rtl, out, cfg["formal"])
    rows.append({"case": case.key, "status": verdict.get("status"),
                 "dontcare": verdict.get("dontcare_miter"), "rtl": str(rtl)})
    print(f"{case.key:34s} {verdict.get('status'):16s} dontcare={verdict.get('dontcare_miter')}", flush=True)
out = Path(cfg["run"]["output_root"]) / "_batch" / "reverify.json"
out.write_text(json.dumps(rows, ensure_ascii=False, indent=2))
ok = sum(1 for r in rows if r["status"] == "equivalent")
print(f"{ok}/{len(rows)} equivalent -> {out}")
