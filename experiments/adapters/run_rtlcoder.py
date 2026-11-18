#!/usr/bin/env python3
"""RTL-Coder adapter (RTLCoder baseline) for the spec-to-RTL comparison experiment.

Two execution modes
-------------------
``local_hf``
    The *released* RTLCoder-v1.1-gguf-4bit checkpoint (Mistral-7B, q4_0, the
    CPU build that the upstream README recommends when no GPU is available)
    executed through llama.cpp.  Used by ``--mode auto`` whenever the GGUF
    file and the ``llama_cpp`` python package are present.  The CPU runtime is
    installed into ``experiments/_rtlcoder_local/pylibs`` and added to
    ``sys.path`` here, so no environment variables are required.

``api_prompt``
    RTLCoder-*style* prompting of an AIC chat model (``deepseek`` / ``qwen``):
    the same instruction template, the same module-header priming and the same
    response post-processing as the upstream inference scripts, but **not** the
    released RTLCoder weights.  ``result.json`` records this explicitly in
    ``mode_note`` / ``weights`` so downstream comparisons do not confuse it
    with a true RTLCoder run.

Prompt construction follows ``benchmark_inference/test_on_rtllm.py``::

    prompt = Instruction + "\\n" + Input + "\\n"

where ``Instruction`` is the case's ``prompt.md`` and ``Input`` is the module
header (``module <name>( ... );``, including ``#(...)`` parameters) taken from
the reference RTL and renamed to the case's expected top module.  VerilogEval
``prompt.md`` files already embed that header; it is extracted, replaced by the
reference-derived one and kept last so the model continues with the body.

Two additions are made on top of the raw RTLCoder template (both are recorded in
``result.json``):

* an output contract ("code only, start with ``module``, end with ``endmodule``")
  inserted between the instruction and the header.  Chat models otherwise like
  to answer with prose or markdown;
* a retry loop (``--max-attempts``, default 3).  An attempt is rejected and
  regenerated when extraction is empty, ``endmodule`` is missing (truncation) or
  Verilator fails to compile it.  The retry prompt carries the concrete failure
  (compile ``%Error`` lines / truncation) as a hint, and the retry temperature is
  raised so a deterministic ``temperature=0`` first pass can still produce a
  different answer.

Post-processing follows the upstream Mistral extraction (keep up to the last
``endmodule``, drop anything after a ``tb_module`` / ``testbench`` marker), then
``common.extract_verilog`` for fences/truncation, a prose stripper for text
emitted right after the module header, a ``begin``/``case`` balance repair for
truncated bodies, and finally a forced top-module name.

Examples
--------
    # released CPU weights (auto-detected when the GGUF + llama_cpp exist)
    python run_rtlcoder.py --case RTLLM/accu --model deepseek --out runs/rtl_accu
    # prompt-style run on an AIC model
    python run_rtlcoder.py --case VERILOGEVAL/Prob146 --model qwen --mode api_prompt \
        --out runs/rtl_p146

Local runtime / headless CPU setup (already done on this host)::

    mkdir -p experiments/_rtlcoder_local/{models,wheels}
    curl -L -o experiments/_rtlcoder_local/models/ggml-model-q4_0.gguf \
        https://huggingface.co/ishorn5/RTLCoder-v1.1-gguf-4bit/resolve/main/ggml-model-q4_0.gguf
    pip install --target experiments/_rtlcoder_local/pylibs llama-cpp-python \
        --extra-index-url https://abetlen.github.io/llama-cpp-python/whl/cpu
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import sys
import time
import urllib.request
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import common  # noqa: E402  (path setup must come first)

LOCAL_ROOT = HERE.parent / "_rtlcoder_local"
DEFAULT_GGUF = LOCAL_ROOT / "models" / "ggml-model-q4_0.gguf"
LOCAL_LIBS = LOCAL_ROOT / "pylibs"   # llama-cpp-python CPU wheel, installed with --target
GGUF_ENV = "RTLCoder_GGUF"
LOCAL_REPO_ID = "ishorn5/RTLCoder-v1.1-gguf-4bit"

# The host's system python has no torch/transformers/llama_cpp; the CPU runtime
# lives in ``_rtlcoder_local/pylibs`` (installed with ``pip install --target``)
# so that no environment variables are needed to reproduce a local run.
if LOCAL_LIBS.is_dir() and str(LOCAL_LIBS) not in sys.path:
    sys.path.insert(0, str(LOCAL_LIBS))

CONTRACT = """Output requirements:
- Return the complete, self-contained Verilog/SystemVerilog module named `{top}`.
- Output code only: no explanation, no prose, no markdown fences, no testbench.
- The response must start with `module {top}` and finish with `endmodule`."""

RETRY_HINT = """Note: the previous attempt was rejected because it {why}.
Produce the complete module again from the start, obeying every output requirement.
"""

_MARKER = re.compile(r"(?m)^[ \t]*module\s+[A-Za-z_]\w*")

# Lines that may legitimately follow a module header in Verilog / SV.
# (Single-line pattern: ``#`` cannot be used in verbose mode, it starts a comment.)
_CODE_LINE = re.compile(
    r"^\s*(?:input|output|inout|wire|reg|logic|bit|byte|int|integer|shortint|longint|"
    r"real|realtime|time|signed|unsigned|assign|always|always_ff|always_comb|"
    r"always_latch|initial|final|parameter|localparam|defparam|generate|endgenerate|"
    r"genvar|for|if|else|case|casex|casez|endcase|begin|end|function|endfunction|"
    r"task|endtask|typedef|import|export|package|endpackage|module|endmodule|"
    r"interface|modport|program|class|struct|enum|union|property|assert|assume|"
    r"cover|sequence|specify|endspecify|table|endtable|primitive|endprimitive|wait|"
    r"disable|fork|join|join_any|join_none|repeat|while|do|forever|return|break|"
    r"continue|automatic|static|`\w+|\$[\w$]+|//|/\*|\*/|"
    r"[A-Za-z_]\w*\s*(?:\#\s*\(|\[|<|=|;|,))")


# --------------------------------------------------------------------------- #
# prompt construction
# --------------------------------------------------------------------------- #
def module_header(reference_text: str, top: str) -> str:
    """Return the ``module <top>( ... );`` header of the reference RTL."""
    m = _MARKER.search(reference_text)
    if not m:
        return f"module {top}();"
    start = m.start()
    end = reference_text.find(");", m.end())
    if end != -1 and "endmodule" not in reference_text[m.end():end]:
        header = reference_text[start:end + 2]
    else:  # non-ANSI / header terminated by a plain ';'
        semi = reference_text.find(";", m.end())
        header = reference_text[start:semi + 1] if semi != -1 else reference_text[start:]
    header = re.sub(r"(?m)^([ \t]*)module\s+[A-Za-z_]\w*",
                    lambda mo: f"{mo.group(1)}module {top}", header, count=1)
    return header.strip()


def build_prompt(case: dict[str, Any], instruction: str, reference: str,
                 hint: str = "") -> tuple[str, dict[str, Any]]:
    """RTLCoder template: instruction (+ contract) then the module header last."""
    top = case["top_module"]
    header = module_header(reference, top)
    body = instruction.strip()
    embedded = re.search(rf"(?m)^[ \t]*module\s+{re.escape(top)}\b", body)
    inline = embedded is not None
    if inline:  # VerilogEval prompt.md already ends with the header: rebuild it
        body = body[:embedded.start()].rstrip()
    contract = CONTRACT.format(top=top)
    hint_block = RETRY_HINT.format(why=hint) + "\n" if hint else ""
    prompt = f"{body}\n\n{contract}\n\n{hint_block}{header}\n"
    meta = {"template": "instruction+contract+header",
            "header": header,
            "prompt_chars": len(prompt),
            "inline_header_replaced": inline,
            "retry_hint": bool(hint)}
    return prompt, meta


# --------------------------------------------------------------------------- #
# response post-processing
# --------------------------------------------------------------------------- #
def postprocess(text: str) -> tuple[str, dict[str, Any]]:
    """RTLCoder / Mistral extraction: anchor on the last endmodule, drop testbenches."""
    notes: list[str] = []
    s = (text or "").strip()
    if not s:
        return "", {"notes": ["empty_response"]}
    if "endmodule" in s:
        s = s.rsplit("endmodule", 1)[0] + "\nendmodule"
        notes.append("endmodule_anchor")
    first_mod = _MARKER.search(s)
    floor = first_mod.start() if first_mod else 0
    idx = s.rfind("tb_module")
    if idx != -1 and idx < floor:
        idx = -1
    if idx == -1:
        probe = s.find("testbench")
        idx = probe if probe > floor else -1
    if idx != -1:
        s = s[:idx].rsplit("endmodule", 1)[0] + "\nendmodule"
        notes.append("trimmed_at_testbench_marker")
    code = common.extract_verilog(s)
    if code and "endmodule" not in code:
        code = code.rstrip() + "\nendmodule\n"
        notes.append("appended_missing_endmodule")
    return code, {"notes": notes, "raw_chars": len(text)}


def _is_prose(line: str) -> bool:
    s = line.strip()
    if not s:
        return False
    if s.startswith("```"):
        return True
    if _CODE_LINE.match(s):
        return False
    if re.search(r"[;=]", s) or re.search(r"[(){}\[\]]", s):
        return False
    if re.match(r"^[A-Za-z_]\w*\s*(?::|,)", s):
        return False
    return len(s.split()) >= 3


def strip_prose_after_header(code: str) -> tuple[str, dict[str, Any]]:
    """Drop natural-language lines emitted between the header and the first statement."""
    m = _MARKER.search(code)
    if not m:
        return code, {"prose_lines_removed": 0}
    end = code.find(");", m.end())
    if end != -1 and "endmodule" not in code[m.end():end]:
        end += 2
    else:
        semi = code.find(";", m.end())
        end = semi + 1 if semi != -1 else -1
    if end <= 0:
        return code, {"prose_lines_removed": 0}
    head, tail = code[:end], code[end:]
    lines = tail.splitlines(keepends=True)
    removed = 0
    i = 0
    while i < len(lines) and removed < 40:
        line = lines[i]
        if not line.strip():
            i += 1
            continue
        if _is_prose(line):
            removed += 1
            i += 1
            continue
        break
    if not removed:
        return code, {"prose_lines_removed": 0}
    rest = "".join(lines[i:])
    if not rest.startswith("\n"):
        rest = "\n" + rest
    return head + rest, {"prose_lines_removed": removed}


def close_unbalanced_blocks(code: str) -> tuple[str, dict[str, Any]]:
    """Close begin/case blocks left open by a truncated generation."""
    body = re.sub(r"//[^\n]*", "", code)
    body = re.sub(r"/\*.*?\*/", "", body, flags=re.S)
    body = re.sub(r'"(?:\\.|[^"\\])*"', '""', body)
    opens = len(re.findall(r"\bbegin\b", body))
    closes = len(re.findall(r"\bend\b", body))
    cases = len(re.findall(r"\bcase[xz]?\b", body))
    endcases = len(re.findall(r"\bendcase\b", body))
    need_end = max(0, opens - closes)
    need_endcase = max(0, cases - endcases)
    if not need_end and not need_endcase:
        return code, {"added_end": 0, "added_endcase": 0}
    idx = code.rfind("endmodule")
    indent = "    "
    filler = (indent + "endcase\n") * need_endcase + (indent + "end\n") * need_end
    if idx == -1:
        repaired = code.rstrip() + "\n" + filler
    else:
        repaired = code[:idx] + filler + code[idx:]
    return repaired, {"added_end": need_end, "added_endcase": need_endcase}


def ensure_top_module(code: str, top: str, header: str) -> tuple[str, dict[str, Any]]:
    """Force the expected top-module name; wrap a bodiless answer (RTLCoder continuation)."""
    info: dict[str, Any] = {"expected": top, "renamed_from": None, "wrapped": False}
    if not code.strip():
        return code, info
    decls = list(_MARKER.finditer(code))
    if not decls:
        body = code.strip()
        if not body.endswith("endmodule"):
            body = body + "\nendmodule"
        out = header.rstrip() + "\n\n" + body + "\n"
        info.update(wrapped=True)
        return out, info
    names = [re.sub(r"^\s*module\s+", "", d.group(0)).strip() for d in decls]
    info["modules"] = names
    if top in names:
        return code, info
    cand = names[0]
    for name in names:  # prefer a module nobody instantiates
        if not re.search(rf"\b{re.escape(name)}\s+[A-Za-z_]\w*\s*\(", code):
            cand = name
            break
    code = re.sub(rf"(?m)^([ \t]*)module\s+{re.escape(cand)}\b",
                  lambda mo: f"{mo.group(1)}module {top}", code, count=1)
    info.update(renamed_from=cand)
    return code, info


# --------------------------------------------------------------------------- #
# generation backends
# --------------------------------------------------------------------------- #
def _http_chat(messages: list[dict[str, str]], cfg: dict[str, str],
               temperature: float, max_tokens: int, timeout: int = 900
               ) -> tuple[str, dict[str, Any]]:
    """Minimal OpenAI-compatible chat call (used when `openai` is absent)."""
    body = json.dumps({"model": cfg["model"], "messages": messages,
                       "temperature": temperature,
                       "max_tokens": max_tokens}).encode("utf-8")
    req = urllib.request.Request(
        cfg["base"].rstrip("/") + "/chat/completions", data=body,
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + cfg["key"]})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        data = json.loads(resp.read().decode("utf-8", "replace"))
    text = (data["choices"][0]["message"].get("content") or "")
    return text, (data.get("usage") or {})


def call_chat(messages: list[dict[str, str]], model_name: str, *,
              temperature: float, max_tokens: int
              ) -> tuple[str, dict[str, Any], str]:
    if importlib.util.find_spec("openai") is not None:
        text, usage = common.call_llm(messages, model_name,
                                      temperature=temperature,
                                      max_tokens=max_tokens)
        return text, usage, "openai_sdk"
    # Host python may have no `openai`; fall back to a direct HTTP call using
    # the very same endpoint/credentials that common.model_config resolves.
    cfg = common.model_config(model_name)
    text, usage = _http_chat(messages, cfg, temperature, max_tokens)
    return text, usage, "urllib_fallback"


def run_local(prompt: str, gguf: Path, *, max_new_tokens: int,
              temperature: float, seed: int) -> tuple[str, dict[str, Any]]:
    if importlib.util.find_spec("llama_cpp") is None:
        raise RuntimeError(
            "llama_cpp is not importable; install llama-cpp-python (CPU wheel: "
            "pip install llama-cpp-python --extra-index-url "
            "https://abetlen.github.io/llama-cpp-python/whl/cpu)")
    from llama_cpp import Llama

    n_threads = int(os.environ.get("RTLCoder_THREADS", "0")) or min(
        32, os.cpu_count() or 8)
    n_ctx = int(os.environ.get("RTLCoder_NCTX", "8192"))
    t0 = time.time()
    llm = Llama(model_path=str(gguf), n_ctx=n_ctx, n_threads=n_threads,
                n_batch=512, seed=seed, verbose=False)
    load_s = time.time() - t0
    out = llm(prompt, max_tokens=max_new_tokens, temperature=temperature,
              top_p=0.95,
              stop=["\nmodule tb_module", "\nmodule testbench", "\nendmodule"])
    text = (out["choices"][0].get("text") or "")
    usage = dict(out.get("usage") or {})
    usage["model_load_s"] = round(load_s, 2)
    usage["n_threads"] = n_threads
    return text, usage


# --------------------------------------------------------------------------- #
# mode resolution
# --------------------------------------------------------------------------- #
def resolve_mode(requested: str, gguf: Path) -> tuple[str, str]:
    if requested == "api_prompt":
        return "api_prompt", "explicit --mode api_prompt (AIC model, prompt-style)"
    if requested == "local_hf":
        if not gguf.is_file():
            raise SystemExit(f"--mode local_hf but GGUF missing: {gguf}")
        return "local_hf", f"explicit --mode local_hf (released weights: {LOCAL_REPO_ID})"
    usable = (gguf.is_file() and gguf.stat().st_size > 100_000_000
              and importlib.util.find_spec("llama_cpp") is not None)
    if usable:
        return "local_hf", f"auto: GGUF + llama_cpp found ({LOCAL_REPO_ID})"
    if gguf.is_file() and importlib.util.find_spec("llama_cpp") is None:
        return "api_prompt", ("auto: GGUF present but llama_cpp unavailable; "
                              "fell back to prompt-style AIC run")
    return "api_prompt", "auto: no local GGUF checkpoint; prompt-style AIC run"


# --------------------------------------------------------------------------- #
# main
# --------------------------------------------------------------------------- #
def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    ap = argparse.ArgumentParser(
        description="RTLCoder adapter: released CPU weights or RTLCoder-style AIC prompting")
    ap.add_argument("--case", required=True,
                    help="case id or short name, e.g. RTLLM/accu or VERILOGEVAL/Prob146")
    ap.add_argument("--model", required=True, choices=sorted(common.MODELS),
                    help="AIC model used in api_prompt mode (ignored for local_hf)")
    ap.add_argument("--out", required=True, help="output directory")
    ap.add_argument("--mode", default="auto",
                    choices=("auto", "api_prompt", "local_hf"))
    ap.add_argument("--temperature", type=float, default=None,
                    help="default: 0.0 for api_prompt, 0.5 for local_hf (repo setting)")
    ap.add_argument("--max-new-tokens", type=int, default=4096)
    ap.add_argument("--max-attempts", type=int, default=3,
                    help="generation attempts on truncation/compile failure")
    ap.add_argument("--gguf", default=os.environ.get(GGUF_ENV, str(DEFAULT_GGUF)))
    ap.add_argument("--seed", type=int, default=0)
    ap.add_argument("--eval-timeout", type=int, default=300)
    return ap.parse_args(argv)


def _attempt_hint(eval_res: dict[str, Any] | None, issues: list[str]) -> str:
    if "empty_response" in issues or "no_code" in issues:
        return "contained no Verilog module"
    if "missing_endmodule" in issues:
        return "was truncated before `endmodule`"
    if eval_res and eval_res.get("status") == "compile_failed":
        errs = [re.sub(r"\x1b\[[0-9;]*m", "", e)
                for e in (eval_res.get("errors") or [])][:3]
        if errs:
            return "did not compile: " + " | ".join(errs)[:600]
        return "did not compile"
    return "was rejected"


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    started = time.time()
    out_dir = Path(args.out).expanduser().resolve()
    out_dir.mkdir(parents=True, exist_ok=True)

    case = common.case_info(args.case)
    runner = common.RUNNER
    prompt_md = (runner / case["prompt"]).read_text(encoding="utf-8", errors="replace")
    reference = (runner / case["reference"]).read_text(encoding="utf-8", errors="replace")
    top = case["top_module"]

    mode, mode_note = resolve_mode(args.mode, Path(args.gguf))
    base_temp = args.temperature if args.temperature is not None else (
        0.5 if mode == "local_hf" else 0.0)

    payload: dict[str, Any] = {
        "system": "rtlcoder",
        "mode": mode,
        "mode_note": mode_note,
        "weights": (LOCAL_REPO_ID if mode == "local_hf"
                    else f"{common.MODELS[args.model]['model']} "
                         f"({common.model_config(args.model)['base']}, prompt-style)"),
        "case": case["case_id"],
        "model": (LOCAL_REPO_ID if mode == "local_hf" else args.model),
        "status": "error",
        "rtl_path": None,
        "eval": None,
        "llm_usage": None,
        "elapsed_s": None,
        "error": None,
        # extra bookkeeping
        "dataset": case["dataset"],
        "top_module": top,
        "base_temperature": base_temp,
        "max_new_tokens": args.max_new_tokens,
        "max_attempts": args.max_attempts,
        "prompt_file": str(out_dir / "prompt.txt"),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    if mode == "api_prompt":
        payload["prompt_style"] = True
        payload["disclaimer"] = ("prompt-style reproduction of RTLCoder behaviour on the "
                                 "configured endpoint; the released RTLCoder weights were NOT used")
    if mode == "local_hf":
        payload["gguf"] = str(Path(args.gguf).resolve())
        if LOCAL_LIBS.is_dir():
            payload["local_libs"] = str(LOCAL_LIBS)

    rtl_path = out_dir / f"{case['short']}.sv"
    attempts: list[dict[str, Any]] = []
    eval_res: dict[str, Any] | None = None
    hint = ""
    t_gen_all = 0.0

    for attempt in range(1, max(1, args.max_attempts) + 1):
        prompt, prompt_meta = build_prompt(case, prompt_md, reference, hint=hint)
        (out_dir / "prompt.txt").write_text(prompt, encoding="utf-8")
        temperature = base_temp if attempt == 1 else max(base_temp, 0.3)
        record: dict[str, Any] = {"attempt": attempt, "temperature": temperature,
                                  "retry_hint": hint or None}
        t_gen = time.time()
        try:
            if mode == "local_hf":
                text, usage = run_local(prompt, Path(args.gguf),
                                        max_new_tokens=args.max_new_tokens,
                                        temperature=temperature, seed=args.seed + attempt - 1)
                transport = "llama_cpp"
            else:
                text, usage, transport = call_chat(
                    [{"role": "user", "content": prompt}], args.model,
                    temperature=temperature, max_tokens=args.max_new_tokens)
        except Exception as exc:  # noqa: BLE001 - recorded in result.json
            record.update(status="error", error=f"{type(exc).__name__}: {exc}",
                          generation_s=round(time.time() - t_gen, 2))
            attempts.append(record)
            payload["error"] = record["error"]
            break

        gen_s = round(time.time() - t_gen, 2)
        t_gen_all += gen_s
        (out_dir / f"raw_response_attempt{attempt}.txt").write_text(text or "",
                                                                   encoding="utf-8")
        (out_dir / "raw_response.txt").write_text(text or "", encoding="utf-8")
        record.update(llm_usage=usage, llm_transport=transport, generation_s=gen_s)

        code, pp_meta = postprocess(text)
        code, prose_meta = strip_prose_after_header(code)
        code, repair_meta = close_unbalanced_blocks(code)
        code, top_meta = ensure_top_module(code, top, prompt_meta["header"])
        record["postprocess"] = {**pp_meta, **prose_meta, **repair_meta, **top_meta}

        issues: list[str] = []
        if not code.strip():
            issues.append("no_code")
        elif "endmodule" not in code:
            issues.append("missing_endmodule")
        if usage.get("completion_tokens") and usage["completion_tokens"] >= args.max_new_tokens:
            issues.append("hit_token_limit")
        record["issues"] = issues

        if code.strip():
            rtl_path.write_text(code, encoding="utf-8")
            payload["rtl_path"] = str(rtl_path)
            eval_res = common.evaluate_rtl(rtl_path, case, out_dir,
                                           timeout=args.eval_timeout)
            record["eval_status"] = eval_res.get("status")
            if eval_res.get("status") == "compile_failed":
                record["compile_errors"] = (eval_res.get("errors") or [])[:5]
            payload["eval"] = eval_res
            payload["status"] = eval_res.get("status", "error")
        else:
            eval_res = None
            record["eval_status"] = None
            payload["status"] = "error"
            payload["error"] = "no Verilog extracted from model response"
        attempts.append(record)

        status = record.get("eval_status")
        retryable = (status is None and payload["status"] == "error") or \
                    status == "compile_failed" or bool(issues)
        if not retryable or attempt >= args.max_attempts:
            break
        hint = _attempt_hint(eval_res, issues)

    payload["attempts"] = attempts
    payload["attempt_count"] = len(attempts)
    payload["generation_s"] = round(t_gen_all, 2)
    # last-used prompt metadata (header excluded: it is already in prompt.txt)
    if attempts:
        _, last_meta = build_prompt(case, prompt_md, reference, hint=hint)
        payload["prompt_meta"] = {k: v for k, v in last_meta.items() if k != "header"}
    payload["postprocess"] = attempts[-1].get("postprocess") if attempts else None
    payload["llm_usage"] = attempts[-1].get("llm_usage") if attempts else None
    payload["llm_transport"] = attempts[-1].get("llm_transport") if attempts else None

    final_eval = payload.get("eval") or {}
    if final_eval.get("status") == "compile_failed":
        payload["failure_summary"] = (final_eval.get("errors") or [])[:5]
    elif final_eval.get("status") == "failed":
        payload["failure_summary"] = (final_eval.get("failure_markers")
                                      or final_eval.get("errors") or [])[:5]

    payload["elapsed_s"] = round(time.time() - started, 2)
    payload["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")
    common.write_result(out_dir / "result.json", payload)

    print(json.dumps({"case": payload["case"], "system": payload["system"],
                      "mode": payload["mode"], "model": payload["model"],
                      "status": payload["status"],
                      "eval_status": (payload["eval"] or {}).get("status"),
                      "attempts": payload["attempt_count"],
                      "elapsed_s": payload["elapsed_s"],
                      "rtl_path": payload["rtl_path"],
                      "error": payload["error"]}, ensure_ascii=False))
    return 0 if payload["status"] == "passed" else 1


if __name__ == "__main__":
    sys.exit(main())