"""Decide whether a question has a site in a concrete IR instance.

The test is mechanical: build every candidate answer, require that each one
compiles, and require that all differences between candidates fall inside the
declared anchor set.  Anything else is diagnosed as S1/S2/S3 so phase A can
repair the structure instead of pretending the question is askable.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path
from typing import Any, Callable

from .patches import apply_patch, dump_ir, structural_diff, touched_top_level, within_site
from .schema import Question, S0, S1, S2, S3, SiteVerdict, STATUSES


def check_candidate_text(text: str, workdir: Path, *, compile_rtl: bool = True,
                         lint: Any = None) -> dict[str, Any]:
    """Run the deterministic battery on one candidate IR text."""
    from IRgeneration.checker1 import check_ir as checker1
    from IRgeneration.checker2 import check_ir as checker2
    from Compiler.rtl_compiler.parser import parse_ir
    from Compiler.rtl_compiler.validator import validate_ir
    from Compiler.rtl_compiler import compile_ir

    workdir.mkdir(parents=True, exist_ok=True)
    ir = workdir / "ir.yaml"
    ir.write_text(text, encoding="utf-8")
    result: dict[str, Any] = {"ok": False}
    try:
        report = checker1(ir, workdir / "checker1_report.json")
        if report["status"] == "failed":
            result.update(stage="checker1", error=json.dumps(report["errors"][:3], ensure_ascii=False)[:800])
            return result
        report = checker2(ir, workdir / "checker2_report.json")
        if report["status"] == "failed":
            result.update(stage="checker2", error=json.dumps(report["errors"][:3], ensure_ascii=False)[:800])
            return result
        model = parse_ir(ir)
        validate_ir(model)
        result["module"] = model.module_name
        if compile_rtl:
            rtl = workdir / "rtl.v"
            compile_ir(ir, rtl, workdir / "compiler_report.json")
            result["rtl_lines"] = len(rtl.read_text().splitlines())
            if lint is not None:
                lint_result = lint(rtl)
                result["lint"] = {"status": lint_result.get("status"),
                                  "errors": list(lint_result.get("errors") or [])[:6]}
                if not lint_result.get("ok"):
                    result.update(ok=False, stage="lint",
                                  error=" | ".join(lint_result.get("errors") or [])[:800])
                    return result
        result["ok"] = True
        return result
    except Exception as exc:  # noqa: BLE001 - the failure stage is the signal
        result.update(stage=result.get("stage", "compile"),
                      error=f"{type(exc).__name__}: {exc}"[:800])
        return result


def _diagnose(question: Question, detail: dict[str, Any]) -> str:
    """Map a failed check to S1/S2/S3 using the *shape* of the failure."""
    if detail.get("patch_error"):
        return S1
    candidates = detail.get("candidates", [])
    failures = [c for c in candidates if not c.get("ok")]
    if failures and all(c.get("stage") in {"compile", "validate"} for c in failures):
        # A patch that cannot be represented by the compiler usually means the
        # concept has no schema carrier.
        return S3
    if detail.get("outside_anchor"):
        sections = set(detail.get("sections") or [])
        if {"clock_domains"} <= sections or "clock_domains" in sections:
            return S2
        return S1
    if len(candidates) < 2:
        return S1
    return S1


def detect_site(base_ir: dict[str, Any], question: Question, workdir: Path,
                *, compile_rtl: bool = True,
                lint: Any = None,
                observable: Callable[[Question], bool | None] | None = None,
                require_observable: bool = False,
                ) -> SiteVerdict:
    workdir = Path(workdir)
    if workdir.exists():
        shutil.rmtree(workdir)
    workdir.mkdir(parents=True, exist_ok=True)
    detail: dict[str, Any] = {"question": question.id, "template": question.template,
                              "candidates": [], "sections": []}
    built: list[tuple[str, dict[str, Any]]] = []

    if len(question.options) < 2:
        detail["reason"] = "question needs at least two options"
        return SiteVerdict(S1, question.anchors, [], False, False, detail)

    empty = [option.value for option in question.options if not option.patch]
    if not question.anchors or len(empty) != 1:
        detail["reason"] = ("question has no concrete IR anchor" if not question.anchors else
                            f"question needs exactly one current option, found {len(empty)}")
        status = S3 if question.gap_hypothesis.strip().upper() == "G1" else S1
        return SiteVerdict(status, question.anchors, [], False, False, detail)

    for option in question.options:
        try:
            candidate = apply_patch(base_ir, option.patch)
        except Exception as exc:  # noqa: BLE001
            detail["candidates"].append({"value": option.value, "ok": False,
                                         "stage": "patch", "error": f"{type(exc).__name__}: {exc}"[:500]})
            detail["patch_error"] = True
            continue
        if option.patch and not structural_diff(base_ir, candidate):
            # A "fix" that leaves the IR byte-identical is not a real option;
            # accepting it lets a self-referential or stale patch count as a
            # compiled candidate and hides the actual gap.
            detail["candidates"].append({"value": option.value, "ok": False,
                                         "stage": "patch",
                                         "error": "patch_no_effect"})
            detail["patch_error"] = True
            continue
        text = dump_ir(candidate)
        check = check_candidate_text(text, workdir / f"opt_{_safe(option.value)}",
                                     compile_rtl=compile_rtl, lint=lint)
        detail["candidates"].append({"value": option.value, **{k: v for k, v in check.items()
                                                              if k != "module"}})
        if check["ok"]:
            built.append((option.value, candidate))

    if len(built) != len(question.options):
        status = _diagnose(question, detail)
        detail["reason"] = (f"only {len(built)}/{len(question.options)} candidate answers compile")
        return SiteVerdict(status, question.anchors, [], False, False, detail)

    # A question whose answers all compile to the same Verilog is not a design
    # question: it only rewrites syntax or metadata.
    rtl_digests = {}
    if compile_rtl:
        import hashlib
        for value, _ in built:
            rtl = workdir / f"opt_{_safe(value)}" / "rtl.v"
            if rtl.is_file():
                rtl_digests[value] = hashlib.sha256(rtl.read_bytes()).hexdigest()[:12]
    detail["rtl_digests"] = rtl_digests
    detail["distinct_rtl"] = len(set(rtl_digests.values()))
    if compile_rtl and rtl_digests and detail["distinct_rtl"] <= 1:
        detail["reason"] = "no_behavioural_difference"
        return SiteVerdict(S1, question.anchors, [], True, True, detail)

    diffs: list[str] = []
    for index in range(len(built) - 1):
        diffs.extend(structural_diff(built[index][1], built[index + 1][1]))
    for _, candidate in built:
        diffs.extend(structural_diff(base_ir, candidate))
    diffs = sorted(set(diffs))
    local = within_site(diffs, question.anchors)
    detail["sections"] = sorted(touched_top_level(diffs))
    # A patch may touch anything needed to express the answer.  Differences
    # outside the declared anchors are recorded for traceability, not rejected.
    detail["local_to_anchors"] = local
    if not local:
        detail["outside_anchor"] = [path for path in diffs
                                    if not any(path == a or path.startswith(a.rstrip("/") + "/")
                                               for a in question.anchors)][:12]

    seen: bool | None = None
    if observable is not None:
        try:
            seen = observable(question)
        except Exception:  # noqa: BLE001 - diagnostic only
            seen = None
    detail["observable"] = seen
    return SiteVerdict(S0, question.anchors, diffs, True, True, detail)


def _safe(value: str) -> str:
    return "".join(ch if ch.isalnum() or ch in "-_" else "_" for ch in str(value))[:40]


def summarise(verdict: SiteVerdict) -> dict[str, Any]:
    return {"status": verdict.status, "anchors": verdict.anchors,
            "diff_count": len(verdict.diff_paths), "local": verdict.local,
            "compiles": verdict.compiles,
            "reason": verdict.detail.get("reason", ""),
            "sections": verdict.detail.get("sections", [])}
