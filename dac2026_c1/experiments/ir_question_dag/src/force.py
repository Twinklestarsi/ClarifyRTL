#!/usr/bin/env python3
"""Extra rewrite attempts for the residual cases, with mismatch feedback."""
import sys, json, time
from pathlib import Path
HERE = Path(__file__).resolve(); ROOT = HERE.parents[1]
sys.path.insert(0, str(ROOT)); sys.path.insert(0, str(HERE.parents[3]))
from blocks.b1_scaffold import get_case, load_config, sanitize_network_env, patch_library_clients
from blocks.b1_scaffold.llm import LLM
from blocks.b2_site_model.patches import load_ir
from blocks.b3_phase_a.checks import check_ir
from blocks.b6_dialogue.rewrite import rewrite_ir
from blocks.b7_phase_c.equivalence import check_equivalence
from blocks.b7_phase_c.exhaustive import exhaustive_equivalence, is_combinational
from blocks.b7_phase_c.simdiff import simulate_diff

sanitize_network_env(); patch_library_clients(); cfg = load_config()
llm = LLM(timeout=300)
for name in sys.argv[1:]:
    case = get_case(cfg["run"]["case_root"], name)
    runs = sorted(Path(cfg["run"]["output_root"]).glob(f"{case.key}/2*"))
    d = runs[-1]
    ir_path = d / "derived/ir_final.yaml"
    if not ir_path.exists(): ir_path = d / "phase_a/ir_exp.yaml"
    best = None
    for attempt in range(5):
        rtl = d / f"rtl/force_{attempt}.v"
        # compile current IR first
        from blocks.b7_phase_c.equivalence import compile_ir
        try:
            compile_ir(ir_path, rtl)
        except Exception as exc:
            print(name, "compile failed", exc); break
        verdict = check_equivalence(case, rtl, d / f"formal/force_{attempt}", cfg["formal"])
        print(f"{name} attempt {attempt}: {verdict.get('status')}", flush=True)
        if verdict.get("status") == "equivalent":
            best = (ir_path, verdict); break
        trace = None
        sim = simulate_diff(case, rtl, d / f"sim/force_{attempt}", cfg["formal"])
        if sim.get("status") == "mismatch": trace = sim.get("first")
        text = rewrite_ir(case, llm, ir_path.read_text(encoding="utf-8"),
                          rtl.read_text(encoding="utf-8"), verdict,
                          label=f"force:{attempt}", trace=trace)
        if not text: break
        cand = d / f"derived/force_{attempt}.yaml"
        cand.write_text(text, encoding="utf-8")
        checks = check_ir(cand, d / f"derived/force_{attempt}_checks", compile_smoke=False)
        if checks.get("status") != "passed": continue
        ir_path = cand
    if best:
        import shutil
        stamp = time.strftime("%Y%m%dT%H%M%S") + "-force"
        out = Path(cfg["run"]["output_root"]) / case.key / stamp
        (out / "rtl").mkdir(parents=True, exist_ok=True)
        shutil.copyfile(best[0], out / "derived_ir_final.yaml")
        shutil.copyfile(d / f"rtl/force_{attempt}.v", out / "rtl/generated_rtl.v")
        (out / "result.json").write_text(json.dumps({"case": case.key, "arm": "A5_force",
            "status": "equivalent", "formal": best[1], "duration_s": 0,
            "directory": str(out)}, ensure_ascii=False, indent=2) + "\n")
        print(name, "SOLVED")
