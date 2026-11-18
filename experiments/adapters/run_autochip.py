#!/usr/bin/env python3
"""AutoChip adapter for the spec-to-RTL comparison experiment.

Runs the upstream AutoChip flow (``generate_verilog.py``: generate -> compile
with iverilog -> simulate -> feed errors back) on a case from
``experiments/runner_cases`` and scores the final RTL with
``common.evaluate_rtl`` (Verilator).

Usage
-----
    python run_autochip.py --case RTLLM/accu --model deepseek --out <dir> [--iterations 3]

Notes
-----
* The cloned AutoChip repo is never modified.  Every run copies the upstream
  scripts into ``<out>/autochip/scripts`` and patches *that copy* only.  The
  three shims are:

  1. optional-SDK imports (``anthropic`` / ``tiktoken`` / ``torch`` /
     ``transformers`` / ``google.generativeai`` / ``mistralai``) are guarded so
     a stock ``openai`` install is enough for the ChatGPT code path;
  2. AutoChip's ranking hardcodes ``iverilog -s tb`` while the runner_cases
     testbenches use their own top-module names (RTLLM) -> the top module is
     detected from the testbench;
  3. AutoChip requires the VerilogEval ``Mismatches: N in M samples`` summary
     line; RTLLM testbenches only print pass/fail text -> a marker based
     fallback keeps the repair loop working.

  In addition the adapter builds a self-contained testbench
  (``tb + reference`` when the testbench instantiates ``RefModule``), matching
  the shape of the testbenches shipped in ``AutoChip/verilogeval_prompts_tbs``.

* ``OPENAI_API_KEY`` / ``OPENAI_BASE_URL`` come from ``common.model_config``;
  proxy variables are stripped for the child process because this host exports
  ``all_proxy=socks5://...`` and a ``no_proxy`` value that httpx rejects
  (``Invalid port: ':1]'``).

* iverilog/vvp come from ``/home/2531930/nas/2531930-2/eda_tools/conda/bin``,
  which is prepended to ``PATH`` for the child process.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))
import common  # noqa: E402  (path bootstrap above)

AUTOCHIP_REPO = common.WORKSPACE / "version2/baselines/spec2rtl/AutoChip"
AUTOCHIP_SCRIPTS = AUTOCHIP_REPO / "autochip_scripts"
VENV_PYTHON = common.WORKSPACE / "dac2026_c1/.venv_c1/bin/python"

PROXY_VARS = (
    "http_proxy", "https_proxy", "all_proxy",
    "HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
    "no_proxy", "NO_PROXY",
)

SHIMS: list[str] = []


# --------------------------------------------------------------------------- #
# environment helpers
# --------------------------------------------------------------------------- #
def find_python(explicit: str | None) -> str:
    """Return a python interpreter that can import openai."""
    candidates = []
    if explicit:
        candidates.append(explicit)
    if os.environ.get("AUTOCHIP_PYTHON"):
        candidates.append(os.environ["AUTOCHIP_PYTHON"])
    candidates += [sys.executable, str(VENV_PYTHON), "python3"]
    seen: set[str] = set()
    for cand in candidates:
        if not cand or cand in seen:
            continue
        seen.add(cand)
        if os.sep not in cand:
            cand = shutil.which(cand) or ""
        if not cand or not Path(cand).exists():
            continue
        try:
            probe = subprocess.run([cand, "-c", "import openai"],
                                   stdout=subprocess.DEVNULL,
                                   stderr=subprocess.PIPE, text=True, timeout=120)
        except (OSError, subprocess.SubprocessError):
            continue
        if probe.returncode == 0:
            return cand
    raise RuntimeError(
        "no python interpreter with the 'openai' package found; "
        "set AUTOCHIP_PYTHON or use --python")


def child_env(cfg: dict[str, str]) -> dict[str, str]:
    env = os.environ.copy()
    env["PATH"] = common.CONDA_BIN + os.pathsep + env.get("PATH", "")
    for var in PROXY_VARS:
        env.pop(var, None)
    env["OPENAI_API_KEY"] = cfg["key"]
    env["OPENAI_BASE_URL"] = cfg["base"]
    env["PYTHONUNBUFFERED"] = "1"
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    env.pop("OPENAI_ORG_ID", None)
    return env


# --------------------------------------------------------------------------- #
# vendored, shimmed copy of the AutoChip scripts
# --------------------------------------------------------------------------- #
def _replace(text: str, old: str, new: str, label: str) -> str:
    if old not in text:
        raise RuntimeError(
            f"AutoChip shim '{label}' no longer applies: upstream file changed")
    return text.replace(old, new, 1)


_AUTOCHIP_HELPERS = '''


# --------------------------------------------------------------------------- #
# adapter shims (added to the per-run copy only; upstream is untouched)
# --------------------------------------------------------------------------- #
def _tb_top(testbench_path, dut_name="tb"):
    """Return the testbench module that instantiates the DUT."""
    try:
        with open(testbench_path, "r", errors="replace") as handle:
            text = handle.read()
    except OSError:
        return "tb"
    blocks = list(re.finditer(r"(?m)^\\s*module\\s+([A-Za-z_]\\w*)", text))
    for i, match in enumerate(blocks):
        end = blocks[i + 1].start() if i + 1 < len(blocks) else len(text)
        body = text[match.start():end]
        if re.search(r"\\b" + re.escape(dut_name) + r"\\s+[A-Za-z_]\\w*\\s*\\(", body):
            return match.group(1)
    return blocks[0].group(1) if blocks else "tb"


def _marker_rank(sim_out):
    """Pass/fail fallback for testbenches without a mismatch summary line.

    Returns ``(mismatches, samples)``.
    """
    low = sim_out.lower()
    for marker in ("error", "fail", "fatal", "mismatch", "timeout"):
        if marker in low:
            return 1, 1
    return 0, 1
'''


def vendor_autochip(dst_scripts: Path, work: Path) -> None:
    """Copy the upstream scripts and apply the documented local shims."""
    if not AUTOCHIP_SCRIPTS.is_dir():
        raise RuntimeError(f"AutoChip scripts not found: {AUTOCHIP_SCRIPTS}")
    if dst_scripts.exists():
        shutil.rmtree(dst_scripts)
    dst_scripts.mkdir(parents=True)
    for src in sorted(AUTOCHIP_SCRIPTS.glob("*.py")):
        shutil.copy2(src, dst_scripts / src.name)

    # -- 1. optional model-SDK imports -------------------------------------- #
    lm = dst_scripts / "languagemodels.py"
    text = lm.read_text(encoding="utf-8")
    text = _replace(
        text,
        "## ANTHROPIC\nfrom anthropic import Anthropic\n"
        "from anthropic import AsyncAnthropic, HUMAN_PROMPT, AI_PROMPT\n",
        "## ANTHROPIC\n"
        "try:  # adapter shim: optional SDK\n"
        "    from anthropic import Anthropic\n"
        "    from anthropic import AsyncAnthropic, HUMAN_PROMPT, AI_PROMPT\n"
        "except Exception:\n"
        "    Anthropic = AsyncAnthropic = None\n"
        "    HUMAN_PROMPT = AI_PROMPT = \"\"\n",
        "languagemodels-anthropic",
    )
    text = _replace(
        text,
        "## GEMINI\nimport google.generativeai as genai\n",
        "## GEMINI\n"
        "try:  # adapter shim: optional SDK\n"
        "    import google.generativeai as genai\n"
        "except Exception:\n"
        "    genai = None\n",
        "languagemodels-genai",
    )
    text = _replace(
        text,
        "## VERIGEN\nfrom transformers import AutoTokenizer, AutoModelForCausalLM, "
        "BitsAndBytesConfig\nimport torch\n",
        "## VERIGEN\n"
        "try:  # adapter shim: optional SDK\n"
        "    from transformers import AutoTokenizer, AutoModelForCausalLM, "
        "BitsAndBytesConfig\n"
        "    import torch\n"
        "except Exception:\n"
        "    AutoTokenizer = AutoModelForCausalLM = BitsAndBytesConfig = None\n"
        "    torch = None\n",
        "languagemodels-transformers",
    )
    text = _replace(
        text,
        "## MISTRAL\nfrom mistralai import Mistral\n",
        "## MISTRAL\n"
        "try:  # adapter shim: optional SDK\n"
        "    from mistralai import Mistral\n"
        "except Exception:\n"
        "    Mistral = None\n",
        "languagemodels-mistral",
    )
    lm.write_text(text, encoding="utf-8")

    # -- 2. testbench top module (LLMResponse.calculate_rank) --------------- #
    text = lm.read_text(encoding="utf-8")
    text = _replace(
        text,
        '        compiler_cmd = f"iverilog -Wall -Winfloop -Wno-timescale -g2012 '
        '-s tb -o {vvp_file} {filename} {testbench}"',
        '        compiler_cmd = f"iverilog -Wall -Winfloop -Wno-timescale -g2012 '
        '-s {_tb_top(testbench, module)} -o {vvp_file} {filename} {testbench}"',
        "calculate_rank-tb-top",
    )
    # -- 3. mismatch-summary fallback --------------------------------------- #
    text = _replace(
        text,
        '            mismatch_pattern = r"Mismatches: (\\d+) in (\\d+) samples"\n'
        "            match = re.search(mismatch_pattern, sim_out.splitlines()[-1])\n"
        "            #print(f\"Match: {match}\")\n"
        "\n"
        "            if match:\n"
        "                mismatches = int(match.group(1))\n"
        "                samples = int(match.group(2))\n"
        "            else:\n"
        "                raise ValueError(\"Simulation output does not contain final "
        "mismatch information\")\n",
        '            mismatch_pattern = r"Mismatches: (\\d+) in (\\d+) samples"\n'
        "            match = re.search(mismatch_pattern, sim_out)\n"
        "\n"
        "            if match:\n"
        "                mismatches = int(match.group(1))\n"
        "                samples = int(match.group(2))\n"
        "            else:\n"
        "                # adapter shim: non-VerilogEval testbenches print pass/fail\n"
        "                # text instead of a mismatch summary.\n"
        "                mismatches, samples = _marker_rank(sim_out)\n",
        "calculate_rank-mismatch-fallback",
    )
    lm.write_text(text + _AUTOCHIP_HELPERS, encoding="utf-8")

    # -- optional imports in verilog_handling.py ---------------------------- #
    vh = dst_scripts / "verilog_handling.py"
    text = vh.read_text(encoding="utf-8")
    text = _replace(
        text,
        "import tiktoken\nimport anthropic\n",
        "try:  # adapter shim: optional deps, unused by the ChatGPT path\n"
        "    import tiktoken\n"
        "except ImportError:\n"
        "    tiktoken = None\n"
        "try:\n"
        "    import anthropic\n"
        "except ImportError:\n"
        "    anthropic = None\n",
        "verilog_handling-imports",
    )
    vh.write_text(text, encoding="utf-8")

    # AutoChip reads usage.txt from the process cwd.
    shutil.copy2(AUTOCHIP_SCRIPTS / "usage.txt", work / "usage.txt")


# --------------------------------------------------------------------------- #
# testbench / RTL preparation
# --------------------------------------------------------------------------- #
def compose_testbench(case: dict[str, Any], work: Path) -> tuple[Path, bool]:
    """Return a self-contained testbench for AutoChip (reference inlined).

    AutoChip compiles with a single testbench file, whilst the VerilogEval
    testbenches in runner_cases instantiate ``RefModule`` from the separate
    reference file.  Concatenating them reproduces the shape of the testbenches
    bundled with AutoChip (``*_tb.sv`` contains its reference module).
    """
    tb = common.RUNNER / case["testbench"]
    tb_text = tb.read_text(encoding="utf-8", errors="replace")
    inlined = False
    if re.search(r"\bRefModule\b", tb_text):
        ref = (common.RUNNER / case["reference"]).read_text(
            encoding="utf-8", errors="replace")
        if "module RefModule" not in tb_text:
            tb_text = tb_text.rstrip() + "\n\n// ---- reference (inlined by adapter) ----\n" + ref
            inlined = True
    path = work / "tb_autochip.sv"
    path.write_text(tb_text, encoding="utf-8")
    return path, inlined


def split_modules(text: str) -> list[tuple[str, str]]:
    blocks: list[tuple[str, str]] = []
    pattern = re.compile(r"(?ms)^\s*module\s+([A-Za-z_]\w*).*?^\s*endmodule\b")
    for match in pattern.finditer(text):
        blocks.append((match.group(1), match.group(0).strip() + "\n"))
    return blocks


def clean_rtl(text: str, top: str, tb_text: str) -> str:
    """Strip markdown noise, drop testbench modules, rename top module."""
    rtl = common.extract_verilog(text)
    blocks = split_modules(rtl)
    if not blocks:
        return rtl
    first = re.search(r"(?m)^\s*module\s+[A-Za-z_]\w*", rtl)
    preamble = rtl[:first.start()].strip() if first else ""
    tb_modules = {name for name, _ in split_modules(tb_text)}
    kept = [(name, body) for name, body in blocks
            if not (name in tb_modules and name != top)]
    if not kept:
        kept = blocks
    if not any(name == top for name, _ in kept):
        names = [name for name, _ in kept]
        # prefer a module the testbench actually instantiates
        target = next((n for n in names if re.search(
            r"\b" + re.escape(n) + r"\s+[A-Za-z_]\w*\s*\(", tb_text)), None)
        target = target or names[-1]
        new_blocks = []
        renamed = False
        for name, body in kept:
            if name == target and not renamed:
                body = re.sub(r"(?m)^(\s*)module(\s+)" + re.escape(name) + r"\b",
                              r"\1module\2" + top, body, count=1)
                renamed = True
                name = top
            new_blocks.append((name, body))
        kept = new_blocks
    body = "\n".join(b for _, b in kept).strip() + "\n"
    return (preamble + "\n" + body) if preamble else body


def best_candidate(outdir: Path, top: str, logfile: Path) -> tuple[Path | None, dict[str, Any]]:
    """Pick the candidate AutoChip itself ranked best (fallback: latest)."""
    info: dict[str, Any] = {}
    best_iter = best_resp = None
    if logfile.is_file():
        log = logfile.read_text(encoding="utf-8", errors="replace")
        match = re.search(
            r"Best ranked response at iteration (\d+) with response number (\d+)", log)
        if match:
            best_iter, best_resp = int(match.group(1)), int(match.group(2))
        rank = re.search(r"Rank of best response: ([-+0-9.eE]+)", log)
        if rank:
            info["best_rank"] = rank.group(1)
    if best_iter is not None:
        cand = outdir / f"iter{best_iter}/response{best_resp}" / f"{top}.sv"
        if cand.is_file() and cand.stat().st_size > 0:
            info.update(iteration=best_iter, response=best_resp)
            return cand, info
    candidates = sorted(outdir.glob("iter*/response*/*.sv"),
                        key=lambda p: (int(re.search(r"iter(\d+)", str(p)).group(1)),
                                       int(re.search(r"response(\d+)", str(p)).group(1))))
    for cand in reversed(candidates):
        if cand.stat().st_size > 0:
            info.update(iteration=int(re.search(r"iter(\d+)", str(cand)).group(1)),
                        response=int(re.search(r"response(\d+)", str(cand)).group(1)))
            return cand, info
    # last resort: pull verilog out of the per-response conversation logs
    for cand in reversed(sorted(outdir.glob("iter*/response*/log.txt"))):
        text = common.extract_verilog(
            cand.read_text(encoding="utf-8", errors="replace"))
        if text:
            fallback = cand.parent / f"{top}.sv"
            fallback.write_text(text, encoding="utf-8")
            info.update(iteration=int(re.search(r"iter(\d+)", str(cand)).group(1)),
                        response=int(re.search(r"response(\d+)", str(cand)).group(1)),
                        recovered_from="response log")
            return fallback, info
    return None, info


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def run(args: argparse.Namespace) -> dict[str, Any]:
    started = time.time()
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    work = out_dir / "autochip"
    work.mkdir(exist_ok=True)
    ac_out = work / "out"
    if ac_out.exists():
        shutil.rmtree(ac_out)
    ac_out.mkdir()

    case = common.case_info(args.case)
    cfg = common.model_config(args.model)
    python = find_python(args.python)

    prompt = (common.RUNNER / case["prompt"]).resolve()
    tb_path, inlined = compose_testbench(case, work)
    tb_text = tb_path.read_text(encoding="utf-8", errors="replace")

    vendor_autochip(work / "scripts", work)
    SHIMS.extend(["optional-sdk-imports", "tb-top-detection", "mismatch-summary-fallback"])

    config = {
        "general": {
            "prompt": str(prompt),
            "name": case["top_module"],
            "testbench": str(tb_path),
            "model_family": "ChatGPT",
            "model_id": cfg["model"],
            "num_candidates": args.num_candidates,
            "iterations": args.iterations,
            "outdir": "out",
            "log": "log.txt",
            "mixed-models": False,
        },
        "mixed-models": {},
    }
    (work / "config.json").write_text(
        json.dumps(config, indent=2) + "\n", encoding="utf-8")

    cmd = [
        python, "scripts/generate_verilog.py",
        "-p", str(prompt),
        "-t", str(tb_path),
        "-n", case["top_module"],
        "-f", "ChatGPT",
        "-m", cfg["model"],
        "-i", str(args.iterations),
        "-o", "out",
        "-l", "log.txt",
    ]
    env = child_env(cfg)
    run_log = out_dir / "autochip_run.log"
    timed_out = False
    with run_log.open("w", encoding="utf-8") as handle:
        handle.write("$ " + " ".join(cmd) + f"\n# cwd: {work}\n\n")
        handle.flush()
        try:
            proc = subprocess.run(cmd, cwd=str(work), env=env, text=True,
                                  stdout=handle, stderr=subprocess.STDOUT,
                                  timeout=args.timeout)
            returncode = proc.returncode
        except subprocess.TimeoutExpired:
            timed_out = True
            returncode = -9

    error: str | None = None
    if timed_out:
        error = f"autochip timed out after {args.timeout}s"
    elif returncode != 0:
        tail = run_log.read_text(encoding="utf-8", errors="replace")[-1500:]
        error = f"autochip exited with {returncode}: {tail.strip()[-1200:]}"

    rtl_path = out_dir / "rtl" / f"{case['top_module']}.sv"
    rtl_path.parent.mkdir(parents=True, exist_ok=True)
    candidate, info = best_candidate(ac_out, case["top_module"], ac_out / "log.txt")
    eval_result: dict[str, Any] | None = None
    status = "error"
    if candidate is not None and not timed_out:
        raw = candidate.read_text(encoding="utf-8", errors="replace")
        cleaned = clean_rtl(raw, case["top_module"], tb_text)
        if not cleaned.strip():
            error = error or "AutoChip produced no Verilog text"
        else:
            rtl_path.write_text(cleaned, encoding="utf-8")
            eval_result = common.evaluate_rtl(rtl_path, case, out_dir / "eval")
            status = "passed" if eval_result.get("status") == "passed" else "failed"
    elif not error:
        error = "AutoChip produced no candidate Verilog"

    elapsed = round(time.time() - started, 2)
    result = {
        "system": "autochip",
        "case": case["case_id"],
        "model": args.model,
        "status": status,
        "rtl_path": str(rtl_path) if status != "error" and rtl_path.is_file() else None,
        "eval": eval_result,
        "iterations": args.iterations,
        "elapsed_s": elapsed,
        "error": error,
        "command": " ".join(cmd),
        "autochip": {
            "python": python,
            "model_id": cfg["model"],
            "workdir": str(work),
            "outdir": str(ac_out),
            "log": str(ac_out / "log.txt"),
            "candidate": str(candidate) if candidate else None,
            "reference_inlined": inlined,
            "shims": SHIMS,
            "returncode": returncode,
            **info,
        },
    }
    common.write_result(out_dir / "result.json", result)
    return result


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Run AutoChip on one runner case and evaluate the result.")
    parser.add_argument("--case", required=True,
                        help="case_id or short name, e.g. RTLLM/accu")
    parser.add_argument("--model", required=True, choices=sorted(common.MODELS))
    parser.add_argument("--out", required=True, help="per-case output directory")
    parser.add_argument("--iterations", type=int, default=3,
                        help="AutoChip repair iterations (default 3)")
    parser.add_argument("--num-candidates", type=int, default=1,
                        help="AutoChip candidates per iteration (default 1)")
    parser.add_argument("--timeout", type=int, default=3600,
                        help="wall-clock limit for AutoChip (default 3600s)")
    parser.add_argument("--python", default=None,
                        help="python interpreter with openai (else auto-detected)")
    args = parser.parse_args(argv)

    started = time.time()
    try:
        result = run(args)
    except Exception as exc:  # keep result.json even on adapter failures
        out_dir = Path(args.out).expanduser().resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        payload = {
            "system": "autochip",
            "case": args.case,
            "model": args.model,
            "status": "error",
            "rtl_path": None,
            "eval": None,
            "iterations": args.iterations,
            "elapsed_s": round(time.time() - started, 2),
            "error": f"{type(exc).__name__}: {exc}",
        }
        try:
            case = common.case_info(args.case)
            payload["case"] = case["case_id"]
        except Exception:
            pass
        common.write_result(out_dir / "result.json", payload)
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return 1

    print(json.dumps({k: v for k, v in result.items() if k != "eval"},
                     ensure_ascii=False, indent=2))
    print(f"[autochip] {result['status']} in {result['elapsed_s']}s -> "
          f"{result['rtl_path']}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())