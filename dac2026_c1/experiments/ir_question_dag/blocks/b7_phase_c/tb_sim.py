"""Verilator testbench simulation for saved-testbench cases."""

from __future__ import annotations

import json
import os
import re
import subprocess
from pathlib import Path
from typing import Any

VERILATOR = Path("/home/2531930/nas/2531930-2/eda_tools/conda/bin/verilator")
_MODULE = re.compile(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)")


def _markers(output: str) -> list[str]:
    found = []
    generic = re.compile(r"(?i)(?:\btest\s+failed\b|={3,}\s*(?:failed|error)|\bfatal\b)")
    counted = re.compile(r"(?i)\btest\s+completed\s+with\s+([0-9]+)\s*/\s*([0-9]+)\s+failures?\b")
    for line in output.splitlines():
        stripped = line.strip()
        if "%Warning" in line or "Warning-" in line:
            continue
        match = counted.search(line)
        mism = re.search(r"mismatches:\s*(\d+)\s+in\s+(\d+)\s+samples", line, re.I)
        if (generic.search(line) or re.search(r"={3,}\s*error\b", line, re.I)
                or re.match(r"^(?:error|failed|failure|mismatch)\b", stripped, re.I)
                or (mism and int(mism.group(1)) > 0)
                or (match and int(match.group(1)) > 0)):
            if stripped and stripped not in found:
                found.append(stripped)
    return found


def run_testbench(rtl_path: Path, testbench: Path, out_dir: Path,
                  *, timeout: int = 300) -> dict[str, Any]:
    out_dir = Path(out_dir).resolve()
    rtl_path = Path(rtl_path).resolve()
    testbench = Path(testbench).resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    build = out_dir / "build"
    build.mkdir(parents=True, exist_ok=True)
    text = Path(testbench).read_text(encoding="utf-8", errors="replace")
    rtl_text = Path(rtl_path).read_text(encoding="utf-8", errors="replace")
    dut_modules = set(_MODULE.findall(rtl_text))
    # Prefer the testbench module that actually instantiates the DUT.  This
    # matters for VerilogEval, whose file starts with `module stimulus_gen`.
    tb_blocks = list(re.finditer(r"(?m)^\s*module\s+([A-Za-z_]\w*)", text))
    top = None
    for i, block in enumerate(tb_blocks):
        end = tb_blocks[i + 1].start() if i + 1 < len(tb_blocks) else len(text)
        body = text[block.start():end]
        if any(re.search(r"\b" + re.escape(m) + r"\s+[A-Za-z_]\w*\s*\(",
                         body) for m in dut_modules):
            top = block.group(1)
            break
    if top is None:
        match = _MODULE.search(text)
        if not match:
            return {"status": "failed", "reason": "testbench module not found"}
        top = match.group(1)
    env = os.environ.copy()
    env["CCACHE_DISABLE"] = "1"
    conda_bin = "/home/2531930/nas/2531930-2/eda_tools/conda/bin"
    env["PATH"] = conda_bin + os.pathsep + env.get("PATH", "")
    executable = out_dir / "sim"
    sources = [str(rtl_path)]
    # VerilogEval testbenches instantiate both TopModule and RefModule; the
    # reference is staged next to the generated RTL by run_case.
    if "RefModule" in text:
        for name in ("reference.sv", "reference.v"):
            candidate = rtl_path.parent / name
            if candidate.is_file() and candidate.resolve() != rtl_path.resolve():
                sources.append(str(candidate))
                break
    command = [str(VERILATOR), "--language", "1800-2017", "--Wno-fatal",
               "--binary", "--timing", "--top-module", top,
               "--Mdir", str(build), "-o", str(executable),
               *sources, str(testbench)]
    try:
        compiled = subprocess.run(command, cwd=str(out_dir), text=True,
                                  stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                                  timeout=timeout, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = {"status": "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "failed",
                  "stage": "compile",
                  "reason": f"{type(exc).__name__}: {exc}"[:500]}
        (out_dir / "report.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    (out_dir / "compile.log").write_text(compiled.stdout, encoding="utf-8")
    if compiled.returncode != 0:
        errors = [line.strip() for line in compiled.stdout.splitlines()
                  if "%Error" in line]
        result = {"status": "failed", "stage": "compile", "top": top,
                  "returncode": compiled.returncode,
                  "errors": errors[:12],
                  "log_tail": compiled.stdout[-6000:],
                  "reason": (" | ".join(errors[:6]) if errors
                             else compiled.stdout[-2000:])}
        (out_dir / "report.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    try:
        ran = subprocess.run([str(executable)], cwd=str(out_dir), text=True,
                             stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             timeout=timeout, env=env, check=False)
    except (OSError, subprocess.TimeoutExpired) as exc:
        result = {"status": "timeout" if isinstance(exc, subprocess.TimeoutExpired) else "failed",
                  "stage": "run", "top": top,
                  "reason": f"{type(exc).__name__}: {exc}"[:500]}
        (out_dir / "report.json").write_text(json.dumps(result, indent=2) + "\n")
        return result
    (out_dir / "simulation.log").write_text(ran.stdout, encoding="utf-8")
    failure_markers = _markers(ran.stdout)
    detail_errors = [line.strip() for line in ran.stdout.splitlines()
                     if "ERROR" in line or "MISMATCH" in line]
    passed = ran.returncode == 0 and not failure_markers
    result = {"status": "passed" if passed else "failed", "stage": "run",
              "top": top, "returncode": ran.returncode,
              "failure_markers": failure_markers,
              "errors": detail_errors[:12],
              "log_tail": ran.stdout[-6000:],
              "log": str(out_dir / "simulation.log"),
              "reason": (" | ".join((detail_errors + failure_markers)[:6])[:2000]
                         if (detail_errors or failure_markers) else None)}
    (out_dir / "report.json").write_text(json.dumps(result, indent=2) + "\n")
    return result


def testbench_for_case(case, config: dict | None = None) -> Path | None:
    """Resolve a saved testbench from config/local files, not a dataset name."""
    simulation = (config or {}).get("simulation") or {}
    name = str(simulation.get("testbench_name", "testbench.sv"))
    names = [name]
    for alt in ("testbench.sv", "testbench.v"):
        if alt not in names:
            names.append(alt)
    bases = [Path(getattr(case, "reference")).parent]
    root = simulation.get("testbench_root")
    if root:
        root = Path(str(root))
        bases.extend([root / str(case.short), root / str(case.key)])
    for base in bases:
        for tb_name in names:
            path = base / tb_name
            if path.is_file():
                return path
    return None



