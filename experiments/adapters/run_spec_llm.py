#!/usr/bin/env python3
"""Direct LLM-from-spec baseline (spec-LLM-RTL).

One-shot: the model sees only the visible spec and is asked for synthesizable
Verilog.  No reference RTL, no testbench feedback, no repair loop.  The
generated RTL is evaluated with the same Verilator testbench as every other
system, so the number is directly comparable.
"""
from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import common  # noqa: E402

SYSTEM = "spec_llm"

INSTRUCTION = """You are a professional Verilog designer.
Write a complete, synthesizable Verilog module that implements the specification below.
Return ONLY the Verilog code, enclosed in one ```verilog code block. Do not explain.
The top module name must be exactly `{top}`.

Specification:
{spec}
"""


def run(args) -> dict:
    common.load_env()
    case = common.case_info(args.case)
    top = case["top_module"]
    out = Path(args.out).resolve()
    out.mkdir(parents=True, exist_ok=True)
    spec_path = common.RUNNER / case["prompt"]
    spec = spec_path.read_text(encoding="utf-8", errors="replace")
    prompt = INSTRUCTION.format(top=top, spec=spec)
    (out / "prompt.txt").write_text(prompt, encoding="utf-8")
    started = time.time()
    payload = {
        "system": SYSTEM, "case": case["case_id"], "model": args.model,
        "status": "error", "rtl_path": None, "eval": None,
        "llm_usage": None, "elapsed_s": None, "error": None,
        "dataset": case["dataset"], "top_module": top,
        "prompt_file": str(out / "prompt.txt"),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    try:
        text, usage = common.call_llm(
            [{"role": "user", "content": prompt}], args.model,
            temperature=args.temperature, max_tokens=args.max_tokens)
        (out / "raw_response.txt").write_text(text, encoding="utf-8")
        payload["llm_usage"] = usage
        rtl = common.extract_verilog(text)
        if not rtl.strip():
            payload["status"] = "error"
            payload["error"] = "empty generation"
            return payload
        rtl_path = out / f"{top}.sv"
        rtl_path.write_text(rtl, encoding="utf-8")
        payload["rtl_path"] = str(rtl_path)
        ev = common.evaluate_rtl(rtl_path, case, out)
        payload["eval"] = ev
        payload["status"] = "passed" if ev.get("status") == "passed" else (
            "compile_failed" if ev.get("status") == "compile_failed" else "failed")
    except Exception as exc:  # noqa: BLE001
        payload["status"] = "error"
        payload["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        payload["elapsed_s"] = round(time.time() - started, 2)
        payload["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    return payload


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--case", required=True)
    ap.add_argument("--model", required=True)
    ap.add_argument("--out", required=True)
    ap.add_argument("--temperature", type=float, default=0.0)
    ap.add_argument("--max-tokens", type=int, default=8192)
    args = ap.parse_args()
    payload = run(args)
    common.write_result(Path(args.out) / "result.json", payload)
    print(json.dumps({k: payload[k] for k in
                      ("system", "case", "model", "status", "elapsed_s")}))


if __name__ == "__main__":
    main()
