"""Verilator lint and testbench simulation integration."""

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path


class VerilatorValidationError(Exception):
    def __init__(self, message, report_path):
        self.report_path = str(report_path)
        super().__init__(f"{message}; report: {self.report_path}")


class SVACompileValidationError(VerilatorValidationError):
    """The generated SVA/instrumented RTL failed its pre-simulation check."""


def infer_testbench_top(testbench_paths):
    pattern = re.compile(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_$]*)")
    for path in testbench_paths:
        text = Path(path).read_text(encoding="utf-8", errors="replace")
        match = pattern.search(text)
        if match:
            return match.group(1)
    raise ValueError(
        "No module was found in the testbench; use --tb-top to specify the top module")


def _execute(command, cwd, timeout):
    started = time.monotonic()
    environment = os.environ.copy()
    # Some Verilator installations automatically wrap the compiler with
    # ccache.  Disabling its cache avoids writes outside generation/ and makes
    # the build reproducible in restricted environments.
    environment["CCACHE_DISABLE"] = "1"
    try:
        result = subprocess.run(
            command, cwd=str(cwd), text=True, stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT, timeout=timeout, check=False,
            env=environment)
        return result.returncode, result.stdout, time.monotonic() - started, None
    except subprocess.TimeoutExpired as exc:
        output = exc.stdout or ""
        if isinstance(output, bytes):
            output = output.decode("utf-8", errors="replace")
        return 124, output, time.monotonic() - started, f"timeout after {timeout}s"
    except FileNotFoundError:
        return 127, "verilator executable not found\n", 0.0, "verilator not found"


def _version():
    executable = shutil.which("verilator")
    if not executable:
        return None
    result = subprocess.run(
        [executable, "--version"], text=True, stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT, check=False)
    return result.stdout.strip()


def _write_report(path, report):
    Path(path).write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def _assertion_failures(output):
    return [line.strip() for line in output.splitlines()
            if re.search(
                r"(?i)SVA_FAIL|assertion failed|assert_[A-Za-z0-9_$]+",
                line)]


def _testbench_failure_markers(output):
    """Return explicit failure lines even when a TB exits through $finish."""
    markers = []
    generic = re.compile(
        r"(?i)(?:\btest\s+failed\b|={3,}\s*(?:failed|error)|\bfatal\b)")
    counted = re.compile(
        r"(?i)\btest\s+completed\s+with\s+([0-9]+)\s*/\s*"
        r"([0-9]+)\s+failures?\b")
    for line in output.splitlines():
        stripped = line.strip()
        match = counted.search(line)
        if generic.search(line) or (match and int(match.group(1)) > 0):
            if stripped and stripped not in markers:
                markers.append(stripped)
    return markers


def run_verilator(rtl_path, generation_dir, *, testbenches=None,
                  extra_sources=None, tb_top=None, timeout=120,
                  lint_only=False, assertions=False, coverage=False,
                  artifact_prefix="verilator", raise_on_error=True,
                  base_rtl_path=None, sva_path=None, report_output=None,
                  compile_log_output=None, simulation_log_output=None):
    """Lint generated RTL or build and execute a Verilator testbench."""
    rtl = Path(rtl_path).resolve()
    output_dir = Path(generation_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    report_path = Path(
        report_output or output_dir / f"{artifact_prefix}_report.json").resolve()
    compile_log = Path(
        compile_log_output or output_dir / f"{artifact_prefix}_compile.log").resolve()
    simulation_log = Path(
        simulation_log_output or
        output_dir / f"{artifact_prefix}_simulation.log").resolve()
    testbenches = [Path(path).resolve() for path in (testbenches or [])]
    extra_sources = [Path(path).resolve() for path in (extra_sources or [])]
    for source in [rtl] + testbenches + extra_sources:
        if not source.is_file():
            raise ValueError(f"Verilator source does not exist: {source}")

    mode = "lint" if lint_only or not testbenches else "simulation"
    workdir = testbenches[0].parent if testbenches else rtl.parent
    requested_build_dir = output_dir / f"{artifact_prefix}_build"
    build_dir = requested_build_dir
    temporary_build_dir = None
    if re.search(r"\s", str(requested_build_dir)):
        temporary_build_dir = Path(tempfile.mkdtemp(
            prefix="spec2rtl_verilator_"))
        build_dir = temporary_build_dir
    common = ["verilator", "--language", "1800-2017", "--Wno-fatal"]
    if assertions:
        common.append("--assert")
    if coverage:
        common.append("--coverage-user")
    sources = [str(rtl)] + [str(path) for path in extra_sources]
    report = {
        "tool": "verilator",
        "version": _version(),
        "status": "running",
        "mode": mode,
        "started_at": datetime.now(timezone.utc).isoformat(),
        "rtl": str(rtl),
        "base_rtl": (str(Path(base_rtl_path).resolve())
                     if base_rtl_path else str(rtl)),
        "sva": str(Path(sva_path).resolve()) if sva_path else None,
        "testbenches": [str(path) for path in testbenches],
        "extra_sources": [str(path) for path in extra_sources],
        "testbench_top": None,
        "assertions_enabled": assertions,
        "coverage_enabled": coverage,
        "working_directory": str(workdir),
        "build_directory": str(requested_build_dir) if mode == "simulation" else None,
        "temporary_build_directory": (str(temporary_build_dir)
                                      if mode == "simulation" and temporary_build_dir else None),
        "compile_log": str(compile_log),
        "simulation_log": str(simulation_log) if mode == "simulation" else None,
    }

    if mode == "lint":
        command = common + ["--lint-only"] + sources
        code, output, duration, execution_error = _execute(command, workdir, timeout)
        compile_log.write_text(output, encoding="utf-8")
        report.update({
            "command": command, "compile_returncode": code,
            "compile_duration_seconds": round(duration, 3),
            "status": "passed" if code == 0 else "failed",
            "error": execution_error,
        })
        _write_report(report_path, report)
        if code != 0 and raise_on_error:
            raise VerilatorValidationError("Verilator lint failed", report_path)
        return report_path, report

    top = tb_top or infer_testbench_top(testbenches)
    report["testbench_top"] = top
    requested_build_dir.mkdir(parents=True, exist_ok=True)
    build_dir.mkdir(parents=True, exist_ok=True)
    staged_sources = []
    original_sources = [rtl] + extra_sources + testbenches
    for index, source in enumerate(original_sources):
        suffix = source.suffix if source.suffix else ".v"
        staged = build_dir / f"source_{index}{suffix}"
        shutil.copyfile(str(source), str(staged))
        staged_sources.append(staged)
    staged_rtl = staged_sources[0]
    staged_extras = staged_sources[1:1 + len(extra_sources)]
    staged_testbenches = staged_sources[1 + len(extra_sources):]

    def build_source_argument(path):
        relative = os.path.relpath(str(path), str(workdir))
        return relative if not re.search(r"\s", relative) else str(path)

    executable_name = f"{artifact_prefix}_sim"
    built_executable = build_dir / executable_name
    executable = requested_build_dir / executable_name
    build_dir_argument = str(build_dir)
    relative_build_dir = os.path.relpath(str(build_dir), str(workdir))
    if not re.search(r"\s", relative_build_dir):
        # Verilator embeds --Mdir in an internal `make -C` command.  Some
        # versions lose quoting for absolute paths containing whitespace.
        build_dir_argument = relative_build_dir
    build_command = common + [
        "--binary", "--timing", "--top-module", top,
        "--Mdir", build_dir_argument, "-o", executable_name,
    ] + [build_source_argument(staged_rtl)] + [
        build_source_argument(path) for path in staged_extras
    ] + [build_source_argument(path) for path in staged_testbenches]
    code, output, duration, execution_error = _execute(
        build_command, workdir, timeout)
    compile_log.write_text(output, encoding="utf-8")
    report.update({
        "build_command": build_command, "compile_returncode": code,
        "compile_duration_seconds": round(duration, 3),
        "executable": str(executable), "error": execution_error,
    })
    if code != 0:
        report["status"] = "failed"
        _write_report(report_path, report)
        if temporary_build_dir:
            shutil.rmtree(str(temporary_build_dir), ignore_errors=True)
        if raise_on_error:
            raise VerilatorValidationError("Verilator testbench build failed", report_path)
        return report_path, report

    if built_executable != executable:
        shutil.copy2(str(built_executable), str(executable))
        shutil.rmtree(str(temporary_build_dir), ignore_errors=True)

    run_code, run_output, run_duration, run_error = _execute(
        [str(executable)], workdir, timeout)
    simulation_log.write_text(run_output, encoding="utf-8")
    failure_markers = _testbench_failure_markers(run_output)
    failure_text = bool(failure_markers)
    passed = run_code == 0 and not failure_text
    assertion_failures = _assertion_failures(run_output)
    report.update({
        "run_command": [str(executable)], "simulation_returncode": run_code,
        "simulation_duration_seconds": round(run_duration, 3),
        "failure_marker_detected": failure_text,
        "failure_markers": failure_markers,
        "assertion_failure_count": len(assertion_failures),
        "assertion_failures": assertion_failures,
        "status": "passed" if passed else "failed",
        "error": run_error,
    })
    _write_report(report_path, report)
    if not passed and raise_on_error:
        raise VerilatorValidationError("Verilator simulation failed", report_path)
    return report_path, report
