"""Probe: spec -> IR -> checker1/2 -> RTL for one dataset case."""
import sys, time
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from blocks.b1_scaffold import get_case, load_config, patch_library_clients, RunContext
from blocks.b1_scaffold.env import load_dotenv

config = load_config()
patch_library_clients()
env = load_dotenv()
case = get_case(config["run"]["case_root"], sys.argv[1])
ctx = RunContext.start(output_root=config["run"]["output_root"], case_key=case.key, config=config)
print("run dir:", ctx.directory)

from IRgeneration.ir_generate.gen_petrinet_ir import generate_ir
from IRgeneration.checker1 import check_ir as c1
from IRgeneration.checker2 import check_ir as c2
from Compiler.rtl_compiler import compile_ir

with ctx.stage("generate_ir"):
    t = time.time()
    ir_path, meta = generate_ir(case.prompt, ctx.path("phase_a/ir_init.yaml"),
        api_key=env["OPENAI_API_KEY"], base_url=env["OPENAI_BASE_URL"],
        model=env["OPENAI_MODEL"], temperature=0.2, max_tokens=None,  # was 32000
        repair=True, allow_invalid=True)
    print("generated in %.1fs" % (time.time() - t), "usage:", (meta.get("usage") or {}).get("total_tokens"))

with ctx.stage("checker1"):
    print("checker1", c1(ir_path, ctx.path("phase_a/checker1_report.json"))["status"])
with ctx.stage("checker2"):
    print("checker2", c2(ir_path, ctx.path("phase_a/checker2_report.json"))["status"])
with ctx.stage("compile"):
    try:
        compile_ir(ir_path, ctx.path("rtl/generated_rtl.v"), ctx.path("rtl/compiler_report.json"))
        print("compile OK:", len(ctx.path("rtl/generated_rtl.v").read_text().splitlines()), "lines")
    except Exception as exc:
        print("compile FAIL:", type(exc).__name__, str(exc)[:400])
