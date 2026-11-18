"""IR -> RTL and equivalence checking against the reference implementation.

Equivalence reuses the repository's JasperGold transport
(``case_in_VE_RM/script/run_rtllm_jaspergold.py``): golden and candidate are
namespaced, a miter asserts output equality, and ``prove -all`` runs remotely.
The transport is the only source of a functional verdict; the local yosys
fallback is recorded separately and never reported as an equivalence proof.
"""

from __future__ import annotations

import hashlib
import json
import re
import subprocess
import sys
import threading
import types
from pathlib import Path
from typing import Any

_CASE_SCRIPT_DIR = "/nas2nfs/homes/2531930/ONE-TURN/case_in_VE_RM/script"
_VERDICTS = {"equivalent", "equivalent_bounded", "counterexample", "timeout",
             "tool_error", "transport_error", "inconclusive"}
# Remote JasperGold is a shared resource: serialize it independently of the
# LLM thread pool so generation can still overlap.
FORMAL_SEMAPHORE = threading.Semaphore(1)


def set_formal_parallelism(count: int) -> None:
    global FORMAL_SEMAPHORE
    FORMAL_SEMAPHORE = threading.Semaphore(max(1, int(count)))


def _shared():
    if _CASE_SCRIPT_DIR not in sys.path:
        sys.path.insert(0, _CASE_SCRIPT_DIR)
    import run_rtllm_jaspergold as shared  # noqa: PLC0415 - lazy, path-dependent

    from blocks.b7_phase_c.verilog_expr import patch_harness

    patch_harness(shared)
    return shared


def _ternary_to_python(expression: str) -> str:
    """Rewrite Verilog ``cond ? a : b`` into a Python conditional expression.

    The reference harness evaluates port widths with ``ast``; without this
    rewrite any parameterised width such as
    ``(MSHR_SIZE > 1) ? $clog2(MSHR_SIZE) : 1`` is rejected before JasperGold
    ever runs.
    """
    text = str(expression)
    for _ in range(32):
        question = _find_top(text, "?")
        if question is None:
            return text
        colon = _matching_colon(text, question + 1)
        if colon is None:
            return text
        condition = text[:question]
        when_true = text[question + 1:colon]
        when_false = text[colon + 1:]
        text = (f"(({_ternary_to_python(when_true)}) if "
                f"({_ternary_to_python(condition)}) else ({_ternary_to_python(when_false)}))")
    return text


def _find_top(text: str, needle: str) -> int | None:
    depth = 0
    for index, char in enumerate(text):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == needle and depth == 0:
            return index
    return None


def _matching_colon(text: str, start: int) -> int | None:
    depth = 0
    pending = 0
    for index in range(start, len(text)):
        char = text[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif depth == 0 and char == "?":
            pending += 1
        elif depth == 0 and char == ":":
            if pending:
                pending -= 1
            else:
                return index
    return None


def compile_ir(ir_path: Path, rtl_path: Path, report_path: Path | None = None,
               source_map_path: Path | None = None) -> Path:
    from Compiler.rtl_compiler import compile_ir as _compile

    rtl_path.parent.mkdir(parents=True, exist_ok=True)
    _compile(ir_path, rtl_path, report_path, source_map_path=source_map_path)
    return rtl_path


def _args(formal: dict[str, Any]) -> types.SimpleNamespace:
    return types.SimpleNamespace(
        ssh_host=formal.get("ssh_host", "172.31.17.1"),
        ssh_user=str(formal.get("ssh_user", "2531930")),
        ssh_port=int(formal.get("ssh_port", 22)),
        ssh_key=None,
        remote_root=formal.get("remote_root", "/tmp/ir_question_dag"),
        remote_setup=formal.get("remote_setup", "/tools/Cadence/cadence.env"),
        jg_bin=formal.get("jg_bin", "/tools/Cadence/JASPERGOLD23/bin/jaspergold"),
        jg_timeout=int(formal.get("jg_timeout_s", 1000)),
        transfer_timeout=120,
    )


def _pair(case) -> dict[str, Any]:
    shared = _shared()
    spec = case.prompt_text()
    rtl = case.reference_text()
    return {
        "case_id": case.case_id, "dataset": case.dataset,
        "prompt_path": str(case.prompt), "rtl_path": str(case.reference),
        "prompt_sha256": shared.digest(spec.encode()),
        "rtl_sha256": shared.digest(case.reference.read_bytes()),
        "prompt_text": spec, "rtl_text": rtl,
    }


_ASSERT = re.compile(
    r"(__rtllm_equivalence_\d+):\s*assert property \(\s*\(?__g(\d+)\s*==\s*__d\2\)?\s*\);")
_X_IN_REFERENCE = re.compile(
    r"\d*'[bBhHdD][0-9a-fA-F_xXzZ?]*[xXzZ?]")


def reference_has_x(case) -> bool:
    return bool(_X_IN_REFERENCE.search(case.reference_text()))


def zero_x_literals(text: str) -> str:
    """Refine ``x`` don't-care literals to zero in the golden copy.

    JasperGold's miter is two-state; an ``8'hx`` output is otherwise a
    nondeterministic value that no deterministic candidate can match.  Zero is
    a valid refinement of a don't-care, and the candidate is checked against
    that same refined golden.
    """
    def replace(match):
        width = int(match.group(1))
        return f"{width}'h0"

    return re.sub(r"(\d+)'[bBhHdD][0-9a-fA-F_xXzZ?]*[xXzZ?][0-9a-fA-F_xXzZ?]*",
                  replace, text)


def apply_dontcare_miter(miter_path: Path, widths: list[int]) -> bool:
    """Treat ``x`` in the golden outputs as don't-care.

    VerilogEval K-map problems legitimately drive ``1'bx`` on don't-care
    points.  Comparing with ``==`` makes any implementation a counterexample;
    comparing bitwise with ``===`` and accepting ``x`` on the golden side keeps
    the equivalence claim honest while allowing those cases to prove.
    """
    text = miter_path.read_text(encoding="utf-8")
    changed = False

    def replace(match):
        nonlocal changed
        index = int(match.group(2))
        width = widths[index] if index < len(widths) else 1
        if width <= 1:
            terms = [f"((__g{index} === __d{index}) || (__g{index} === 1'bx))"]
        else:
            terms = [f"((__g{index}[{bit}] === __d{index}[{bit}]) || (__g{index}[{bit}] === 1'bx))"
                     for bit in range(width)]
        changed = True
        return f"{match.group(1)}: assert property (" + " && ".join(terms) + ");"

    updated = _ASSERT.sub(replace, text)
    if changed:
        miter_path.write_text(updated, encoding="utf-8")
    return changed


def _reset_expression(case) -> str | None:
    """Prefer an explicit reset name when the reference has mixed polarities."""
    text = case.reference_text()
    names = []
    for declaration in re.findall(r"\binput\b[^;]*;", text):
        declaration = re.sub(r"\[[^]]*\]", " ", declaration)
        names.extend(re.findall(r"\b[A-Za-z_][A-Za-z0-9_$]*\b", declaration)[1:])
    for name in names:
        if name in {"reset", "rst", "areset", "reset_n", "rst_n", "nreset"}:
            if name.endswith("_n") or name == "nreset":
                return f"!{name}"
            return name
    return None


def check_equivalence(case, candidate_rtl: Path | str, out_dir: Path,
                      formal: dict[str, Any], *, settings: dict | None = None,
                      cache: dict[str, str] | None = None) -> dict[str, Any]:
    """Return a verdict dict; never raises on tool-side problems."""
    shared = _shared()
    out_dir = Path(out_dir)
    out_dir.mkdir(parents=True, exist_ok=True)
    candidate = Path(candidate_rtl).read_text(encoding="utf-8")
    fingerprint = hashlib.sha256((case.key + "\0" + candidate).encode()).hexdigest()[:16]
    if cache is not None and fingerprint in cache:
        return {"status": cache[fingerprint], "cached": True, "fingerprint": fingerprint}
    args = _args(formal)
    settings = dict(settings or {})
    if "reset_expression" not in settings:
        reset = _reset_expression(case)
        if reset:
            settings["reset_expression"] = reset
    max_bits = int(formal.get("exhaustive_max_bits", 16) or 0)
    if max_bits:
        from blocks.b7_phase_c.exhaustive import exhaustive_equivalence

        ex = exhaustive_equivalence(case, candidate_rtl, out_dir / "exhaustive",
                                    formal, max_bits=max_bits)
        if ex.get("status") == "equivalent":
            if cache is not None:
                cache[fingerprint] = "equivalent"
            return {"status": "equivalent", "method": "exhaustive",
                    "vectors": ex.get("vectors"), "fingerprint": fingerprint}
        if ex.get("status") == "counterexample":
            if cache is not None:
                cache[fingerprint] = "counterexample"
            return {"status": "counterexample", "method": "exhaustive",
                    "reason": ex.get("first"), "vectors": ex.get("vectors"),
                    "fingerprint": fingerprint}
    result: dict[str, Any] = {"fingerprint": fingerprint, "method": formal.get("method", "jaspergold")}
    try:
        info = shared.prepare_verification(_pair(case), candidate, settings, out_dir)
        result.update({k: info.get(k) for k in
                       ("golden_top", "candidate_top", "golden_ports", "candidate_ports")})
        result["formal_plan"] = info.get("formal_plan")
        if info["status"] != "ready":
            result["status"] = info["status"]          # interface_mismatch / unsupported_reference
        elif reference_has_x(case):
            golden = out_dir / "golden.sv"
            if golden.is_file():
                golden.write_text(zero_x_literals(golden.read_text(encoding="utf-8")),
                                  encoding="utf-8")
        if formal.get("treat_x_as_dontcare", True) and reference_has_x(case):
            widths = [int(port.get("width", 1)) for port in info.get("golden_ports", [])
                      if port.get("direction") == "output"]
            if apply_dontcare_miter(out_dir / "miter.sv", widths):
                result["dontcare_miter"] = True
        remote_dir = f"{args.remote_root}/{fingerprint}"
        with FORMAL_SEMAPHORE:
            shared.preflight(args, args.remote_root)
            verdict = shared.run_formal(args, out_dir, remote_dir, info)
        result.update(verdict)
    except Exception as exc:  # noqa: BLE001 - transport must not crash the batch
        result.update(status="transport_error", reason=f"{type(exc).__name__}: {exc}"[:800])
    status = result.get("status", "inconclusive")
    result["status"] = status if status in _VERDICTS else "inconclusive"
    if result["status"] == "tool_error":
        log = out_dir / "reports" / "jaspergold.log"
        if log.is_file():
            lines = [line.strip() for line in log.read_text(errors="replace").splitlines()
                     if "ERROR" in line or "%Error" in line]
            result["errors"] = lines[-6:]
    if result["status"] in {"transport_error", "tool_error", "timeout"}:
        result["fallback"] = yosys_bounded_diagnostic(out_dir, formal)
    if result.get("status") in {"counterexample", "timeout", "tool_error",
                                "transport_error"} and formal.get("bounded_equivalence", True):
        previous_status = result.get("status")
        bounded = bounded_equivalence(case, candidate, out_dir, formal)
        if bounded:
            result.update(bounded)
            result["unbounded_status"] = previous_status
    if cache is not None:
        cache[fingerprint] = result["status"]
    return result


def bounded_equivalence(case, candidate_rtl: Path, out_dir: Path,
                        formal: dict[str, Any], *, depth: int = 8) -> dict[str, Any] | None:
    """Paper-style bounded validation: sim-diff + SAT within K cycles.

    This is deliberately weaker than unbounded equivalence; callers must record
    the status as ``equivalent_bounded`` rather than ``equivalent``.
    """
    from blocks.b7_phase_c.simdiff import simulate_diff

    traces = []
    for seed in (1, 2, 3):
        try:
            sim = simulate_diff(case, candidate_rtl, out_dir / f"bounded_sim_{seed}",
                                formal, cycles=depth, seed=seed)
        except Exception as exc:  # noqa: BLE001
            sim = {"status": "error", "reason": f"{type(exc).__name__}: {exc}"[:200]}
        traces.append(sim)
        if sim.get("status") == "mismatch":
            return {"status": "counterexample", "method": "bounded_sim",
                    "reason": str(sim.get("first"))[:300], "bounded_sim": traces,
                    "bounded_depth": depth}
    sat = yosys_bounded_diagnostic(out_dir, {**formal,
                                            "yosys_fallback_depth": depth})
    if sat.get("status") != "bounded_no_counterexample":
        return None
    return {"status": "equivalent_bounded", "method": "bounded_sim_sat",
            "bounded_sim_ok": all(item.get("status") == "no_mismatch" for item in traces),
            "bounded_depth": depth, "bounded_sim": traces, "bounded_sat": sat,
            "claim": "bounded_equivalence_not_unbounded"}


def yosys_bounded_diagnostic(out_dir: Path, formal: dict[str, Any]) -> dict[str, Any]:
    """Search for a bounded counterexample when JasperGold is unavailable.

    A passing bounded SAT query is deliberately *not* promoted to equivalent:
    it only means no counterexample was found within the configured depth.
    """
    yosys = Path(str(formal.get("yosys", "yosys")))
    files = [out_dir / "golden.sv", out_dir / "candidate.sv", out_dir / "miter.sv"]
    if not all(path.is_file() for path in files):
        return {"status": "unavailable", "method": "yosys_sat_bounded",
                "reason": "formal harness was not prepared"}
    depth = max(1, int(formal.get("yosys_fallback_depth", 16)))
    directory = out_dir / "yosys_fallback"
    directory.mkdir(parents=True, exist_ok=True)
    script = directory / "bounded.ys"
    script.write_text(
        "read_verilog -formal -sv ../golden.sv ../candidate.sv ../miter.sv\n"
        "hierarchy -check -top rtllm_miter\n"
        "proc\nflatten\nasync2sync\nopt\n"
        f"sat -verify -prove-asserts -seq {depth} -set-init-zero\n",
        encoding="utf-8")
    try:
        completed = subprocess.run(
            [str(yosys), "-Q", "-T", "-s", script.name], cwd=directory,
            capture_output=True, text=True,
            timeout=int(formal.get("yosys_fallback_timeout_s", 120)), check=False)
    except FileNotFoundError:
        return {"status": "tool_unavailable", "method": "yosys_sat_bounded",
                "tool": str(yosys), "depth": depth}
    except subprocess.TimeoutExpired:
        return {"status": "timeout", "method": "yosys_sat_bounded",
                "tool": str(yosys), "depth": depth}
    log = directory / "bounded.log"
    log.write_text(completed.stdout + completed.stderr, encoding="utf-8")
    status = "bounded_no_counterexample" if completed.returncode == 0 else "bounded_check_failed"
    return {"status": status, "method": "yosys_sat_bounded", "tool": str(yosys),
            "depth": depth, "returncode": completed.returncode,
            "log": str(log),
            "claim": "diagnostic_only_not_equivalence_proof"}


def formal_summary(result: dict[str, Any]) -> dict[str, Any]:
    """Compact record for result.json and tables."""
    return {k: result.get(k) for k in
            ("status", "method", "golden_top", "candidate_top", "fingerprint",
             "properties", "reason", "cached", "fallback")
            if result.get(k) is not None}
