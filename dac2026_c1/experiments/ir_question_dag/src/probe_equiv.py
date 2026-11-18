"""Probe: miter + remote JasperGold equivalence for one run directory."""
import sys, json, types, hashlib
from pathlib import Path
ROOT = Path(__file__).resolve()
sys.path.insert(0, str(ROOT.parents[1])); sys.path.insert(0, str(ROOT.parents[3]))
sys.path.insert(0, "/nas2nfs/homes/2531930/ONE-TURN/case_in_VE_RM/script")
from blocks.b1_scaffold import get_case, load_config
import run_rtllm_jaspergold as shared

config = load_config()
case = get_case(config["run"]["case_root"], sys.argv[1])
run_dir = Path(sys.argv[2]) if len(sys.argv) > 2 else None
candidate = (run_dir / "rtl/generated_rtl.v").read_text()
formal = run_dir / "formal"
args = types.SimpleNamespace(ssh_host="172.31.17.1", ssh_user="2531930", ssh_port=22,
    ssh_key=None, remote_root="/tmp/ir_question_dag", remote_setup="/tools/Cadence/cadence.env",
    jg_bin="/tools/Cadence/JASPERGOLD23/bin/jaspergold", jg_timeout=600, transfer_timeout=120)

pair = dict(case_id=case.case_id, dataset=case.dataset, prompt_path=str(case.prompt),
    rtl_path=str(case.reference), prompt_sha256=shared.digest(case.prompt_text().encode()),
    rtl_sha256=shared.digest(case.reference.read_bytes()),
    prompt_text=case.prompt_text(), rtl_text=case.reference_text())
info = shared.prepare_verification(pair, candidate, {}, formal)
print("prepare:", info["status"], "golden=", info.get("golden_top"), "cand=", info.get("candidate_top"),
      "plan=", info.get("formal_plan", {}).get("status"), info.get("reason", ""))
if info["status"] != "ready":
    sys.exit(0)
remote_dir = args.remote_root + "/" + shared.digest((case.key + candidate).encode())[:16]
shared.preflight(args, args.remote_root)
result = shared.run_formal(args, formal, remote_dir, info)
print(json.dumps(result, ensure_ascii=False, indent=2)[:1200])
