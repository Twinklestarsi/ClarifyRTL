#!/usr/bin/env python3
"""Re-verify every historical candidate RTL under the current checker.

Candidates that failed an older, weaker check (e.g. JasperGold rejecting a
don't-care design) are re-evaluated here.  A passing candidate is recorded in a
fresh run directory, so the normal metrics pick it up without rewriting history.
"""
import json, shutil, sys, time
from pathlib import Path
HERE = Path(__file__).resolve(); ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(HERE.parents[3]))
from blocks.b1_scaffold import get_case, load_config, sanitize_network_env, patch_library_clients
from blocks.b7_phase_c.equivalence import check_equivalence, formal_summary

sanitize_network_env(); patch_library_clients()
cfg = load_config()
names = sys.argv[1:]
cases = [get_case(cfg["run"]["case_root"], n) for n in names]
root = Path(cfg["run"]["output_root"])
for case in cases:
    seen, found = set(), None
    candidates = []
    for run in sorted(root.glob(f"{case.key}/2*")):
        for rtl in sorted(run.glob("rtl/*.v")):
            digest = rtl.read_bytes()
            if digest in seen:
                continue
            seen.add(digest)
            candidates.append(rtl)
    print(f"{case.key}: {len(candidates)} candidates", flush=True)
    for rtl in candidates:
        verdict = check_equivalence(case, rtl, rtl.parents[1] / "salvage_check", cfg["formal"])
        if verdict.get("status") == "equivalent":
            found = (rtl, verdict)
            break
    if not found:
        print(f"  -> none equivalent", flush=True)
        continue
    rtl, verdict = found
    stamp = time.strftime("%Y%m%dT%H%M%S") + "-salvage"
    out = root / case.key / stamp
    (out / "rtl").mkdir(parents=True, exist_ok=True)
    shutil.copyfile(rtl, out / "rtl" / "generated_rtl.v")
    (out / "result.json").write_text(json.dumps({
        "case": case.key, "arm": "A4_salvage", "status": "equivalent",
        "formal": formal_summary(verdict), "salvaged_from": str(rtl),
        "duration_s": 0, "directory": str(out)}, ensure_ascii=False, indent=2) + "\n")
    print(f"  -> salvaged: {rtl} ({verdict.get('method')})", flush=True)
