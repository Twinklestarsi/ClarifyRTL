#!/usr/bin/env python3
"""Run the complete Spec -> HW-IR/Property IR -> Verilog/SVA pipeline."""
# Usage: python main.py spec --repair
#        python main.py spec --hls-re   # existing IR -> HLS C++, no LLM

import argparse
import json
import os
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

from IRgeneration.checker1 import Checker1Error, check_ir
from IRgeneration.checker2 import Checker2Error, check_ir as check_ir_semantics
from Compiler.hls_generator import HLSGenerationError, generate_hls_c
from iteration.hw_ir_repair import build_repair_context, repair_hw_ir
from IRgeneration.ir_generate.gen_petrinet_ir import generate_ir
from Compiler.llm_rtl_generator import generate_rtl_from_ir
from sva_generation.property_ir import (PropertyIRCheckerError, check_property_ir,
                         generate_property_ir, generate_spec_property_ir)
from Compiler.rtl_compiler import CompilerError, compile_ir
from sva_generation.sva_generator import (COMPATIBILITY_PROFILES, SVAInstrumentationError,
                           SVALoweringError, generate_sva,
                           instrument_rtl_with_sva)
from iteration.verilator_runner import (SVACompileValidationError,
                              VerilatorValidationError, run_verilator)


def _arguments():
    parser = argparse.ArgumentParser(
        description="Generate Hardware-aware Petri Net IR and compile it to Verilog")
    parser.add_argument("spec", nargs="?", help="hardware specification file")
    parser.add_argument(
        "-spec", "--spec", dest="spec_option", metavar="SPEC",
        help="hardware specification file (same as the positional SPEC)")
    parser.add_argument(
        "--ir", help="compile an existing YAML/JSON IR instead of calling the LLM")
    parser.add_argument(
        "--generation-dir",
        help="artifact directory (default: generation/ beside the spec)")
    parser.add_argument("--model", default=os.environ.get("OPENAI_MODEL"))
    parser.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL"))
    parser.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"))
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=32000)
    parser.add_argument(
        "--repair", action="store_true", help="ask the front-end LLM to repair malformed YAML once")
    parser.add_argument(
        "--testbench", action="append", default=[],
        help="Verilator testbench source; repeat for multiple files. Default: auto-detect beside spec")
    parser.add_argument(
        "--extra-source", action="append", default=[],
        help="additional RTL source for Verilator; repeat for multiple files")
    parser.add_argument("--tb-top", help="Verilator testbench top module")
    parser.add_argument(
        "--verilator-timeout", type=int, default=120,
        help="timeout in seconds for Verilator build and simulation (default: 120)")
    parser.add_argument(
        "--verilator-lint-only", action="store_true",
        help="run lint only even when a testbench is available")
    parser.add_argument(
        "--no-verilator", action="store_true",
        help="skip Verilator validation")
    parser.add_argument(
        "--hls", action="store_true",
        help=("generate Vitis-style HLS C++ after checker1/checker2; skip RTL, "
              "Verilator, Property IR, and SVA"))
    parser.add_argument(
        "--hls-re", dest="hls_re", action="store_true",
        help=("regenerate Vitis-style HLS C++ from a completed run's existing "
              "IR without calling the LLM; leaves RTL/SVA/Verilator artifacts "
              "unchanged"))
    parser.add_argument(
        "--no-compiler", "--no_compiler", dest="no_compiler",
        action="store_true",
        help=("use an LLM to generate RTL from checked HW-IR instead of the "
              "deterministic compiler"))
    parser.add_argument(
        "--no-sva", "--no_sva", dest="no_sva", action="store_true",
        help=("disable Property IR/SVA generation and RTL+SVA diagnostics; "
              "RTL-only failures are still eligible for HW-IR repair"))
    parser.add_argument(
        "--max-sva-compile-repair-iterations", type=int, default=3,
        help="maximum deterministic SVA compatibility repairs (0-3)")
    parser.add_argument(
        "--no-sva-compile-repair", action="store_true",
        help="disable the SVA-only compile repair loop")
    parser.add_argument(
        "--max-hw-ir-repair-iterations", "--max-sva-repair-iterations",
        dest="max_hw_ir_repair_iterations", type=int, default=3,
        help="maximum LLM HW-IR repairs after checker/pipeline/simulation failures (0-3)")
    parser.add_argument(
        "--no-hw-ir-repair", "--no-sva-repair",
        dest="no_hw_ir_repair", action="store_true",
        help="disable LLM HW-IR repair after deterministic repair fails")
    parser.add_argument("--pre-ir-analysis", action="store_true", help="enable optional clear/load pre-IR clarification DAG")
    parser.add_argument("--analyze-spec-only", action="store_true", help="save pre-IR DAG/contract without calling IR generation")
    parser.add_argument("--skip-pre-ir-analysis", action="store_true", help="disable optional pre-IR analysis")
    parser.add_argument("--clarifications", help="version-matched JSON answers for pre-IR analysis")
    parser.add_argument("--no-assumption-analysis", action="store_true", help="disable Contribution 1 analysis")
    parser.add_argument("--c1-bounded", action="store_true", help="run post-validation bounded impact queries and directed RTL replay")
    parser.add_argument("--c1-horizon", type=int, default=16)
    parser.add_argument("--c1-solver-timeout-ms", type=int, default=2000)
    parser.add_argument("--c1-output-protocol", help="JSON with sourced always_valid outputs or valid/payload channels")
    parser.add_argument("--c1-properties", help="existing Property IR available before prediction; validated against current IR")
    parser.add_argument("--c1-requirements", help="reviewed structured requirements JSON matching the visible Spec hash")
    parser.add_argument("--c1-evidence", help="version-matched user/document evidence JSON")
    parser.add_argument("--c1-max-alternatives", type=int, default=2, help="fixed executable alternative budget per assumption")
    parser.add_argument("--c1-calibrator", help="optional calibration file matching checkpoint/data scope")
    parser.add_argument("--c1-checkpoint", help="optional trained Contribution 1 NumPy checkpoint")
    parser.add_argument("--c1-encoder-path", help="frozen local semantic encoder used by checkpoint")
    parser.add_argument("--c1-llm", action="store_true", help="use frozen LLM structured requirements/candidates with binding and tool validation")
    args = parser.parse_args()
    # ``-spec``/``--spec`` shares the positional's meaning but cannot share its
    # ``dest``: a ``nargs="?"`` positional always rewrites its own default.
    if args.spec_option is not None:
        args.spec = args.spec_option
    return args


def _artifact_directory(args):
    if args.generation_dir:
        return Path(args.generation_dir).resolve()
    anchor = Path(args.spec or args.ir).resolve()
    return anchor.parent / "generation"


def _testbenches(args):
    if args.testbench:
        return [Path(path).resolve() for path in args.testbench]
    anchor = Path(args.spec or args.ir).resolve().parent
    for name in ("testbench.sv", "testbench.v", "tb.sv", "tb.v"):
        candidate = anchor / name
        if candidate.is_file():
            return [candidate]
    return []


def _effective_model(args):
    """Return the model name a downstream LLM call will actually use."""
    return args.model or os.environ.get("OPENAI_MODEL")


def _print_verilator_result(report, report_path, *, stream=None,
                            heading="VERILATOR VALIDATION RESULT"):
    """Print a stable, immediately visible terminal validation summary."""
    stream = stream or sys.stdout
    status = str(report.get("status", "failed")).upper()
    mode = str(report.get("mode") or "disabled").upper()
    print("\n" + "=" * 58, file=stream)
    print(heading, file=stream)
    print(f"  Result : {status}", file=stream)
    print(f"  Mode   : {mode}", file=stream)
    if report.get("testbench_top"):
        print(f"  TB top : {report['testbench_top']}", file=stream)
    if report.get("compile_returncode") is not None:
        print(f"  Compile: exit {report['compile_returncode']}", file=stream)
    if report.get("simulation_returncode") is not None:
        print(f"  Sim    : exit {report['simulation_returncode']}", file=stream)
    print(f"  Report : {report_path}", file=stream)
    if report.get("compile_log"):
        print(f"  Build log: {report['compile_log']}", file=stream)
    if (report.get("simulation_log") and
            report.get("simulation_returncode") is not None):
        print(f"  Sim log  : {report['simulation_log']}", file=stream)
    print("=" * 58 + "\n", file=stream)


class _AttemptStageError(RuntimeError):
    def __init__(self, stage, cause):
        self.stage = stage
        self.cause = cause
        super().__init__(f"{stage}: {cause}")


def _paths(generation_dir):
    return {
        "ir": generation_dir / "ir.yaml",
        "rtl": generation_dir / "generated_rtl.v",
        "hls": generation_dir / "generated_hls.cpp",
        "hls_report": generation_dir / "hls_generation_report.json",
        "instrumented_rtl": generation_dir / "generated_rtl_with_sva.sv",
        "compiler_report": generation_dir / "compiler_report.json",
        "rtl_llm_prompt": generation_dir / "rtl_llm_prompt.json",
        "rtl_llm_raw_response": generation_dir / "rtl_llm_raw_response.txt",
        "rtl_llm_response": generation_dir / "rtl_llm_response.json",
        "checker1_report": generation_dir / "checker1_report.json",
        "checker2_report": generation_dir / "checker2_report.json",
        "property_ir": generation_dir / "temporal_property_ir.yaml",
        "property_ir_prompt": generation_dir / "property_ir_prompt.json",
        "property_ir_raw_response": generation_dir / "property_ir_raw_response.txt",
        "property_ir_response": generation_dir / "property_ir_response.json",
        "property_ir_checker_input": (
            generation_dir / "temporal_property_ir.property_checker_input.yaml"),
        "property_checker_report": generation_dir / "property_ir_checker_report.json",
        "sva": generation_dir / "generated_properties.sva",
        "sva_report": generation_dir / "sva_generation_report.json",
        "sva_compile_report": generation_dir / "sva_compile_report.json",
        "sva_compile_log": generation_dir / "sva_compile.log",
        "sva_compile_feedback": generation_dir / "sva_compile_feedback.json",
        "sva_compile_repair_loop_report": (
            generation_dir / "sva_compile_repair_loop_report.json"),
        "rtl_verilator_report": generation_dir / "rtl_verilator_report.json",
        "rtl_verilator_compile_log": (
            generation_dir / "rtl_verilator_compile.log"),
        "rtl_verilator_simulation_log": (
            generation_dir / "rtl_verilator_simulation.log"),
        "verilator_report": generation_dir / "verilator_report.json",
        "verilator_compile_log": generation_dir / "verilator_compile.log",
        "verilator_simulation_log": generation_dir / "verilator_simulation.log",
        "repair_loop_report": generation_dir / "hw_ir_repair_loop_report.json",
        "legacy_repair_loop_report": generation_dir / "sva_repair_loop_report.json",
    }


def _write_json(path, value):
    Path(path).write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")


def _write_loop_report(paths, value):
    _write_json(paths["repair_loop_report"], value)
    # Preserve the artifact name used by the original SVA-only repair loop.
    _write_json(paths["legacy_repair_loop_report"], value)


def _clear_attempt_outputs(paths, *, preserve_property_ir=False):
    """Prevent artifacts from an older attempt entering current feedback."""
    for key in (
            "checker1_report", "checker2_report",
            "property_checker_report", "sva", "sva_report", "rtl",
            "hls", "hls_report",
            "instrumented_rtl", "compiler_report", "rtl_llm_prompt",
            "rtl_llm_raw_response", "rtl_llm_response", "sva_compile_report",
            "sva_compile_log", "sva_compile_feedback", "verilator_report",
            "verilator_compile_log", "verilator_simulation_log",
            "rtl_verilator_report", "rtl_verilator_compile_log",
            "rtl_verilator_simulation_log"):
        path = paths[key]
        if path.is_file():
            path.unlink()
    if not preserve_property_ir:
        for key in ("property_ir", "property_ir_checker_input"):
            if paths[key].is_file():
                paths[key].unlink()


def _mechanical_repair_summary(paths, stage):
    summaries = []
    candidates = []
    if stage != "checker1":
        candidates.append(("checker1", paths["checker1_report"]))
    candidates.append(("checker2" if stage != "checker1" else "checker1",
                       paths["checker2_report"] if stage != "checker1"
                       else paths["checker1_report"]))
    if stage not in {"checker1", "checker2", "property_ir_generation"}:
        candidates.append(
            ("property_ir_checker", paths["property_checker_report"]))
    seen = set()
    for checker, path in candidates:
        if path in seen or not path.is_file():
            continue
        seen.add(path)
        try:
            report = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            continue
        if checker == "checker1":
            summaries.append({
                "checker": checker,
                "status": report.get("status"),
                "repair_count": report.get("repair_count", 0),
                "repairs_applied": report.get("repairs_applied", False),
                "repairs": report.get("repairs", []),
                "unresolved_errors": report.get("errors", []),
            })
        else:
            summaries.append({
                "checker": checker,
                "status": report.get("status"),
                "repair_count": report.get("normalization_count", 0),
                "repairs_applied": report.get(
                    "normalizations_applied", False),
                "repairs": report.get("normalizations", []),
                "unresolved_errors": report.get("errors", []),
            })
    return summaries


def _sva_compile_feedback(paths, report):
    """Persist complete SVA-generator feedback without blaming HW-IR."""
    def read(path):
        source = Path(path)
        return (source.read_text(encoding="utf-8", errors="replace")
                if source.is_file() else f"<missing file: {source}>")

    return {
        "classification": report.get("classification", "sva_compile_failure"),
        "feedback_target": "sva_generator",
        "hw_ir_repair_eligible": False,
        "verilator_report": report,
        "property_ir_path": str(paths["property_ir"]),
        "property_ir": read(paths["property_ir"]),
        "generated_sva_path": str(paths["sva"]),
        "generated_sva": read(paths["sva"]),
        "instrumented_rtl_path": str(paths["instrumented_rtl"]),
        "instrumented_rtl": read(paths["instrumented_rtl"]),
        "sva_generation_report": read(paths["sva_report"]),
        "compile_log_path": str(paths["sva_compile_log"]),
        "compile_log": read(paths["sva_compile_log"]),
    }


def _write_sva_compile_feedback(paths, report, output_path=None):
    feedback = _sva_compile_feedback(paths, report)
    _write_json(output_path or paths["sva_compile_feedback"], feedback)
    return feedback


def _decorate_sva_compile_report(report):
    report.update({
        "classification": ("sva_compile_check" if
                           report["status"] == "passed" else
                           "sva_compile_failure"),
        "feedback_target": "sva_generator",
        "hw_ir_repair_eligible": False,
    })
    return report


def _compile_sva(args, generation_dir, paths, extra_sources, sva_report):
    _, report = run_verilator(
        paths["instrumented_rtl"], generation_dir,
        testbenches=[], extra_sources=extra_sources,
        timeout=args.verilator_timeout, lint_only=True, assertions=True,
        coverage=sva_report["cover_count"] > 0,
        artifact_prefix="sva_compile", raise_on_error=False,
        base_rtl_path=paths["rtl"], sva_path=paths["sva"],
        report_output=paths["sva_compile_report"],
        compile_log_output=paths["sva_compile_log"])
    _decorate_sva_compile_report(report)
    _write_json(paths["sva_compile_report"], report)
    return report


def _snapshot_sva_repair_artifacts(paths, repair_dir, prefix):
    names = {
        "sva": "generated_properties.sva",
        "sva_report": "sva_generation_report.json",
        "instrumented_rtl": "generated_rtl_with_sva.sv",
        "sva_compile_report": "sva_compile_report.json",
        "sva_compile_log": "sva_compile.log",
    }
    for key, name in names.items():
        source = paths[key]
        if source.is_file():
            shutil.copyfile(str(source), str(repair_dir / f"{prefix}_{name}"))


def _repair_sva_compile(args, generation_dir, paths, extra_sources,
                        sva_report, initial_report):
    """Apply up to three cumulative, semantics-preserving SVA profiles."""
    maximum = getattr(args, "max_sva_compile_repair_iterations", 3)
    if getattr(args, "no_sva_compile_repair", False):
        maximum = 0
    if maximum < 0 or maximum > 3:
        raise ValueError(
            "--max-sva-compile-repair-iterations must be between 0 and 3")
    used = getattr(args, "_sva_compile_repairs_used", 0)
    history = getattr(args, "_sva_compile_repair_history", [])
    current_profile = getattr(args, "_sva_compatibility_profile", "default")
    report = initial_report

    while report["status"] != "passed" and used < maximum:
        current_index = COMPATIBILITY_PROFILES.index(current_profile)
        if current_index + 1 >= len(COMPATIBILITY_PROFILES):
            break
        profile = COMPATIBILITY_PROFILES[current_index + 1]
        repair_number = used + 1
        repair_dir = (generation_dir / "sva_repair_iterations" /
                      f"iteration_{repair_number}")
        repair_dir.mkdir(parents=True, exist_ok=True)
        _snapshot_sva_repair_artifacts(paths, repair_dir, "input")
        _write_sva_compile_feedback(
            paths, report, repair_dir / "input_feedback_context.json")
        print(f"[SVA repair {repair_number}/{maximum}] "
              f"Applying compatibility profile {profile}: {repair_dir}")

        _, _, sva_report = generate_sva(
            paths["ir"], paths["property_ir"], paths["sva"],
            paths["sva_report"], compatibility_profile=profile)
        instrument_rtl_with_sva(
            paths["rtl"], paths["sva"], paths["instrumented_rtl"])
        report = _compile_sva(
            args, generation_dir, paths, extra_sources, sva_report)
        _snapshot_sva_repair_artifacts(paths, repair_dir, "candidate")
        feedback = _write_sva_compile_feedback(
            paths, report, repair_dir / "feedback_context.json")
        record = {
            "iteration": repair_number,
            "input_profile": current_profile,
            "candidate_profile": profile,
            "status": report["status"],
            "compile_returncode": report.get("compile_returncode"),
            "repair_directory": str(repair_dir),
            "feedback": str(repair_dir / "feedback_context.json"),
            "input_feedback": str(
                repair_dir / "input_feedback_context.json"),
            "classification": feedback["classification"],
        }
        _write_json(repair_dir / "repair_result.json", record)
        history.append(record)
        used += 1
        current_profile = profile
        args._sva_compile_repairs_used = used
        args._sva_compile_repair_history = history
        args._sva_compatibility_profile = profile
        _write_json(paths["sva_compile_repair_loop_report"], {
            "status": "passed" if report["status"] == "passed" else "repairing",
            "max_repair_iterations": maximum,
            "repairs_used": used,
            "active_profile": current_profile,
            "attempts": history,
        })

    final_status = "passed" if report["status"] == "passed" else "exhausted"
    _write_json(paths["sva_compile_repair_loop_report"], {
        "status": final_status,
        "max_repair_iterations": maximum,
        "repairs_used": used,
        "active_profile": current_profile,
        "attempts": history,
    })
    if report["status"] == "passed" and paths["sva_compile_feedback"].is_file():
        paths["sva_compile_feedback"].unlink()
    elif report["status"] != "passed":
        _write_sva_compile_feedback(paths, report)
    return report, sva_report


def _run_attempt_impl(args, generation_dir, paths, testbenches, extra_sources,
                 attempt_number):
    step_total = 4 if getattr(args, "hls", False) else 10
    sva_disabled = bool(getattr(args, "no_sva", False))
    fixed_spec_property_ir = bool(args.spec and paths["property_ir"].is_file())
    _clear_attempt_outputs(
        paths, preserve_property_ir=fixed_spec_property_ir)
    label = f"attempt {attempt_number}"
    stage = "checker1"
    try:
        print(f"[2/{step_total}][{label}] checker1 IR format check: "
              f"{paths['checker1_report']}")
        checker1_report = check_ir(paths["ir"], paths["checker1_report"])
        stage = "checker2"
        print(f"[3/{step_total}][{label}] checker2 hardware semantics check: "
              f"{paths['checker2_report']}")
        checker2_report = check_ir_semantics(
            paths["ir"], paths["checker2_report"])
        if getattr(args, '_generation_contract', None):
            from assumption_modeling.conformance import check_conformance
            from Compiler.rtl_compiler.parser import parse_ir
            from assumption_modeling.schema import write_json
            conformance = check_conformance(parse_ir(paths['ir']), args._generation_contract)
            write_json(args._c1_attempt_dir / 'ir_conformance.json', conformance)
            if conformance['status'] != 'conformant':
                stage = 'ir_contract_conformance'
                raise ValueError('Generation contract could not be verified: ' + json.dumps(conformance))
        if getattr(args, "hls", False):
            stage = "hls_generation"
            print(f"[4/4][{label}] Generating HLS C++: {paths['hls']}")
            _, hls_report = generate_hls_c(
                paths["ir"], paths["hls"], paths["hls_report"])
            skipped = {
                "tool": "verilator", "status": "skipped", "mode": "hls",
                "reason": "HLS output mode generates C++ only",
                "assertions_enabled": False, "rtl": None, "sva": None,
            }
            _write_json(paths["rtl_verilator_report"], skipped)
            _write_json(paths["verilator_report"], skipped)
            return {
                "checker1": checker1_report,
                "checker2": checker2_report,
                "property_checker": None,
                "sva": None,
                "compiler": hls_report,
                "hls": hls_report,
                "sva_compile": {
                    "status": "not_run", "mode": "hls",
                    "classification": "sva_disabled_in_hls_mode",
                    "reason": "HLS output mode does not generate RTL/SVA",
                },
                "rtl_verilator": skipped,
                "verilator": skipped,
                "sva_activated": False,
                "sva_activation_reason": "hls_output_mode",
            }
        stage = "rtl_compilation"
        if getattr(args, "no_compiler", False):
            stage = "llm_rtl_generation"
            print(f"[4/10][{label}] --no-compiler: calling model "
                  f"[{_effective_model(args)}] for IR -> RTL: {paths['rtl']}")
            rtl_path, compiler_report = generate_rtl_from_ir(
                paths["ir"], paths["rtl"], paths["compiler_report"],
                api_key=args.api_key, base_url=args.base_url,
                model=args.model, temperature=args.temperature,
                max_tokens=args.max_tokens)
        else:
            print(f"[4/10][{label}] Deterministic IR -> RTL compilation: {paths['rtl']}")
            rtl_path, compiler_report = compile_ir(
                paths["ir"], paths["rtl"], paths["compiler_report"],
                source_map_path=(args._c1_attempt_dir / 'source_map.json') if getattr(args, '_c1_enabled', False) else None)
        if getattr(args, '_c1_enabled', False):
            from assumption_modeling.analysis import start_analysis
            stage = 'assumption_analysis'
            args._c1_current = start_analysis(
                ir_path=paths['ir'], rtl_path=paths['rtl'],
                source_map_path=None if getattr(args, 'no_compiler', False) else args._c1_attempt_dir / 'source_map.json',
                spec_path=args.spec, output_dir=args._c1_attempt_dir,
                attempt_id=attempt_number, case_id=args._c1_run_id,
                contract=getattr(args, '_generation_contract', None),
                requirements_path=getattr(args, 'c1_requirements', None), evidence_path=getattr(args, 'c1_evidence', None),
                max_alternatives=getattr(args, 'c1_max_alternatives', 2), calibrator_path=getattr(args, 'c1_calibrator', None),
                property_path=getattr(args, 'c1_properties', None),
                checkpoint=getattr(args, 'c1_checkpoint', None), encoder_path=getattr(args, 'c1_encoder_path', None),
                output_protocol=json.loads(Path(args.c1_output_protocol).read_text(encoding='utf-8')) if getattr(args, 'c1_output_protocol', None) else None,
                horizon=getattr(args, 'c1_horizon', 16), budget_ms=getattr(args, 'c1_solver_timeout_ms', 2000),
                llm_options=dict(api_key=args.api_key, base_url=args.base_url, model=_effective_model(args)) if getattr(args, 'c1_llm', False) else None)
    except HLSGenerationError:
        raise
    except (ValueError, RuntimeError, Checker1Error, Checker2Error,
            PropertyIRCheckerError, SVALoweringError, SVAInstrumentationError,
            CompilerError) as exc:
        if stage == 'assumption_analysis':
            raise RuntimeError(f'Contribution 1 analysis failed (HW-IR repair is not applicable): {exc}') from exc
        raise _AttemptStageError(stage, exc) from exc

    property_checker_report = None
    sva_report = None
    sva_compile_report = {
        "tool": "verilator", "status": "not_run", "mode": "deferred",
        "reason": ("SVA disabled by --no_sva" if sva_disabled else
                   "SVA is generated only after an RTL-only simulation failure"),
        "classification": ("sva_disabled" if sva_disabled else
                           "sva_not_activated"),
        "feedback_target": "none",
        "hw_ir_repair_eligible": False,
    }

    if args.no_verilator:
        rtl_verilator_report = {
            "tool": "verilator", "status": "skipped", "mode": "disabled",
            "reason": "disabled by --no-verilator",
            "assertions_enabled": False,
            "rtl": str(paths["rtl"]),
            "base_rtl": str(paths["rtl"]),
            "sva": None,
        }
        _write_json(paths["rtl_verilator_report"], rtl_verilator_report)
        _write_json(paths["verilator_report"], rtl_verilator_report)
        print(f"[5/10][{label}] RTL-only Verilator validation skipped")
        print(f"[6-10/10][{label}] No RTL simulation failure; SVA will not be generated")
        return {
            "checker1": checker1_report,
            "checker2": checker2_report,
            "property_checker": property_checker_report,
            "sva": sva_report,
            "compiler": compiler_report,
            "sva_compile": sva_compile_report,
            "rtl_verilator": rtl_verilator_report,
            "verilator": rtl_verilator_report,
            "sva_activated": False,
            "sva_activation_reason": "verilator_disabled",
        }

    rtl_mode = ("lint" if args.verilator_lint_only or not testbenches
                else "simulation")
    print(f"[5/10][{label}] Verilator RTL-only {rtl_mode} validation: "
          f"{paths['rtl_verilator_report']}")
    _, rtl_verilator_report = run_verilator(
        rtl_path, generation_dir,
        testbenches=testbenches, extra_sources=extra_sources,
        tb_top=args.tb_top, timeout=args.verilator_timeout,
        lint_only=args.verilator_lint_only, assertions=False,
        coverage=False, artifact_prefix="rtl_verilator",
        raise_on_error=False, base_rtl_path=paths["rtl"], sva_path=None,
        report_output=paths["rtl_verilator_report"],
        compile_log_output=paths["rtl_verilator_compile_log"],
        simulation_log_output=paths["rtl_verilator_simulation_log"])

    rtl_runtime_failure = (
        rtl_verilator_report.get("status") == "failed" and
        rtl_verilator_report.get("mode") == "simulation" and
        rtl_verilator_report.get("compile_returncode") == 0 and
        rtl_verilator_report.get("simulation_returncode") is not None)
    if not rtl_runtime_failure:
        _write_json(paths["verilator_report"], rtl_verilator_report)
        reason = ("rtl_only_passed" if
                  rtl_verilator_report.get("status") == "passed" else
                  "rtl_failed_before_simulation")
        if reason == "rtl_only_passed":
            print(f"[6-10/10][{label}] RTL-only validation passed; SVA will not be generated")
        else:
            print(f"[6-10/10][{label}] RTL did not reach simulation; "
                  "sending feedback directly to HW-IR without generating SVA")
        return {
            "checker1": checker1_report,
            "checker2": checker2_report,
            "property_checker": property_checker_report,
            "sva": sva_report,
            "compiler": compiler_report,
            "sva_compile": sva_compile_report,
            "rtl_verilator": rtl_verilator_report,
            "verilator": rtl_verilator_report,
            "sva_activated": False,
            "sva_activation_reason": reason,
            "failure_stage": (None if reason == "rtl_only_passed" else
                              "rtl_only_validation_failure"),
        }

    if sva_disabled:
        # Keep the original RTL-only failure as the verification result.  The
        # outer HW-IR repair loop may still use the RTL/testbench diagnostics,
        # but no Property IR, SVA, or SVA-derived feedback is produced.
        verilator_report = dict(rtl_verilator_report)
        verilator_report["classification"] = (
            "rtl_only_simulation_failure_sva_disabled")
        verilator_report["sva_diagnostics"] = "disabled_by_no_sva"
        _write_json(paths["verilator_report"], verilator_report)
        print(f"[6-10/10][{label}] RTL-only simulation failed; --no-sva "
              "disables Property IR/SVA diagnostics, so feedback goes directly to HW-IR")
        return {
            "checker1": checker1_report,
            "checker2": checker2_report,
            "property_checker": property_checker_report,
            "sva": sva_report,
            "compiler": compiler_report,
            "sva_compile": sva_compile_report,
            "rtl_verilator": rtl_verilator_report,
            "verilator": verilator_report,
            "sva_activated": False,
            "sva_activation_reason": "disabled_by_no_sva",
            "failure_stage": "rtl_only_simulation_failure",
        }

    print(f"[SVA diagnostic][{label}] RTL-only simulation failed; "
          "starting SVA diagnostic generation")
    args._sva_diagnostics_activations = getattr(
        args, "_sva_diagnostics_activations", 0) + 1
    try:
        stage = "property_ir_generation"
        if args.spec:
            if fixed_spec_property_ir:
                print(f"[6/10][{label}] Reusing fixed Spec Property IR: "
                      f"{paths['property_ir']}")
            else:
                print(f"[6/10][{label}] Generating and freezing Property IR from SPEC: "
                      f"{paths['property_ir']}")
                print(f"[6/10][{label}] Calling model [{_effective_model(args)}] "
                      "to generate Property IR from SPEC")
                generate_spec_property_ir(
                    Path(args.spec).resolve(), paths["ir"], paths["property_ir"],
                    api_key=args.api_key, base_url=args.base_url,
                    model=args.model, temperature=args.temperature,
                    max_tokens=args.max_tokens)
        else:
            print(f"[6/10][{label}] No SPEC; generating Property IR from HW-IR "
                  "in compatibility mode: "
                  f"{paths['property_ir']}")
            generate_property_ir(paths["ir"], paths["property_ir"])
        stage = "property_ir_checker"
        print(f"[7/10][{label}] Checking Property IR: "
              f"{paths['property_checker_report']}")
        property_checker_report = check_property_ir(
            paths["ir"], paths["property_ir"],
            paths["property_checker_report"])
        stage = "sva_generation"
        compatibility_profile = getattr(
            args, "_sva_compatibility_profile", "default")
        print(f"[8/10][{label}] Deterministically generating SVA "
              f"(profile={compatibility_profile}): {paths['sva']}")
        _, _, sva_report = generate_sva(
            paths["ir"], paths["property_ir"], paths["sva"],
            paths["sva_report"],
            compatibility_profile=compatibility_profile)
        stage = "sva_instrumentation"
        instrument_rtl_with_sva(
            rtl_path, paths["sva"], paths["instrumented_rtl"])
    except (ValueError, RuntimeError, PropertyIRCheckerError,
            SVALoweringError, SVAInstrumentationError) as exc:
        raise _AttemptStageError(stage, exc) from exc

    print(f"[9/10][{label}] Independent SVA compile check: "
          f"{paths['sva_compile_report']}")
    sva_compile_report = _compile_sva(
        args, generation_dir, paths, extra_sources, sva_report)
    if sva_compile_report["status"] != "passed":
        sva_compile_report, sva_report = _repair_sva_compile(
            args, generation_dir, paths, extra_sources,
            sva_report, sva_compile_report)
    if sva_compile_report["status"] != "passed":
        verilator_report = {
            "tool": "verilator", "status": "skipped", "mode": "simulation",
            "reason": "blocked by sva_compile_failure",
            "sva_compile_report": str(paths["sva_compile_report"]),
            "rtl_only_report": str(paths["rtl_verilator_report"]),
            "assertions_enabled": True,
        }
        _write_json(paths["verilator_report"], verilator_report)
        return {
            "checker1": checker1_report,
            "checker2": checker2_report,
            "property_checker": property_checker_report,
            "sva": sva_report,
            "compiler": compiler_report,
            "sva_compile": sva_compile_report,
            "rtl_verilator": rtl_verilator_report,
            "verilator": verilator_report,
            "sva_activated": True,
            "sva_activation_reason": "rtl_only_simulation_failed",
            "failure_stage": "sva_compile_failure",
        }
    print(f"[10/10][{label}] Verilator RTL+SVA diagnostic simulation: "
          f"{paths['verilator_report']}")
    _, verilator_report = run_verilator(
        paths["instrumented_rtl"], generation_dir,
        testbenches=testbenches, extra_sources=extra_sources,
        tb_top=args.tb_top, timeout=args.verilator_timeout,
        lint_only=False, assertions=True,
        coverage=sva_report["cover_count"] > 0,
        raise_on_error=False, base_rtl_path=paths["rtl"],
        sva_path=paths["sva"])
    verilator_report["classification"] = (
        "rtl_only_simulation_failure_with_sva_diagnostics")
    verilator_report["rtl_only_report"] = str(paths["rtl_verilator_report"])
    verilator_report["rtl_only_status"] = rtl_verilator_report["status"]
    if verilator_report["status"] == "passed":
        # SVA is diagnostic here.  It cannot erase the original RTL-only
        # testbench failure that activated it.
        verilator_report["diagnostic_simulation_status"] = "passed"
        verilator_report["status"] = "failed"
        verilator_report["reason"] = (
            "RTL-only simulation failed; RTL+SVA diagnostic simulation "
            "did not add an assertion failure")
    _write_json(paths["verilator_report"], verilator_report)
    return {
        "checker1": checker1_report,
        "checker2": checker2_report,
        "property_checker": property_checker_report,
        "sva": sva_report,
        "compiler": compiler_report,
        "sva_compile": sva_compile_report,
        "rtl_verilator": rtl_verilator_report,
        "verilator": verilator_report,
        "sva_activated": True,
        "sva_activation_reason": "rtl_only_simulation_failed",
        "failure_stage": "rtl_only_simulation_failure",
    }


def _run_attempt(args, generation_dir, paths, testbenches, extra_sources, attempt_number):
    args._c1_attempt_dir = getattr(args, '_c1_run_dir', generation_dir / 'contribution1') / 'attempts' / f'attempt_{attempt_number}'
    args._c1_current = None
    outcome, error = None, None
    try:
        outcome = _run_attempt_impl(args, generation_dir, paths, testbenches, extra_sources, attempt_number)
        return outcome
    except Exception as exc:
        error = str(exc)
        raise
    finally:
        if getattr(args, '_c1_enabled', False):
            from assumption_modeling.schema import write_json, envelope
            if args._c1_current:
                from assumption_modeling.analysis import finish_analysis
                summary = finish_analysis(args._c1_current,
                    outcome.get('verilator', {}) if outcome else dict(status='failed', error=error),
                    bounded=getattr(args, 'c1_bounded', False), replay_enabled=not args.no_verilator,
                    timeout=args.verilator_timeout, property_path=paths['property_ir'])
            else:
                summary = dict(**envelope(args.spec, paths['ir'], attempt_id=attempt_number),
                               status='not_analyzed', reason=error or 'RTL unavailable', directory=str(args._c1_attempt_dir))
                write_json(args._c1_attempt_dir / 'analysis_manifest.json', summary)
            args._c1_attempts.append(summary)
            write_json(args._c1_run_dir / 'manifest.json', dict(run_id=args._c1_run_id,
                       pre_ir=getattr(args, '_pre_ir_summary', None), attempts=args._c1_attempts))


def _feedback_artifacts(paths, *, include_sva=True, compiler_mode="deterministic"):
    if compiler_mode == "hls":
        return [
            ("CHECKER1 REPORT", paths["checker1_report"]),
            ("CHECKER2 REPORT", paths["checker2_report"]),
            ("HLS GENERATION REPORT", paths["hls_report"]),
            ("GENERATED HLS C++", paths["hls"]),
        ]
    artifacts = [
        ("CHECKER1 REPORT", paths["checker1_report"]),
        ("CHECKER2 REPORT", paths["checker2_report"]),
        (("LLM RTL GENERATION REPORT" if compiler_mode == "llm" else
          "RTL COMPILER REPORT"), paths["compiler_report"]),
        ("VERILATOR RTL-ONLY REPORT", paths["rtl_verilator_report"]),
        ("VERILATOR RTL-ONLY BUILD LOG", paths["rtl_verilator_compile_log"]),
        ("VERILATOR RTL-ONLY SIMULATION LOG",
         paths["rtl_verilator_simulation_log"]),
    ]
    if include_sva:
        artifacts.extend([
            ("PROPERTY IR BEFORE CHECKER NORMALIZATION",
             paths["property_ir_checker_input"]),
            ("PROPERTY IR CHECKER REPORT", paths["property_checker_report"]),
            ("SVA GENERATION REPORT", paths["sva_report"]),
            ("SVA COMPILE CHECK REPORT", paths["sva_compile_report"]),
            ("SVA COMPILE CHECK LOG", paths["sva_compile_log"]),
            ("SVA COMPILE REPAIR LOOP REPORT",
             paths["sva_compile_repair_loop_report"]),
            ("VERILATOR RTL+SVA REPORT", paths["verilator_report"]),
            ("VERILATOR BUILD LOG", paths["verilator_compile_log"]),
            ("VERILATOR SIMULATION LOG", paths["verilator_simulation_log"]),
        ])
    if compiler_mode == "llm":
        artifacts.extend([
            ("LLM RTL GENERATION PROMPT", paths["rtl_llm_prompt"]),
            ("LLM RTL RAW RESPONSE", paths["rtl_llm_raw_response"]),
            ("LLM RTL RESPONSE METADATA", paths["rtl_llm_response"]),
        ])
    return artifacts


def run_hls_regeneration(args):
    """Lower an already-generated HW-IR to HLS C++ without calling the LLM."""
    if getattr(args, "hls", False):
        raise ValueError("--hls and --hls-re cannot be used together")
    if getattr(args, "no_compiler", False):
        raise ValueError("--hls-re and --no-compiler cannot be used together")
    if args.ir:
        raise ValueError(
            "--hls-re uses the IR already produced beside the spec; "
            "do not pass --ir (use --generation-dir to select the artifact directory)")
    if not args.spec:
        raise ValueError("--hls-re requires a spec file")
    if not Path(args.spec).is_file():
        raise ValueError(f"Spec file not found: {args.spec}")

    generation_dir = _artifact_directory(args)
    paths = _paths(generation_dir)
    ir_path = paths["ir"]
    if not ir_path.is_file():
        raise ValueError(
            "--hls-re requires an existing IR from a completed pipeline run: "
            f"{ir_path}")

    generation_dir.mkdir(parents=True, exist_ok=True)
    print(f"[1/2] Using existing IR (no LLM): {ir_path}")
    print(f"[2/2] Generating HLS C++ from existing IR: {paths['hls']}")
    _, hls_report = generate_hls_c(ir_path, paths["hls"], paths["hls_report"])

    created_at = datetime.now(timezone.utc).isoformat()
    hls_fields = {
        "hls_c": str(paths["hls"]),
        "hls_generation_report": str(paths["hls_report"]),
        "hls_regenerated": True,
        "hls_regenerated_at": created_at,
        "hls_regeneration_source_ir": str(ir_path),
        "hls_generation_status": hls_report.get("status"),
        "hls_module": hls_report.get("module"),
    }
    manifest_path = generation_dir / "pipeline_manifest.json"
    on_disk = {}
    if manifest_path.is_file():
        try:
            on_disk = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            on_disk = {}
    if on_disk:
        on_disk.update(hls_fields)
    else:
        on_disk = {
            "status": "ok",
            "created_at": created_at,
            "spec": str(Path(args.spec).resolve()),
            "frontend": "existing_ir",
            "output_mode": "hls_c",
            "rtl_generation_mode": "not_run_hls_regeneration",
            "verification_strategy": "hls_regeneration_from_existing_ir",
            "sva_enabled": False,
            "ir": str(ir_path),
            "module": hls_report.get("module"),
            "llm_response": None,
            **hls_fields,
        }
    _write_json(manifest_path, on_disk)

    print_manifest = {
        "status": "ok",
        "spec": str(Path(args.spec).resolve()),
        "frontend": "existing_ir",
        "output_mode": "hls_c",
        "hls_regenerated": True,
        "ir": str(ir_path),
        "checker1_report": (str(paths["checker1_report"])
                            if paths["checker1_report"].is_file() else None),
        "checker2_report": (str(paths["checker2_report"])
                            if paths["checker2_report"].is_file() else None),
        "hls_c": str(paths["hls"]),
        "hls_generation_report": str(paths["hls_report"]),
        "module": hls_report.get("module"),
    }
    return manifest_path, print_manifest


def _run_pipeline_impl(args):
    if getattr(args, "hls_re", False):
        return run_hls_regeneration(args)
    if not args.spec and not args.ir:
        raise ValueError("Provide a spec file or use --ir to compile an existing IR")
    if args.spec and not Path(args.spec).is_file():
        raise ValueError(f"Spec file not found: {args.spec}")
    if args.ir and not Path(args.ir).is_file():
        raise ValueError(f"IR file not found: {args.ir}")
    if getattr(args, "hls", False) and getattr(args, "no_compiler", False):
        raise ValueError("--hls and --no-compiler cannot be used together")

    generation_dir = _artifact_directory(args)
    generation_dir.mkdir(parents=True, exist_ok=True)
    paths = _paths(generation_dir)
    generated_ir = paths["ir"]

    from uuid import uuid4
    from assumption_modeling.schema import write_json as c1_write_json
    if getattr(args, 'c1_horizon', 16) < 1 or getattr(args, 'c1_solver_timeout_ms', 2000) < 1:
        raise ValueError('Contribution 1 horizon and solver budget must be positive')
    if getattr(args, 'c1_llm', False) and not all((args.api_key, args.base_url, _effective_model(args))):
        raise ValueError('--c1-llm requires configured API key, base URL and frozen model version')
    if getattr(args, 'c1_max_alternatives', 2) < 1:
        raise ValueError('--c1-max-alternatives must be positive')
    args._c1_run_id = datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%S') + '_' + uuid4().hex[:8]
    args._c1_run_dir = generation_dir / 'contribution1' / 'runs' / args._c1_run_id
    args._c1_enabled = not getattr(args, 'no_assumption_analysis', False) and not getattr(args, 'hls', False)
    args._c1_attempts = []
    args._generation_contract = None
    args._pre_ir_summary = dict(status='not_run', mode='existing_ir' if args.ir else 'optional_extension_disabled')
    pre_enabled = (getattr(args, 'pre_ir_analysis', False) or getattr(args, 'analyze_spec_only', False)
                   or getattr(args, 'clarifications', None))
    if getattr(args, 'skip_pre_ir_analysis', False) and pre_enabled:
        raise ValueError('--skip-pre-ir-analysis conflicts with pre-IR analysis/clarifications')
    if pre_enabled:
        if not args.spec:
            raise ValueError('Pre-IR analysis requires a specification')
        from assumption_modeling.pre_ir import analyze_before_ir
        pre = analyze_before_ir(args.spec, generation_dir / 'contribution1' / 'pre_ir', getattr(args, 'clarifications', None))
        args._pre_ir_summary = pre['state']
        args._generation_contract = pre['contract']
        if pre['status'] != 'ready' or getattr(args, 'analyze_spec_only', False):
            manifest = dict(status=pre['status'], frontend='spec_analysis', ir=None,
                            analysis_only=True, pre_ir=pre['state'])
            manifest_path = generation_dir / 'pipeline_manifest.json'
            c1_write_json(manifest_path, manifest)
            return manifest_path, manifest

    args._sva_compatibility_profile = "default"
    args._sva_compile_repairs_used = 0
    args._sva_compile_repair_history = []
    args._sva_diagnostics_activations = 0
    for key in ("sva_compile_repair_loop_report", "sva_compile_feedback"):
        if paths[key].is_file():
            paths[key].unlink()
    sva_repair_directory = generation_dir / "sva_repair_iterations"
    if sva_repair_directory.is_dir():
        shutil.rmtree(str(sva_repair_directory))

    # A new top-level run must never reuse a Spec Property IR from an older
    # run. Once generated in this run it is preserved across HW-IR repairs.
    for key in ("property_ir", "property_ir_checker_input", "property_ir_prompt",
                "property_ir_raw_response", "property_ir_response"):
        if paths[key].is_file():
            paths[key].unlink()

    if args.ir:
        source_ir = Path(args.ir).resolve()
        if source_ir != generated_ir.resolve():
            shutil.copyfile(str(source_ir), str(generated_ir))
        frontend = "existing_ir"
        llm_meta = None
        first_step = "1/4" if getattr(args, "hls", False) else "1/10"
        print(f"[{first_step}] Using existing IR: {generated_ir}")
    else:
        first_step = "1/4" if getattr(args, "hls", False) else "1/10"
        print(f"[{first_step}] Generating IR from spec: {generated_ir}")
        print(f"[{first_step}] Calling model [{_effective_model(args)}] "
              "to generate IR from spec")
        generated_ir, llm_meta = generate_ir(
            Path(args.spec).resolve(), generated_ir,
            api_key=args.api_key, base_url=args.base_url, model=args.model,
            temperature=args.temperature, max_tokens=args.max_tokens,
            repair=args.repair, allow_invalid=True,
            generation_contract=args._generation_contract)
        frontend = "llm"
    maximum = getattr(args, "max_hw_ir_repair_iterations", None)
    if maximum is None:
        maximum = getattr(args, "max_sva_repair_iterations", 3)
    if maximum < 0 or maximum > 3:
        raise ValueError("--max-hw-ir-repair-iterations must be between 0 and 3")
    repair_disabled = (
        getattr(args, "no_hw_ir_repair", False) or
        getattr(args, "no_sva_repair", False))
    if repair_disabled:
        maximum = 0
    testbenches = [] if getattr(args, "hls", False) else _testbenches(args)
    extra_sources = ([] if getattr(args, "hls", False) else
                     [Path(path).resolve() for path in args.extra_source])
    history = []
    repair_meta = []
    repairs_used = 0
    attempt_number = 0
    while True:
        try:
            outcome = _run_attempt(
                args, generation_dir, paths, testbenches, extra_sources,
                attempt_number)
            sva_compile_report = outcome.get("sva_compile", {
                "status": "not_run", "mode": "deferred"})
            if sva_compile_report["status"] == "failed":
                raise SVACompileValidationError(
                    "The independent SVA compile check and SVA repair loop "
                    "both failed. The result is classified as "
                    "sva_compile_failure and will not be sent to HW-IR; "
                    f"complete feedback: {paths['sva_compile_feedback']}",
                    paths["sva_compile_report"])
            verilator_report = outcome["verilator"]
            if not getattr(args, "hls", False):
                _print_verilator_result(
                    verilator_report, paths["verilator_report"])
            if verilator_report["status"] in {"passed", "skipped"}:
                break
            failure_stage = outcome.get(
                "failure_stage", "rtl_only_simulation_failure")
            failure = {
                "stage": failure_stage,
                "attempt": attempt_number,
                "sva_activated": bool(outcome.get("sva_activated", False)),
                "sva_activation_reason": outcome.get(
                    "sva_activation_reason"),
                "rtl_only_verilator_report": outcome.get(
                    "rtl_verilator", verilator_report),
                "verilator_report": verilator_report,
            }
        except _AttemptStageError as exc:
            outcome = None
            failure = {
                "stage": "deterministic_pipeline_failure",
                "attempt": attempt_number,
                "failed_step": exc.stage,
                "error_type": type(exc.cause).__name__,
                "message": str(exc.cause),
                "mechanical_repair": _mechanical_repair_summary(
                    paths, exc.stage),
            }

        record = {"attempt": attempt_number, "failure": failure}
        history.append(record)
        loop_report = {
            "status": "repairing",
            "max_repair_iterations": maximum,
            "repairs_used": repairs_used,
            "attempts": history,
        }
        _write_loop_report(paths, loop_report)
        if repairs_used >= maximum:
            loop_report["status"] = "exhausted"
            _write_loop_report(paths, loop_report)
            if failure["stage"] in {
                    "rtl_only_simulation_failure",
                    "rtl_only_validation_failure"}:
                raise VerilatorValidationError(
                    "RTL-only validation failed and HW-IR repair attempts are exhausted",
                    paths["verilator_report"])
            raise RuntimeError(
                f"HW-IR repair attempts are exhausted; see {paths['repair_loop_report']}")

        repair_number = repairs_used + 1
        repair_dir = generation_dir / "repair_iterations" / f"iteration_{repair_number}"
        repair_dir.mkdir(parents=True, exist_ok=True)
        failure_label = failure.get(
            "failed_step", failure.get("stage", "pipeline_failure"))
        failure_reason = (
            "still failed after mechanical repair" if failure.get("failed_step")
            else ("RTL-only simulation failed; SVA diagnostics were generated on demand"
                  if failure.get("sva_activated") else
                  ("RTL-only simulation failed; --no-sva disabled SVA diagnostics"
                   if failure.get("sva_activation_reason") ==
                   "disabled_by_no_sva" else
                   "RTL-only lint/build failed; SVA was not generated")))
        print(f"[HW-IR repair {repair_number}/{maximum}] "
              f"{failure_label}: {failure_reason}; writing complete feedback to {repair_dir}")
        shutil.copyfile(str(generated_ir), str(repair_dir / "input_hw_ir.yaml"))
        context = build_repair_context(
            iteration=repair_number, failure=failure,
            spec_path=Path(args.spec).resolve() if args.spec else None,
            hw_ir_path=generated_ir, property_ir_path=paths["property_ir"],
            rtl_path=paths["rtl"],
            instrumented_rtl_path=paths["instrumented_rtl"],
            sva_path=paths["sva"], testbenches=testbenches,
            extra_sources=extra_sources,
            artifacts=_feedback_artifacts(
                paths, include_sva=not getattr(args, "no_sva", False),
                compiler_mode=("hls" if getattr(args, "hls", False) else
                               "llm" if getattr(args, "no_compiler", False)
                               else "deterministic")),
            history=history,
            sva_enabled=(not getattr(args, "no_sva", False) and
                         not getattr(args, "hls", False)),
            output_mode=("hls" if getattr(args, "hls", False) else "rtl"),
            generation_contract=args._generation_contract)
        # Persist the complete, untruncated feedback before contacting the
        # model so diagnostics remain available even when configuration or
        # the remote request fails.
        (repair_dir / "feedback_context.md").write_text(
            context, encoding="utf-8")
        try:
            print(f"[HW-IR repair {repair_number}/{maximum}] "
                  f"Calling model [{_effective_model(args)}] to repair HW-IR")
            candidate, meta = repair_hw_ir(
                context_text=context, output_dir=repair_dir,
                hw_ir_path=generated_ir,
                api_key=args.api_key, base_url=args.base_url, model=args.model,
                temperature=args.temperature, max_tokens=args.max_tokens)
        except (ValueError, RuntimeError) as exc:
            loop_report["status"] = "blocked"
            loop_report["error"] = str(exc)
            _write_loop_report(paths, loop_report)
            raise
        shutil.copyfile(str(candidate), str(generated_ir))
        print(f"[HW-IR repair {repair_number}/{maximum}] "
              "Patch candidate passed checker1/checker2; committing it and "
              "rerunning the complete pipeline: "
              f"{generated_ir}")
        record["repair"] = meta
        repair_meta.append(meta)
        repairs_used += 1
        attempt_number += 1
        loop_report["repairs_used"] = repairs_used
        _write_loop_report(paths, loop_report)

    checker1_report = outcome["checker1"]
    checker2_report = outcome["checker2"]
    property_checker_report = outcome.get("property_checker")
    sva_report = outcome.get("sva")
    rtl_verilator_report = outcome.get("rtl_verilator", verilator_report)
    sva_activated = bool(outcome.get("sva_activated", False))
    report = outcome["compiler"]
    if history:
        _write_loop_report(paths, {
            "status": "passed", "max_repair_iterations": maximum,
            "repairs_used": repairs_used, "attempts": history,
        })
    manifest = {
        "status": "ok",
        "pre_ir": args._pre_ir_summary,
        "contribution1": dict(enabled=args._c1_enabled, run_id=args._c1_run_id,
                              attempts=args._c1_attempts),
        "created_at": datetime.now(timezone.utc).isoformat(),
        "spec": str(Path(args.spec).resolve()) if args.spec else None,
        "frontend": frontend,
        "output_mode": ("hls_c" if getattr(args, "hls", False) else "rtl"),
        "rtl_generation_mode": ("not_run_hls_mode" if
                                getattr(args, "hls", False) else
                                "llm" if getattr(args, "no_compiler", False)
                                else "deterministic_compiler"),
        "verification_strategy": ("hls_generation_only" if
                                  getattr(args, "hls", False) else
                                  "rtl_only_no_sva" if
                                  getattr(args, "no_sva", False) else
                                  "rtl_first_sva_on_failure"),
        "sva_enabled": (not getattr(args, "no_sva", False) and
                        not getattr(args, "hls", False)),
        "ir": str(generated_ir),
        "rtl": str(paths["rtl"]) if paths["rtl"].is_file() else None,
        "hls_c": str(paths["hls"]) if paths["hls"].is_file() else None,
        "hls_generation_report": (str(paths["hls_report"])
                                  if paths["hls_report"].is_file() else None),
        "rtl_with_sva": (str(paths["instrumented_rtl"])
                         if paths["instrumented_rtl"].is_file() else None),
        "compiler_report": (str(paths["compiler_report"])
                            if paths["compiler_report"].is_file() else None),
        "rtl_llm_prompt": (str(paths["rtl_llm_prompt"])
                           if paths["rtl_llm_prompt"].is_file() else None),
        "rtl_llm_raw_response": (str(paths["rtl_llm_raw_response"])
                                 if paths["rtl_llm_raw_response"].is_file()
                                 else None),
        "rtl_llm_response": (str(paths["rtl_llm_response"])
                             if paths["rtl_llm_response"].is_file() else None),
        "checker1_report": str(paths["checker1_report"]),
        "checker1_status": checker1_report["status"],
        "checker2_report": str(paths["checker2_report"]),
        "checker2_status": checker2_report["status"],
        "property_ir": (str(paths["property_ir"])
                        if paths["property_ir"].is_file() else None),
        "property_ir_source": (("spec" if args.spec else
                                "hw_ir_compatibility")
                               if paths["property_ir"].is_file() else None),
        "property_ir_prompt": (str(paths["property_ir_prompt"])
                               if paths["property_ir_prompt"].is_file() else None),
        "property_ir_response": (str(paths["property_ir_response"])
                                 if paths["property_ir_response"].is_file() else None),
        "property_ir_checker_report": (
            str(paths["property_checker_report"])
            if paths["property_checker_report"].is_file() else None),
        "property_ir_checker_input": (
            str(paths["property_ir_checker_input"])
            if paths["property_ir_checker_input"].is_file() else None),
        "property_ir_checker_status": (
            property_checker_report["status"] if property_checker_report
            else "not_run"),
        "property_count": (property_checker_report["property_count"]
                           if property_checker_report else 0),
        "sva_activated": sva_activated,
        "sva_activation_reason": outcome.get("sva_activation_reason"),
        "sva_diagnostics_activation_count": getattr(
            args, "_sva_diagnostics_activations", 0),
        "sva": str(paths["sva"]) if paths["sva"].is_file() else None,
        "sva_generation_report": (str(paths["sva_report"])
                                  if paths["sva_report"].is_file() else None),
        "sva_generation_status": (sva_report["status"] if sva_report
                                  else "not_generated"),
        "sva_assertion_count": (sva_report["assertion_count"]
                                if sva_report else 0),
        "sva_assertion_action_block_count": (sva_report.get(
            "assertion_action_block_count", 0) if sva_report else 0),
        "sva_assertion_diagnostic_value_count": (sva_report.get(
            "assertion_diagnostic_value_count", 0) if sva_report else 0),
        "sva_cover_count": (sva_report["cover_count"] if sva_report else 0),
        "sva_compile_report": (str(paths["sva_compile_report"])
                               if paths["sva_compile_report"].is_file()
                               else None),
        "sva_compile_status": sva_compile_report["status"],
        "sva_compatibility_profile": getattr(
            args, "_sva_compatibility_profile", "default"),
        "sva_compile_repair_iterations": getattr(
            args, "_sva_compile_repairs_used", 0),
        "sva_compile_repair_loop_report": (
            str(paths["sva_compile_repair_loop_report"])
            if paths["sva_compile_repair_loop_report"].is_file() else None),
        "rtl_verilator_report": (str(paths["rtl_verilator_report"])
                                 if paths["rtl_verilator_report"].is_file()
                                 else None),
        "rtl_verilator_status": rtl_verilator_report["status"],
        "verilator_report": (str(paths["verilator_report"])
                             if paths["verilator_report"].is_file() else None),
        "verilator_status": verilator_report["status"],
        "verilator_mode": verilator_report.get("mode"),
        "module": report["module"],
        "llm_response": llm_meta,
        "hw_ir_repair_iterations": repairs_used,
        "hw_ir_repair_responses": repair_meta,
        "hw_ir_repair_loop_report": (str(paths["repair_loop_report"])
                                      if history else None),
        "sva_repair_loop_report": (str(paths["legacy_repair_loop_report"])
                                   if history else None),
    }
    manifest_path = generation_dir / "pipeline_manifest.json"
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return manifest_path, manifest


def run_pipeline(args):
    try:
        return _run_pipeline_impl(args)
    except Exception as exc:
        if getattr(args, '_c1_run_id', None) and not getattr(args, 'hls_re', False):
            from assumption_modeling.schema import write_json
            generation_dir = _artifact_directory(args)
            write_json(generation_dir / 'pipeline_manifest.json', dict(
                status='failed', error=str(exc), spec=str(Path(args.spec).resolve()) if args.spec else None,
                frontend='existing_ir' if args.ir else 'llm', pre_ir=getattr(args, '_pre_ir_summary', None),
                contribution1=dict(enabled=getattr(args, '_c1_enabled', False), run_id=args._c1_run_id,
                                   attempts=getattr(args, '_c1_attempts', []))))
        raise


def main():
    args = _arguments()
    try:
        manifest_path, manifest = run_pipeline(args)
    except SVACompileValidationError as exc:
        try:
            failed_report = json.loads(
                Path(exc.report_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failed_report = {"status": "failed", "mode": "lint"}
        _print_verilator_result(
            failed_report, exc.report_path, stream=sys.stderr,
            heading="SVA COMPILE CHECK RESULT")
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except VerilatorValidationError as exc:
        try:
            failed_report = json.loads(
                Path(exc.report_path).read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            failed_report = {"status": "failed"}
        _print_verilator_result(
            failed_report, exc.report_path, stream=sys.stderr)
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    except (ValueError, RuntimeError, Checker1Error, Checker2Error,
            PropertyIRCheckerError, SVALoweringError,
            SVAInstrumentationError, CompilerError, HLSGenerationError) as exc:
        print(f"Error: {exc}", file=sys.stderr)
        return 1
    if manifest.get('analysis_only'):
        print('SPEC ANALYSIS: ' + manifest['status'].upper())
        print(f"  manifest: {manifest_path}")
        return 0 if manifest['status'] == 'ready' else 2
    print("PIPELINE RESULT: PASSED")
    print(f"  IR:       {manifest['ir']}")
    if manifest.get("hls_regenerated"):
        print("  frontend: existing IR (LLM not called)")
        print(f"  HLS C++:  {manifest['hls_c']}")
        print(f"  HLS report: {manifest['hls_generation_report']}")
        print("  note:     existing RTL/SVA artifacts were left unchanged")
        print(f"  manifest: {manifest_path}")
        return 0
    print(f"  checker1: {manifest['checker1_report']}")
    print(f"  checker2: {manifest['checker2_report']}")
    if manifest["output_mode"] == "hls_c":
        print(f"  HLS C++:  {manifest['hls_c']}")
        print(f"  HLS report: {manifest['hls_generation_report']}")
        print(f"  manifest: {manifest_path}")
        return 0
    print(f"  RTL:      {manifest['rtl']}")
    print(f"  RTL generation: {manifest['rtl_generation_mode']}")
    print(f"  RTL-only Verilator: {manifest['rtl_verilator_report']}")
    if manifest["sva_activated"]:
        print(f"  Property IR: {manifest['property_ir']}")
        print(f"  Property checker: {manifest['property_ir_checker_report']}")
        print(f"  SVA:      {manifest['sva']}")
        print(f"  SVA report: {manifest['sva_generation_report']}")
        print(f"  SVA compile: {manifest['sva_compile_report']}")
        print(f"  SVA compile repairs: "
              f"{manifest['sva_compile_repair_iterations']} "
              f"(profile={manifest['sva_compatibility_profile']})")
        print(f"  RTL+SVA:  {manifest['rtl_with_sva']}")
    else:
        reason = manifest.get("sva_activation_reason")
        if reason == "disabled_by_no_sva":
            print("  SVA:      disabled by --no_sva")
        else:
            print("  SVA:      not generated (RTL-only did not fail in simulation)")
    print(f"  Verilator: {manifest['verilator_report']}")
    print(f"  HW-IR repairs: {manifest['hw_ir_repair_iterations']}")
    print(f"  manifest: {manifest_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
