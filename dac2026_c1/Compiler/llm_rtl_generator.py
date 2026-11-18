"""LLM-based HW-IR to RTL generation used by ``main.py --no-compiler``."""

import json
import os
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from IRgeneration.ir_generate.gen_petrinet_ir import OpenAI
from IRgeneration import llm_stream


SYSTEM_PROMPT = """You are an RTL code generator.

Translate the supplied, already validated Hardware IR into complete,
self-contained, synthesizable SystemVerilog RTL. The Hardware IR is the only
behavioral source of truth. Do not reinterpret, repair, simplify, or add
behavior that is absent from the IR.

Requirements:
- Preserve the module name, parameters, port names, directions, widths, and
  signedness exactly.
- Implement every state, derived signal, transition, sequential process,
  priority relation, memory operation, reset semantic, clock domain, CDC
  crossing, and resource represented by the IR.
- Sequential state updates must use non-blocking assignments. Combinational
  logic must be complete and must not infer unintended latches.
- Respect transition guards, else actions, process order, explicit priority,
  reset type/polarity, and memory indexing.
- Do not emit a testbench, SVA, assertions, prose, placeholders, TODOs, or
  markdown fences.
- Output only the complete RTL source, beginning with a module declaration and
  ending with endmodule.
"""


def build_messages(ir_text):
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (
            "Generate RTL from this validated Hardware IR:\n\n" + ir_text)},
    ]


def _response_meta(response):
    choices = getattr(response, "choices", []) or []
    choice = choices[0] if choices else None
    usage = getattr(response, "usage", None)
    return {
        "id": getattr(response, "id", None),
        "model": getattr(response, "model", None),
        "finish_reason": getattr(choice, "finish_reason", None),
        "usage": usage.model_dump() if usage is not None else None,
    }


def extract_rtl(content):
    """Extract one complete RTL source from a plain or fenced response."""
    text = str(content or "").strip()
    fenced = re.findall(
        r"```(?:systemverilog|verilog|sv|v)?\s*(.*?)```", text,
        flags=re.IGNORECASE | re.DOTALL)
    candidates = fenced or [text]
    candidate = next(
        (item for item in candidates if re.search(r"(?m)^\s*module\b", item)),
        candidates[0] if candidates else "")
    module_start = re.search(r"(?m)^\s*module\b", candidate)
    end_matches = list(re.finditer(
        r"\bendmodule\b(?:\s*:\s*[A-Za-z_][A-Za-z0-9_$]*)?", candidate))
    if not module_start or not end_matches:
        raise ValueError(
            "LLM RTL response must contain a complete module ... endmodule")
    return candidate[module_start.start():end_matches[-1].end()].strip() + "\n"


def _module_name(ir_text):
    try:
        document = yaml.safe_load(ir_text)
    except yaml.YAMLError as exc:
        raise ValueError(f"cannot parse validated HW-IR: {exc}") from exc
    module = document.get("module") if isinstance(document, dict) else None
    name = module.get("name") if isinstance(module, dict) else None
    if not isinstance(name, str) or not name.strip():
        raise ValueError("validated HW-IR has no module.name")
    return name.strip()


def _validate_rtl(rtl, expected_module):
    declaration = re.compile(
        rf"\bmodule\s+(?:automatic\s+)?{re.escape(expected_module)}\b")
    if not declaration.search(rtl):
        raise ValueError(
            f"LLM RTL does not declare expected module {expected_module!r}")
    if re.search(r"\b(?:assert|assume|cover)\s+property\b", rtl):
        raise ValueError("LLM RTL response contains SVA instead of RTL only")


def generate_rtl_from_ir(ir_path, output_path, report_path=None, *,
                         api_key=None, base_url=None, model=None,
                         temperature=0.2, max_tokens=32000):
    """Generate RTL from a checked HW-IR using an OpenAI-compatible model."""
    api_key = api_key or os.environ.get("OPENAI_API_KEY")
    base_url = base_url or os.environ.get("OPENAI_BASE_URL")
    model = model or os.environ.get("OPENAI_MODEL")
    if not api_key:
        raise ValueError("API key is required to generate RTL from IR with an LLM")
    if not base_url:
        raise ValueError("Base URL is required to generate RTL from IR with an LLM")
    if not model:
        raise ValueError("Model name is required to generate RTL from IR with an LLM")
    if OpenAI is None:
        raise RuntimeError("The openai package is required for LLM-based RTL generation")

    source = Path(ir_path).resolve()
    try:
        ir_text = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Failed to read HW-IR: {exc}") from exc
    expected_module = _module_name(ir_text)
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    prompt_path = output.with_name("rtl_llm_prompt.json")
    raw_path = output.with_name("rtl_llm_raw_response.txt")
    response_path = output.with_name("rtl_llm_response.json")
    messages = build_messages(ir_text)
    prompt_path.write_text(
        json.dumps(messages, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")

    client = OpenAI(api_key=api_key, base_url=base_url)
    response = llm_stream.create(
        client, model=model, messages=messages,
        temperature=temperature, max_tokens=max_tokens)
    choices = getattr(response, "choices", []) or []
    content = (getattr(choices[0].message, "content", "")
               if choices else "") or ""
    raw_path.write_text(content, encoding="utf-8")
    meta = _response_meta(response)
    try:
        rtl = extract_rtl(content)
        _validate_rtl(rtl, expected_module)
    except ValueError as exc:
        failed = {
            "status": "failed", "generator": "llm", "module": expected_module,
            "input_ir": str(source), "output_rtl": None,
            "prompt": str(prompt_path), "raw_response": str(raw_path),
            "response": meta, "error": str(exc),
        }
        response_path.write_text(
            json.dumps(failed, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
        if report_path:
            report_output = Path(report_path).resolve()
            report_output.parent.mkdir(parents=True, exist_ok=True)
            report_output.write_text(
                json.dumps(failed, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
        raise

    output.write_text(rtl, encoding="utf-8")
    report = {
        "status": "ok", "generator": "llm",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_ir": str(source), "output_rtl": str(output),
        "module": expected_module, "prompt": str(prompt_path),
        "raw_response": str(raw_path), "response_metadata": str(response_path),
        "llm_response": meta,
    }
    response_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    if report_path:
        report_output = Path(report_path).resolve()
        report_output.parent.mkdir(parents=True, exist_ok=True)
        report_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    return output, report
