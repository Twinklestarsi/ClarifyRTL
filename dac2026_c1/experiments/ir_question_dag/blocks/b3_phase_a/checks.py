"""Spec -> initial IR, plus the deterministic check battery.

The checks are the entry ticket for phase A, not its goal: correctness says the
IR compiles, representability (block 2/3) says the questions have a place to
live inside it.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from blocks.b1_scaffold.runctx import RunContext
from blocks.b3_phase_a.test_frontend import (DEFAULT_TEST_ROOT,
                                               frontend_fingerprint,
                                               generate_with_test_frontend,
                                               public_contract,
                                               resolve_generation_contract)


def generation_cache(case, ctx):
    """Per-case cache: only a *passing* IR is reused verbatim."""
    return ctx.directory.parent / "_generation_cache" / "ir.yaml"


def _cache_status(case, ctx):
    import json
    path = generation_cache(case, ctx).parent / "status.json"
    if not path.is_file():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return {}


def freeze_generation(case, ctx, ir_path, *, passed: bool,
                      fingerprint: str | None = None) -> None:
    """Record a passing IR permanently; keep the first failure as a provisional baseline."""
    target = generation_cache(case, ctx)
    status = _cache_status(case, ctx)
    if not passed and status.get("passed"):
        return  # never downgrade a passing baseline
    if not passed and target.is_file() and status.get("attempted"):
        return  # keep the provisional baseline, but let a future pass replace it
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(ir_path.read_text(encoding="utf-8"), encoding="utf-8")
    (target.parent / "status.json").write_text(
        json.dumps({"passed": bool(passed), "attempted": True,
                    "frontend_fingerprint": fingerprint}), encoding="utf-8")
    ctx.event("generation_frozen", cache=str(target.relative_to(ctx.directory.parent)),
              passed=bool(passed), frontend_fingerprint=fingerprint)


def generate_initial_ir(case, ctx: RunContext, env: dict[str, str], *,
                        temperature: float = 0.2,
                        max_tokens: int | None = None,
                        frontend: dict[str, Any] | None = None) -> tuple[Path, dict]:
    """Generate one complete IR with the frozen test monolithic frontend.

    ``max_tokens=None`` removes the old 32000-token output cap.
    """
    settings = dict(frontend or {})
    mode = settings.get("frontend", "test_monolithic")
    if mode != "test_monolithic":
        raise ValueError(f"unsupported initial IR frontend: {mode}")
    test_root = Path(settings.get("test_root", DEFAULT_TEST_ROOT)).resolve()
    contract = resolve_generation_contract(settings)
    fingerprint = frontend_fingerprint(test_root, contract=contract)
    target = ctx.path("phase_a/init/ir.yaml")
    cached = generation_cache(case, ctx)
    status = _cache_status(case, ctx)
    if (cached.is_file() and status.get("passed") and
            status.get("frontend_fingerprint") == fingerprint):
        target.write_text(cached.read_text(encoding="utf-8"), encoding="utf-8")
        ctx.event("generation_reused",
                  cache=str(cached.relative_to(ctx.directory.parent)),
                  frontend="test_monolithic", frontend_variant=contract["version"],
                  frontend_fingerprint=fingerprint)
        return target, {"cached": True, "frontend": "test_monolithic",
                        "frontend_variant": contract["version"],
                        "generation_contract": public_contract(contract),
                        "frontend_root": str(test_root),
                        "frontend_fingerprint": fingerprint}
    ir_path, meta = generate_with_test_frontend(
        case.prompt, target, env, test_root=test_root,
        temperature=temperature, max_tokens=max_tokens,
        timeout_s=float(settings.get("timeout_s", 1200)), contract=contract)
    usage = (meta.get("usage") or {})
    ctx.event("ir_generated", path=str(ir_path.relative_to(ctx.directory)),
              total_tokens=usage.get("total_tokens"), model=meta.get("model"),
              frontend="test_monolithic", frontend_variant=contract["version"],
              frontend_root=str(test_root), frontend_fingerprint=fingerprint)
    for key in ("frontend_log", "frontend_metadata", "frontend_contract_file"):
        artifact = Path(meta.get(key, ""))
        if artifact.is_file():
            ctx.record(artifact)
    return Path(ir_path), meta


def repair_initial_ir(case, ctx: RunContext, env: dict[str, str], *,
                      ir_path: Path, checks: dict[str, Any], checks_dir: Path,
                      round_index: int, history: list[dict[str, Any]],
                      max_tokens: int | None = None,
                      max_patch_attempts: int = 3) -> tuple[Path, dict, dict]:
    """Ask ``hw_ir_repair`` for one transactional patch to a failed initial IR."""
    from iteration.hw_ir_repair import build_repair_context, repair_hw_ir

    failure = {
        "stage": "initial_ir_validation_failure",
        "attempt": round_index - 1,
        "failed_step": checks.get("reason", "check_ir"),
        "error_type": checks.get("reason", "IRValidationError"),
        "message": checks.get("error") or json.dumps(
            checks, ensure_ascii=False)[:4000],
        "checks": checks,
    }
    output_dir = (ctx.directory / "phase_a" / "init" / "repair_iterations" /
                  f"iteration_{round_index}")
    output_dir.mkdir(parents=True, exist_ok=True)
    artifacts = []
    for name, label in (("checker1_report.json", "CHECKER1 REPORT"),
                        ("checker2_report.json", "CHECKER2 REPORT"),
                        ("compiler_report.json", "COMPILER REPORT")):
        path = checks_dir / name
        if path.is_file():
            artifacts.append((label, path))
    context = build_repair_context(
        iteration=round_index, failure=failure, spec_path=case.prompt,
        hw_ir_path=ir_path, property_ir_path=None,
        rtl_path=ctx.directory / "rtl" / "init_smoke.v",
        instrumented_rtl_path=None, sva_path=None,
        testbenches=[], extra_sources=[], artifacts=artifacts,
        history=history + [{"attempt": round_index - 1, "failure": failure}],
        sva_enabled=False, output_mode="rtl", generation_contract=None)
    candidate, metadata = repair_hw_ir(
        context_text=context, output_dir=output_dir, hw_ir_path=ir_path,
        api_key=env["OPENAI_API_KEY"], base_url=env["OPENAI_BASE_URL"],
        model=env["OPENAI_MODEL"], temperature=0.2,
        max_tokens=max_tokens, max_patch_attempts=max_patch_attempts)
    for artifact in output_dir.rglob("*"):
        if artifact.is_file():
            ctx.record(artifact)
    return Path(candidate), metadata, failure


def check_ir(ir_path: Path, out_dir: Path, *, compile_smoke: bool = True,
             emit_rtl: Path | None = None,
             verilator: str | Path | None = None) -> dict[str, Any]:
    """checker1 -> checker2 -> validate_ir -> optional compile.

    checker1/checker2 rewrite the IR file in place, so callers must copy the
    frozen candidate *after* this function returns.
    """
    from IRgeneration.checker1 import check_ir as checker1
    from IRgeneration.checker2 import check_ir as checker2
    from Compiler.rtl_compiler.parser import parse_ir
    from Compiler.rtl_compiler.validator import validate_ir
    from Compiler.rtl_compiler import compile_ir

    out_dir.mkdir(parents=True, exist_ok=True)
    report: dict[str, Any] = {"ir": str(ir_path), "status": "failed", "stages": {}}
    try:
        r1 = checker1(ir_path, out_dir / "checker1_report.json")
        report["stages"]["checker1"] = {"status": r1["status"], "errors": r1["error_count"],
                                        "repairs": r1.get("repairs", [])[:20]}
        if r1["status"] == "failed":
            report["reason"] = "checker1"
            details = r1.get("errors") or []
            report["errors"] = details[:60]
            report["error"] = "; ".join(
                f"{row.get('location', '$')}: {row.get('message', row)}"
                for row in details)[:6000] or "checker1 failed"
            return report
        r2 = checker2(ir_path, out_dir / "checker2_report.json")
        report["stages"]["checker2"] = {"status": r2["status"], "errors": r2["error_count"],
                                        "normalizations": r2.get("normalization_count", 0)}
        if r2["status"] == "failed":
            report["reason"] = "checker2"
            # The rewrite prompt needs the *complete* error list: a truncated
            # one made the model fix the first few and reintroduce the rest.
            details = r2.get("errors") or []
            report["errors"] = details[:80]
            report["error"] = "; ".join(
                f"{row.get('location', '$')}: {row.get('message', row)}"
                for row in details)[:6000] or "checker2 failed"
            return report
        model = parse_ir(ir_path)
        # Derived signals must not form combinational cycles (including
        # self-reference); such an IR cannot be lowered to valid RTL.
        import re as _re

        derived_names = {str(item.get("name", "")).strip()
                         for item in getattr(model, "derived_signals", [])}
        edges: dict[str, set[str]] = {}
        for item in getattr(model, "derived_signals", []):
            name = str(item.get("name", "")).strip()
            symbols = set(_re.findall(r"[A-Za-z_][A-Za-z0-9_$]*",
                                      str(item.get("expression", ""))))
            edges[name] = symbols & derived_names
        visiting: set[str] = set()
        visited: set[str] = set()
        cycle: list[str] = []

        def visit(node: str, stack: list[str]) -> bool:
            if node in visiting:
                cycle.extend(stack[stack.index(node):] + [node])
                return True
            if node in visited:
                return False
            visiting.add(node)
            stack.append(node)
            for dep in edges.get(node, ()):
                if visit(dep, stack):
                    return True
            stack.pop()
            visiting.remove(node)
            visited.add(node)
            return False

        for node in edges:
            if visit(node, []):
                report["stages"]["derived_cycles"] = {
                    "status": "failed", "cycle": cycle[:8]}
                report["status"] = "failed"
                report["reason"] = "derived_cycle"
                report["error"] = "derived signal dependency cycle: " + " -> ".join(cycle[:8])
                return report
        report["stages"]["derived_cycles"] = {"status": "passed"}
        warnings = validate_ir(model)
        report["stages"]["validate"] = {"status": "passed", "warnings": list(warnings)[:20]}
        report["module"] = model.module_name
        report["summary"] = {
            "ports": len(model.ports), "states": len(model.states),
            "transitions": len(model.transitions),
            "derived": len(model.derived_signals),
            "priorities": len(model.priorities),
            "clock_domains": len(model.clock_domains),
        }
        if compile_smoke:
            rtl = emit_rtl or (out_dir / "smoke_rtl.v")
            compile_ir(ir_path, rtl, out_dir / "compiler_report.json")
            report["stages"]["compile"] = {"status": "passed", "rtl": str(rtl),
                                           "lines": len(Path(rtl).read_text().splitlines())}
            if verilator is not None:
                from blocks.b7_phase_c.verilog import lint_rtl

                lint = lint_rtl(rtl, verilator=verilator)
                report["stages"]["lint"] = {
                    "status": lint.get("status"), "ok": lint.get("ok"),
                    "errors": list(lint.get("errors") or [])[:6]}
                if not lint.get("ok"):
                    report["status"] = "failed"
                    report["reason"] = "RTL lint"
                    report["error"] = " | ".join(lint.get("errors") or [])[:2000]
                    return report
        report["status"] = "passed"
        return report
    except Exception as exc:  # noqa: BLE001 - every failure is a stage verdict
        report["reason"] = type(exc).__name__
        report["error"] = str(exc)[:2000]
        return report


def load_ir_model(ir_path: Path):
    from Compiler.rtl_compiler.parser import parse_ir

    return parse_ir(ir_path)


def ir_digest(ir_path: Path) -> str:
    from blocks.b1_scaffold.runctx import sha256_file

    return sha256_file(ir_path)
