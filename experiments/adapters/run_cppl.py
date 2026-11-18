#!/usr/bin/env python3
"""CPPL (CircuitPPL) adapter for the spec-to-RTL comparison experiment.

Pipeline
--------
1. Read the case's ``prompt.md``, reference RTL and testbench from
   ``version2/experiments/runner_cases`` (the shared manifest).
2. Parse the DUT port list from the reference RTL (ANSI *and* non-ANSI
   headers, parameterised widths, ``$clog2``, ``output reg`` ...).  Ports are
   mapped to CPPL ``In[N]`` / ``Clock`` inputs and ``Out[N]`` outputs, keeping
   the reference declaration order and exact widths.
3. Emit a CPPL ``@module`` Python file whose function name is the case's
   expected ``top_module`` and whose docstring is the full ``prompt.md`` text.
4. Compile it with CPPL's LLM compiler against an AIC chat model
   (``deepseek`` or ``qwen``, see :data:`common.MODELS`) and export Verilog via
   CIRCT/pycde (``Design.to_verilog``).
5. Save the Verilog, force the expected top-module name, and evaluate it with
   :func:`common.evaluate_rtl`.
6. Write ``result.json`` next to the generated RTL.

The CPPL package is installed editable into ``adapters/.venv_cppl`` (created
with Python 3.10 so that the ``pycde==0.9.1.dev146`` cp310 wheel is usable)::

    python3 -m venv version2/experiments/adapters/.venv_cppl
    version2/experiments/adapters/.venv_cppl/bin/pip install -e \
        version2/baselines/spec2rtl/CPPL

Run with that interpreter::

    .venv_cppl/bin/python run_cppl.py --case RTLLM/accu --model deepseek \
        --out runs/cppl_accu_deepseek

Secrets (``DAG_API_KEY``) are read through :func:`common.model_config` and are
never logged or written to ``result.json``.
"""
from __future__ import annotations

import argparse
import ast
import contextlib
import importlib.util
import json
import keyword
import math
import os
import re
import signal
import sys
import time
import traceback
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
if str(HERE) not in sys.path:
    sys.path.insert(0, str(HERE))

import common  # noqa: E402

CPPL_DIR = common.EXPERIMENTS.parent / "baselines" / "spec2rtl" / "CPPL"
DEFAULT_VENV = HERE / ".venv_cppl"

CLOCK_NAMES = {
    "clk", "clock", "aclk", "wclk", "rclk", "clk_i", "i_clk", "clock_i",
    "i_clock", "clk_in", "clock_in",
}
_NET_KEYWORDS = re.compile(
    r"\b(?:reg|wire|logic|signed|unsigned|integer|int|bit|var|tri|tri0|tri1|"
    r"wand|wor|supply0|supply1)\b",
    re.I,
)
_MODULE_DECL = re.compile(r"(?m)^[ \t]*(?:module|macromodule)[ \t]+([A-Za-z_]\w*)")
_ENDMODULE = re.compile(r"(?m)^[ \t]*endmodule\b")


class PortParseError(RuntimeError):
    """Raised when the reference RTL port list cannot be understood."""


class CpplTimeout(RuntimeError):
    """Raised when the CPPL compile exceeds its wall-clock budget."""


# ---------------------------------------------------------------------------
# Verilog source helpers
# ---------------------------------------------------------------------------

def _strip_comments(text: str) -> str:
    text = re.sub(r"/\*.*?\*/", " ", text, flags=re.S)
    text = re.sub(r"//[^\n]*", "", text)
    return text


def _find_matching(text: str, start: int) -> int:
    """Return the index of the bracket closing the one at *start*."""
    pairs = {"(": ")", "[": "]", "{": "}"}
    opener = text[start]
    closer = pairs[opener]
    depth = 0
    for i in range(start, len(text)):
        ch = text[i]
        if ch == opener:
            depth += 1
        elif ch == closer:
            depth -= 1
            if depth == 0:
                return i
    raise PortParseError(f"unbalanced {opener!r} starting at offset {start}")


def _split_top_level(text: str, sep: str = ",") -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for ch in text:
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        if ch == sep and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(ch)
    if "".join(current).strip():
        parts.append("".join(current))
    return parts


def _module_spans(text: str) -> list[tuple[str, int, int]]:
    spans: list[tuple[str, int, int]] = []
    for match in _MODULE_DECL.finditer(text):
        end_match = _ENDMODULE.search(text, match.end())
        if end_match:
            end = end_match.end()
        else:
            following = _MODULE_DECL.search(text, match.end())
            end = following.start() if following else len(text)
        spans.append((match.group(1), match.start(), end))
    return spans


def _select_dut_module(text: str, top: str, case_id: str):
    """Pick the module that describes the DUT, returning (name, block, note)."""
    spans = _module_spans(text)
    if not spans:
        raise PortParseError(f"no module declaration found in reference for {case_id}")

    for name, start, end in spans:
        if name == top:
            return name, text[start:end], None
    for name, start, end in spans:
        if name in (f"verified_{top}", f"verified{top}", f"{top}_top"):
            return name, text[start:end], f"selected '{name}' for expected top '{top}'"
    for name, start, end in spans:
        if name.lower().endswith(top.lower()):
            return name, text[start:end], f"selected '{name}' for expected top '{top}'"
    name, start, end = spans[-1]
    return name, text[start:end], (
        f"no module matched expected top '{top}'; using last module '{name}'"
    )


# ---------------------------------------------------------------------------
# Parameter / width evaluation
# ---------------------------------------------------------------------------

def _parse_param_list(text: str) -> dict[str, str]:
    params: dict[str, str] = {}
    decl_re = re.compile(
        r"^\s*(?:parameter|localparam)?\s*"
        r"(?:(?:integer|int|signed|unsigned|type|bit|logic|reg)\s+)?"
        r"(?:\[[^\]]*\]\s*)?"
        r"([A-Za-z_]\w*)\s*=\s*(.+)$",
        re.S,
    )
    for item in _split_top_level(text):
        item = item.strip()
        if not item:
            continue
        match = decl_re.match(item)
        if match:
            params[match.group(1)] = match.group(2).strip()
    return params


def _clog2(value: int) -> int:
    value = int(value)
    if value <= 1:
        return 0
    return (value - 1).bit_length()


def _replace_verilog_literals(expr: str) -> str:
    def sized(match: re.Match) -> str:
        base = match.group(1).lower()
        digits = match.group(2).replace("_", "")
        if re.search(r"[xXzZ?]", digits):
            raise PortParseError(f"x/z literal in width expression: {match.group(0)!r}")
        return str(int(digits, {"d": 10, "h": 16, "o": 8, "b": 2}[base]))

    expr = re.sub(
        r"\d*\s*'\s*[sS]?([dDhHbBoO])\s*([0-9a-fA-FxXzZ_]+)", sized, expr
    )
    # Unsized fill literals: '0 / '1
    expr = re.sub(r"'\s*([01])\b", r"\1", expr)
    return expr.replace("$clog2", "$clog2")


def _convert_ternary(expr: str) -> str:
    """Rewrite Verilog ``cond ? a : b`` into a Python conditional expression."""
    depth = 0
    question = -1
    for i, ch in enumerate(expr):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == "?" and depth == 0:
            question = i
            break
    if question < 0:
        return expr
    depth = 0
    colon = -1
    for i in range(question + 1, len(expr)):
        ch = expr[i]
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ":" and depth == 0:
            colon = i
            break
    if colon < 0:
        raise PortParseError(f"malformed ternary expression {expr!r}")
    cond = expr[:question]
    when_true = expr[question + 1:colon]
    when_false = expr[colon + 1:]
    return (
        f"({_convert_ternary(when_true)} if {_convert_ternary(cond)} "
        f"else {_convert_ternary(when_false)})"
    )


def _safe_arith(expr: str) -> int:
    try:
        node = ast.parse(_convert_ternary(expr), mode="eval")
    except SyntaxError as exc:
        raise PortParseError(f"cannot parse width expression {expr!r}") from exc

    def ev(n: ast.AST) -> int:
        if isinstance(n, ast.Expression):
            return ev(n.body)
        if isinstance(n, ast.Constant) and isinstance(n.value, int):
            return n.value
        if isinstance(n, ast.UnaryOp) and isinstance(n.op, (ast.USub, ast.UAdd)):
            value = ev(n.operand)
            return -value if isinstance(n.op, ast.USub) else value
        if isinstance(n, ast.BinOp):
            left, right = ev(n.left), ev(n.right)
            if isinstance(n.op, ast.Add):
                return left + right
            if isinstance(n.op, ast.Sub):
                return left - right
            if isinstance(n.op, ast.Mult):
                return left * right
            if isinstance(n.op, ast.Div):
                return int(left / right) if right else 0
            if isinstance(n.op, ast.FloorDiv):
                return left // right if right else 0
            if isinstance(n.op, ast.Mod):
                return left % right if right else 0
            if isinstance(n.op, ast.Pow):
                return left ** right
        if isinstance(n, ast.Compare) and len(n.ops) == 1:
            left, right = ev(n.left), ev(n.comparators[0])
            op = n.ops[0]
            if isinstance(op, ast.Gt):
                return int(left > right)
            if isinstance(op, ast.GtE):
                return int(left >= right)
            if isinstance(op, ast.Lt):
                return int(left < right)
            if isinstance(op, ast.LtE):
                return int(left <= right)
            if isinstance(op, ast.Eq):
                return int(left == right)
            if isinstance(op, ast.NotEq):
                return int(left != right)
        if isinstance(n, ast.IfExp):
            return ev(n.body) if ev(n.test) else ev(n.orelse)
        if isinstance(n, ast.BoolOp):
            values = [ev(v) for v in n.values]
            if isinstance(n.op, ast.And):
                return int(all(values))
            if isinstance(n.op, ast.Or):
                return int(any(values))
        raise PortParseError(f"unsupported operator in width expression {expr!r}")

    return int(ev(node))


def _eval_expr(expr: str, params: dict[str, str], depth: int = 0) -> int:
    if depth > 16:
        raise PortParseError(f"parameter recursion too deep for {expr!r}")
    text = _replace_verilog_literals(expr.strip())

    while "$clog2" in text:
        match = re.search(r"\$clog2\s*\(([^()]*)\)", text)
        if not match:
            raise PortParseError(f"malformed $clog2 in {expr!r}")
        inner = _eval_expr(match.group(1), params, depth + 1)
        text = text[: match.start()] + str(_clog2(inner)) + text[match.end():]

    def substitute(match: re.Match) -> str:
        token = match.group(0)
        if token in params:
            return f"({_eval_expr(params[token], params, depth + 1)})"
        raise PortParseError(
            f"cannot resolve identifier {token!r} in width expression {expr!r}"
        )

    text = re.sub(r"[A-Za-z_]\w*", substitute, text)
    text = re.sub(r"(?<=\d)_(?=\d)", "", text)
    if not re.fullmatch(r"[\d\s+\-*/%()<>=!&|?:]+", text):
        raise PortParseError(f"unsupported width expression {expr!r} (reduced to {text!r})")
    return _safe_arith(text)


def _range_width(rng: str, params: dict[str, str]) -> int:
    rng = (rng or "").strip()
    if not rng or ":" not in rng:
        return 1
    msb, lsb = rng.split(":", 1)
    return abs(_eval_expr(msb, params) - _eval_expr(lsb, params)) + 1


# ---------------------------------------------------------------------------
# Port list parsing
# ---------------------------------------------------------------------------

def _clean_port_decl(rest: str) -> str:
    rest = re.sub(r"\[[^\]]*\]", " ", rest)
    rest = _NET_KEYWORDS.sub(" ", rest)
    rest = rest.split("=")[0]
    return rest.strip()


def _parse_ansi_ports(port_text: str, params: dict[str, str]) -> list[dict]:
    ports: list[dict] = []
    current_dir: str | None = None
    current_width = ""
    for chunk in _split_top_level(port_text):
        decl = chunk.strip()
        if not decl:
            continue
        match = re.match(r"(input|output|inout)\b(.*)$", decl, re.S | re.I)
        if match:
            current_dir = match.group(1).lower()
            rest = match.group(2)
            current_width = ""
        else:
            if current_dir is None:
                raise PortParseError(f"port declaration without direction: {decl!r}")
            rest = decl
        range_match = re.search(r"\[([^\]]*)\]", rest)
        if range_match:
            current_width = range_match.group(1)
        name = _clean_port_decl(rest)
        if not name or not re.fullmatch(r"[A-Za-z_]\w*", name):
            raise PortParseError(f"cannot extract port name from {decl!r}")
        ports.append({
            "name": name,
            "dir": current_dir,
            "width": _range_width(current_width, params),
            "range": current_width,
        })
    return ports


def _parse_nonansi_ports(port_text: str, body: str,
                         params: dict[str, str]) -> list[dict]:
    names = []
    for chunk in _split_top_level(port_text):
        name = chunk.strip()
        if name:
            names.append(name)
    declared: dict[str, dict] = {}
    for match in re.finditer(r"\b(input|output|inout)\b([^;]*);", body, re.I):
        direction = match.group(1).lower()
        rest = match.group(2)
        range_match = re.search(r"\[([^\]]*)\]", rest)
        width_text = range_match.group(1) if range_match else ""
        rest = re.sub(r"\[[^\]]*\]", " ", rest)
        rest = _NET_KEYWORDS.sub(" ", rest)
        for chunk in _split_top_level(rest):
            name = chunk.split("=")[0].strip()
            if not name:
                continue
            declared[name] = {
                "name": name,
                "dir": direction,
                "width": _range_width(width_text, params),
                "range": width_text,
            }
    ports: list[dict] = []
    for name in names:
        if name not in declared:
            raise PortParseError(
                f"non-ANSI port {name!r} has no direction declaration"
            )
        ports.append(declared[name])
    return ports


def parse_ports_from_text(text: str, top: str, case_id: str = "") -> tuple[list[dict], dict]:
    """Parse the DUT port list out of Verilog/SystemVerilog *text*."""
    clean = _strip_comments(text)
    dut_name, block, note = _select_dut_module(clean, top, case_id)
    notes = [note] if note else []

    # Header ends at the first top-level ';'.
    header_end = -1
    depth = 0
    for i, ch in enumerate(block):
        if ch in "([{":
            depth += 1
        elif ch in ")]}":
            depth -= 1
        elif ch == ";" and depth == 0:
            header_end = i
            break
    header = block[: header_end + 1] if header_end >= 0 else block

    params: dict[str, str] = {}
    port_text: str | None = None
    param_span: tuple[int, int] | None = None

    first_paren = header.find("(")
    if first_paren != -1:
        first_close = _find_matching(header, first_paren)
        if "#" in header[:first_paren]:
            params.update(_parse_param_list(header[first_paren + 1:first_close]))
            param_span = (header.find("#", 0, first_paren), first_close)
            next_paren = header.find("(", first_close)
            if next_paren != -1:
                port_text = header[next_paren + 1: _find_matching(header, next_paren)]
        else:
            port_text = header[first_paren + 1:first_close]

    # Body-level parameter/localparam declarations (after the header group).
    body_for_params = block
    if param_span is not None:
        start, end = param_span
        body_for_params = block[:start] + " " * (end - start) + block[end:]
    for match in re.finditer(r"\b(?:parameter|localparam)\b([^;]*);", body_for_params):
        params.update(_parse_param_list(match.group(1)))

    if port_text is None or not port_text.strip():
        ports: list[dict] = []
        notes.append(f"module '{dut_name}' declares no ports")
    elif re.search(r"\b(input|output|inout)\b", port_text, re.I):
        ports = _parse_ansi_ports(port_text, params)
    else:
        ports = _parse_nonansi_ports(port_text, block, params)

    # Normalise a couple of oddities and annotate clock ports.
    seen: set[str] = set()
    for port in ports:
        if port["name"] in seen:
            raise PortParseError(f"duplicate port {port['name']!r} in {case_id}")
        seen.add(port["name"])
        if port["dir"] == "inout":
            notes.append(f"inout port '{port['name']}' mapped to input")
            port["dir"] = "input"
        port["type"] = "clock" if _is_clock(port["name"], port["width"]) else "bits"

    meta = {
        "dut_module": dut_name,
        "params": params,
        "notes": notes,
    }
    return ports, meta


def _is_clock(name: str, width: int) -> bool:
    low = name.lower()
    return width == 1 and (low in CLOCK_NAMES or low.endswith("clk") or low.endswith("clock"))


def parse_reference_ports(path: Path, top: str, case_id: str = "") -> tuple[list[dict], dict]:
    text = Path(path).read_text(encoding="utf-8", errors="replace")
    return parse_ports_from_text(text, top, case_id)


# ---------------------------------------------------------------------------
# CPPL module source generation
# ---------------------------------------------------------------------------

def _py_doc_literal(text: str) -> str:
    body = text.replace("\r\n", "\n").replace("\r", "\n").strip("\n")
    body = body.replace("\\", "\\\\").replace('"""', '\\"\\"\\"')
    if body.endswith('"'):
        body = body[:-1] + '\\"'
    return '"""' + body + '"""'


def _ann(port: dict) -> str:
    if port["type"] == "clock":
        return "Clock"
    return f"In[{port['width']}]" if port["dir"] == "input" else f"Out[{port['width']}]"


def _safe_parameter(name: str) -> bool:
    return (
        name.isidentifier()
        and not keyword.iskeyword(name)
        and name not in {"return", "self", "cls"}
    )


def render_cppl_source(case_id: str, top: str, ports: list[dict], spec: str) -> str:
    """Render a CPPL ``@module`` file for the DUT."""
    inputs = [p for p in ports if p["dir"] == "input"]
    outputs = [p for p in ports if p["dir"] == "output"]
    doc = _py_doc_literal(spec)

    lines = [
        "from cppl import Clock, In, Out, module",
        "",
        f"# Generated by version2/experiments/adapters/run_cppl.py",
        f"# case: {case_id}",
        f"# expected top module: {top}",
    ]

    if all(_safe_parameter(p["name"]) for p in ports):
        signature = ", ".join(f"{p['name']}: {_ann(p)}" for p in inputs)
        returns = ", ".join(f'"{p["name"]}": Out[{p["width"]}]' for p in outputs)
        lines += [
            "@module",
            f"def {top}({signature}) -> {{{returns}}}:",
            "    " + doc,
            "    pass",
            "",
        ]
    else:
        # Verilog port names that are Python keywords (e.g. ``in``) cannot be
        # written as function parameters.  CPPL reads ports from
        # ``func.__annotations__`` and calls ``func(**proxy_kwargs)``, so a
        # ``**kwargs`` function plus a hand-built annotation dict works and
        # keeps the exact Verilog port names.
        annotations = [f'    "{p["name"]}": {_ann(p)},' for p in inputs]
        returns = ", ".join(f'"{p["name"]}": Out[{p["width"]}]' for p in outputs)
        annotations.append(f'    "return": {{{returns}}},')
        lines += [
            f"def {top}(**kwargs):",
            "    " + doc,
            "    pass",
            "",
            f"{top}.__annotations__ = {{",
            *annotations,
            "}",
            f"{top} = module({top})",
            "",
        ]
    return "\n".join(lines)


def build_cppl_module(path: Path, case_id: str, top: str, ports: list[dict],
                      spec: str) -> Path:
    path.write_text(render_cppl_source(case_id, top, ports, spec), encoding="utf-8")
    return path


def load_cppl_module(path: Path, top: str):
    module_name = f"cppl_generated_{re.sub(r'[^A-Za-z0-9_]', '_', top)}"
    spec = importlib.util.spec_from_file_location(module_name, path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f"cannot import generated CPPL module {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[module_name] = module
    spec.loader.exec_module(module)
    module_def = getattr(module, top, None)
    if module_def is None:
        raise RuntimeError(f"generated module {path} does not define '{top}'")
    return module_def


# ---------------------------------------------------------------------------
# CPPL compile
# ---------------------------------------------------------------------------

@contextlib.contextmanager
def _time_limit(seconds: float):
    if seconds <= 0 or not hasattr(signal, "SIGALRM"):
        yield
        return

    def handler(signum, frame):  # noqa: ARG001
        raise CpplTimeout(f"CPPL compile exceeded {seconds:.0f}s")

    previous = signal.signal(signal.SIGALRM, handler)
    signal.setitimer(signal.ITIMER_REAL, float(seconds))
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, previous)


def _set_llm_env(cfg: dict[str, str], args: argparse.Namespace) -> None:
    os.environ["LLM_PROVIDER"] = "openai"
    os.environ["LLM_MODEL"] = f"openai/{cfg['model']}"
    os.environ["LLM_BASE_URL"] = cfg["base"]
    os.environ["LLM_API_KEY"] = cfg["key"]
    os.environ["LLM_MAX_TOKENS"] = str(args.max_tokens)
    os.environ["LLM_TIMEOUT"] = str(args.request_timeout)


def compile_with_cppl(module_def, top: str, out_dir: Path,
                      args: argparse.Namespace, cfg: dict[str, str]):
    _set_llm_env(cfg, args)
    try:
        from cppl import AgentConfig, Design
    except Exception as exc:  # noqa: BLE001
        raise RuntimeError(
            f"cannot import CPPL ({type(exc).__name__}: {exc}). "
            f"Install it into {DEFAULT_VENV}: "
            f"`{DEFAULT_VENV}/bin/pip install -e {CPPL_DIR}`"
        ) from exc

    agent_config = AgentConfig(
        max_parallelism=1,
        cache_enabled=not args.no_cache,
        cache_dir=out_dir / ".cppl_cache",
        checkpoint_enabled=False,
        log_enabled=not args.quiet,
        transport_retries=args.transport_retries,
        request_timeout=args.request_timeout,
        module_deadline_seconds=args.deadline,
        tool_mode=args.tool_mode,
    )
    design = Design(agent_config=agent_config)
    design.add(module_def)
    started = time.time()
    with _time_limit(args.timeout):
        verilog = design.to_verilog(top=top, max_retries=args.max_retries)
    return verilog, design, round(time.time() - started, 2)


def _llm_usage(design) -> dict[str, Any] | None:
    report = getattr(design, "last_compile_report", None)
    if report is None:
        return None
    modules = {}
    for name, module_report in report.module_reports.items():
        modules[name] = {
            "status": module_report.status,
            "attempts": module_report.attempts,
            "model_turns": module_report.model_turns,
            "tool_calls": module_report.tool_calls,
            "simulation_attempts": module_report.simulation_attempts,
            "static_repairs": module_report.static_repairs,
            "input_tokens_estimate": module_report.input_tokens_estimate,
            "output_tokens_estimate": module_report.output_tokens_estimate,
            "cache_hit": module_report.cache_hit,
            "duration_seconds": module_report.duration_seconds,
            "error": module_report.error,
        }
    return {
        "source": "cppl_report_estimates",
        "llm_calls": report.llm_calls,
        "cache_hits": report.cache_hits,
        "run_id": report.run_id,
        "duration_seconds": report.duration_seconds,
        "input_tokens_estimate": sum(
            r.input_tokens_estimate for r in report.module_reports.values()
        ),
        "output_tokens_estimate": sum(
            r.output_tokens_estimate for r in report.module_reports.values()
        ),
        "modules": modules,
    }


# ---------------------------------------------------------------------------
# Output post-processing
# ---------------------------------------------------------------------------

def ensure_top_module(verilog: str, top: str) -> tuple[str, str | None]:
    """Guarantee the Verilog defines ``top``; rename a single stray module."""
    names = re.findall(r"(?m)^[ \t]*module[ \t]+([A-Za-z_]\w*)", verilog)
    if top in names:
        return verilog, None
    if len(names) == 1:
        old = names[0]
        verilog = re.sub(
            rf"(?m)^([ \t]*module[ \t]+){re.escape(old)}\b",
            rf"\g<1>{top}",
            verilog,
            count=1,
        )
        return verilog, f"renamed generated module '{old}' to '{top}'"
    raise RuntimeError(
        f"generated Verilog does not define top module '{top}' "
        f"(found: {names or ['<none>']})"
    )


def _port_signature(ports: list[dict]) -> dict[str, tuple[str, int]]:
    return {p["name"]: (p["dir"], p["width"]) for p in ports}


def port_check(expected: list[dict], generated: list[dict]) -> dict[str, Any]:
    exp = _port_signature(expected)
    got = _port_signature(generated)
    missing = [name for name in exp if name not in got]
    extra = [name for name in got if name not in exp]
    mismatch = []
    for name in exp:
        if name in got and exp[name] != got[name]:
            mismatch.append({
                "name": name,
                "expected": list(exp[name]),
                "generated": list(got[name]),
            })
    return {
        "ok": not missing and not extra and not mismatch,
        "missing": missing,
        "extra": extra,
        "mismatch": mismatch,
    }


_MISMATCH_RE = re.compile(r"(?mi)^\s*(?:Mismatches:\s*(\d+)\s*in\s*(\d+)\s*samples|"
                          r"Hint:\s*Output\s+'([^']+)'\s*has\s*(\d+)\s*mismatches)")
_TIMEOUT_RE = re.compile(r"(?m)^\s*TIMEOUT\s*$")

# ``common.evaluate_rtl`` only looks for a couple of textual markers; several
# runner_cases testbenches report failures differently.  These patterns are
# used *in addition* to the shared verdict (never to turn a failure into a
# pass), so a comparison run does not silently count a broken design as passed.
_FAILURE_PATTERNS = [
    (re.compile(r"(?mi)^\s*={3,}\s*Error\s*={3,}\s*$"), "testbench error banner"),
    (re.compile(r"(?mi)^\s*Mismatches:\s*[1-9]\d*\s*in\s*\d+\s*samples"),
     "mismatch summary"),
    (re.compile(r"(?mi)^\s*Hint:\s*Output\s+'[^']+'\s*has\s*[1-9]\d*\s*mismatches"),
     "per-output mismatches"),
    (re.compile(r"(?m)^\s*TIMEOUT\s*$"), "simulation timeout"),
    (re.compile(r"(?i)\bTest failed\b"), "test failed"),
    (re.compile(r"(?i)\bFatal\b"), "fatal"),
]


def eval_notes(evaluation: dict[str, Any], out_dir: Path | None = None) -> dict[str, Any]:
    """Extra, harness-independent observations about the simulation log."""
    log = evaluation.get("log_tail") or ""
    full_log = False
    if out_dir is not None:
        log_path = Path(out_dir) / "simulation.log"
        if log_path.is_file():
            log = log_path.read_text(encoding="utf-8", errors="replace")
            full_log = True
    total = None
    samples = None
    mismatch_match = re.search(r"(?mi)^\s*Mismatches:\s*(\d+)\s*in\s*(\d+)\s*samples", log)
    if mismatch_match:
        total = int(mismatch_match.group(1))
        samples = int(mismatch_match.group(2))
    per_output = {
        m.group(1): int(m.group(2))
        for m in re.finditer(
            r"(?mi)Hint:\s*Output\s+'([^']+)'\s*has\s*(\d+)\s*mismatches", log
        )
    }
    markers = [label for pattern, label in _FAILURE_PATTERNS if pattern.search(log)]
    return {
        "log_mismatches": total,
        "log_samples": samples,
        "per_output_mismatches": per_output,
        "log_timeout": bool(_TIMEOUT_RE.search(log)),
        "failure_markers": markers,
        "full_simulation_log": full_log,
    }


# ---------------------------------------------------------------------------
# CLI / orchestration
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Compile a runner_cases case with CPPL and evaluate the RTL.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--case", required=True, help="case_id or short name")
    parser.add_argument("--model", default="deepseek", choices=sorted(common.MODELS))
    parser.add_argument("--out", required=True, help="output directory")
    parser.add_argument("--max-retries", type=int, default=3,
                        help="CPPL simulation/pattern attempts")
    parser.add_argument("--request-timeout", type=float, default=600.0,
                        help="per-LLM-request timeout (seconds)")
    parser.add_argument("--deadline", type=float, default=1800.0,
                        help="CPPL per-module time budget (seconds)")
    parser.add_argument("--timeout", type=float, default=3600.0,
                        help="hard wall-clock budget for the whole CPPL compile")
    parser.add_argument("--max-tokens", type=int, default=12000,
                        help="LLM output token budget")
    parser.add_argument("--eval-timeout", type=int, default=300,
                        help="Verilator compile/run timeout (seconds)")
    parser.add_argument("--tool-mode", default="off", choices=["auto", "off", "required"])
    parser.add_argument("--transport-retries", type=int, default=2)
    parser.add_argument("--no-cache", action="store_true",
                        help="disable CPPL's content-addressed result cache")
    parser.add_argument("--quiet", action="store_true", help="silence CPPL logs")
    parser.add_argument("--parse-only", action="store_true",
                        help="only parse the reference ports / emit the CPPL module")
    return parser


def execute(args: argparse.Namespace) -> dict[str, Any]:
    out_dir = Path(args.out).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    payload: dict[str, Any] = {
        "system": "cppl",
        "case": args.case,
        "model": args.model,
        "status": "error",
        "rtl_path": None,
        "eval": None,
        "llm_usage": None,
        "elapsed_s": None,
        "error": None,
        "out_dir": str(out_dir),
        "started_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }
    design = None
    try:
        case = common.case_info(args.case)
        payload["case"] = case["case_id"]
        payload["dataset"] = case.get("dataset")
        top = case["top_module"]
        payload["top_module"] = top
        payload["prompt_file"] = str((common.RUNNER / case["prompt"]).resolve())
        payload["reference_file"] = str((common.RUNNER / case["reference"]).resolve())

        reference = common.RUNNER / case["reference"]
        ports, meta = parse_reference_ports(reference, top, case["case_id"])
        if not ports:
            raise PortParseError(f"no ports parsed from {reference}")
        payload["ports"] = ports
        payload["port_parse"] = meta

        spec = (common.RUNNER / case["prompt"]).read_text(
            encoding="utf-8", errors="replace"
        )
        module_path = build_cppl_module(
            out_dir / "cppl_module.py", case["case_id"], top, ports, spec
        )
        payload["cppl_module"] = str(module_path)
        if args.parse_only:
            payload["status"] = "parsed"
            payload["elapsed_s"] = round(time.time() - started, 2)
            return payload

        cfg = common.model_config(args.model)
        payload["model_id"] = cfg["model"]
        payload["model_base"] = cfg["base"]

        module_def = load_cppl_module(module_path, top)
        verilog, design, cppl_seconds = compile_with_cppl(
            module_def, top, out_dir, args, cfg
        )
        payload["cppl_elapsed_s"] = cppl_seconds
        payload["llm_usage"] = _llm_usage(design)

        report = getattr(design, "last_compile_report", None)
        if report is not None and report.modules:
            (out_dir / "cppl_ir.json").write_text(
                json.dumps(report.modules, indent=2) + "\n", encoding="utf-8"
            )

        verilog, rename_note = ensure_top_module(verilog, top)
        if rename_note:
            payload.setdefault("notes", []).append(rename_note)

        rtl_text = common.extract_verilog(verilog) or (
            verilog.strip() + "\n" if verilog.strip() else ""
        )
        rtl_path = out_dir / f"{top}.sv"
        rtl_path.write_text(rtl_text, encoding="utf-8")
        payload["rtl_path"] = str(rtl_path)

        try:
            generated_ports, generated_meta = parse_ports_from_text(rtl_text, top, case["case_id"])
            payload["port_check"] = port_check(ports, generated_ports)
            payload["generated_ports"] = generated_ports
            if generated_meta.get("notes"):
                payload.setdefault("notes", []).extend(generated_meta["notes"])
        except Exception as exc:  # noqa: BLE001 - diagnostic only
            payload["port_check"] = {"ok": False, "error": f"{type(exc).__name__}: {exc}"}

        evaluation = common.evaluate_rtl(
            rtl_path, case, out_dir, timeout=args.eval_timeout
        )
        payload["eval"] = evaluation
        notes = eval_notes(evaluation, out_dir)
        payload["eval_notes"] = notes
        harness_status = evaluation.get("status")
        final_status = harness_status
        if harness_status == "passed" and notes["failure_markers"]:
            final_status = "failed"
            payload["eval_override"] = {
                "from": "passed",
                "to": "failed",
                "reason": (
                    "common.evaluate_rtl did not recognise the testbench failure "
                    f"markers {notes['failure_markers']}"
                ),
            }
        payload["status"] = "passed" if final_status == "passed" else "failed"

    except Exception as exc:  # noqa: BLE001 - recorded in result.json
        payload["status"] = "error"
        payload["error"] = f"{type(exc).__name__}: {exc}"
        payload["traceback_tail"] = traceback.format_exc()[-4000:]
        if design is not None:
            payload["llm_usage"] = _llm_usage(design)
            report = getattr(design, "last_compile_report", None)
            if report is not None:
                payload["cppl_design_error"] = report.design_error
                payload["cppl_failures"] = {
                    name: {"status": r.status, "error": r.error}
                    for name, r in report.failures.items()
                }
    finally:
        payload["elapsed_s"] = round(time.time() - started, 2)
        payload["finished_at"] = time.strftime("%Y-%m-%dT%H:%M:%S%z")

    common.write_result(out_dir / "result.json", payload)
    return payload


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.parse_only and not Path(args.out).exists():
        Path(args.out).mkdir(parents=True, exist_ok=True)
    payload = execute(args)

    if args.parse_only:
        print(json.dumps({
            "case": payload["case"],
            "top_module": payload.get("top_module"),
            "ports": payload.get("ports"),
            "port_parse": payload.get("port_parse"),
            "cppl_module": payload.get("cppl_module"),
        }, indent=2))
        return 0 if payload["status"] == "parsed" else 1

    summary = {
        "status": payload["status"],
        "case": payload["case"],
        "model": payload["model"],
        "top_module": payload.get("top_module"),
        "rtl_path": payload.get("rtl_path"),
        "result": str((Path(payload["out_dir"]) / "result.json")),
        "elapsed_s": payload["elapsed_s"],
        "error": payload.get("error"),
        "eval_status": (payload.get("eval") or {}).get("status"),
        "eval_notes": payload.get("eval_notes"),
        "port_check_ok": (payload.get("port_check") or {}).get("ok"),
        "llm_calls": (payload.get("llm_usage") or {}).get("llm_calls"),
        "cache_hits": (payload.get("llm_usage") or {}).get("cache_hits"),
    }
    print(json.dumps(summary, indent=2))
    return 0 if payload["status"] in {"passed", "failed"} else 1


if __name__ == "__main__":
    sys.exit(main())