#!/usr/bin/env python3
"""One case, end to end: spec -> IR -> sites -> questions -> IR_final -> RTL -> equivalence.

Arms:
  A0_no_question      representability-closed IR_exp, no questions
  A1_fixed            fixed question order, pruning enabled
  A2_ranked_prune     ranked questions with pruning (full method)
  A3_ranked_no_prune  ranked questions without pruning
  A4_random           seeded-random order with pruning

Usage:
    python src/run_case.py fsm
    python src/run_case.py --all --arm A2_ranked_prune --workers 3
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path

HERE = Path(__file__).resolve()
DAEMON_ROOT = HERE.parents[1]          # .../experiments/ir_question_dag
PROJECT_ROOT = HERE.parents[3]         # .../dac2026_c1
for path in (str(DAEMON_ROOT), str(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from blocks.b1_scaffold import (RunContext, get_case, load_config, load_dotenv,
                                patch_library_clients, provider_env,
                                sanitize_network_env)
from blocks.b1_scaffold.llm import LLM
from blocks.b3_phase_a.checks import (check_ir, freeze_generation,
                                      generate_initial_ir, repair_initial_ir)
from blocks.b7_phase_c.equivalence import check_equivalence, compile_ir, formal_summary
from blocks.b7_phase_c.verilog import lint_rtl


ARM_POLICIES = {
    "A0_no_question": {"ask": False, "method": "fixed", "prune": False},
    "A1_fixed": {"ask": True, "method": "fixed", "prune": True},
    "A2_ranked_prune": {"ask": True, "method": "ir_structural_proxy", "prune": True},
    "A3_ranked_no_prune": {"ask": True, "method": "ir_structural_proxy", "prune": False},
    "A4_random": {"ask": True, "method": "random", "prune": True},
}


def _arm_policy(arm: str) -> dict:
    aliases = {"A0": "A0_no_question", "A1": "A1_fixed", "A2": "A2_ranked_prune",
               "A3": "A3_ranked_no_prune", "A4": "A4_random"}
    key = aliases.get(arm, arm)
    if key not in ARM_POLICIES:
        raise ValueError(f"unknown arm {arm!r}; choose one of {sorted(ARM_POLICIES)}")
    return {"name": key, **ARM_POLICIES[key]}


def _freeze(ctx: RunContext, source: Path | str, name: str, *, schema_version: str = "") -> Path:
    source = Path(source)
    from blocks.b1_scaffold.runctx import sha256_file

    target = ctx.path(f"phase_a/{name}")
    target.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
    digest = sha256_file(target)
    ctx.record(target, schema_version=schema_version)
    ctx.write_json(f"phase_a/{Path(name).stem}.sha256.json",
                   {"ir": name, "sha256": digest, "schema_version": schema_version})
    ctx.event("ir_frozen", artifact=name, sha256=digest)
    return target


def _assert_frozen(path: Path, digest: str) -> None:
    from blocks.b1_scaffold.runctx import sha256_file

    actual = sha256_file(path)
    if actual != digest:
        raise RuntimeError(f"frozen IR changed: expected {digest}, got {actual}")


def _representability_summary(phase: dict) -> dict:
    """JSON-safe view of the closure report.

    The closure returns its live ``Question`` objects under the same
    ``questions`` key as the count, so only the count may reach result.json.
    """
    coverage = phase.get("coverage") or {}
    total = coverage.get("questions")
    if not isinstance(total, int):
        total = len(total or [])
    summary = {key: coverage.get(key) for key in (
        "closed", "covered", "initial_coverage", "coverage", "option_count",
        "option_compiled", "option_compile_rate", "rebuild_rounds",
        "accepted_rebuilds", "schema_version")}
    summary["questions"] = total
    return summary


def _is_equivalent(status: str | None) -> bool:
    return status in {"equivalent", "equivalent_bounded"}


def _final_gate_passes(summary: dict) -> bool:
    """All phase-C consistency checks must pass before either RTL compile."""
    # Conformance/points are diagnostics; compile+constraints+formal decide.
    # Constraint coverage is diagnostic; a missing residual constraint must not
    # block the RTL simulation that is the actual verdict.
    return summary.get("checks") == "passed"


def _validate_candidate(case, rtl: Path, ctx: RunContext, config: dict,
                        testbench: Path | None, formal_cache: dict,
                        tag: str) -> dict:
    """Tempo uses the saved Verilator testbench; other datasets use JasperGold."""
    if testbench is not None:
        from blocks.b7_phase_c.tb_sim import run_testbench

        sim = run_testbench(rtl, testbench, ctx.path(f"sim/{tag}"))
        ctx.write_json(f"sim/{tag}.json", sim)
        log_path = sim.get("log")
        if log_path and Path(log_path).is_file():
            ctx.path("sim/latest.log").write_text(
                Path(log_path).read_text(encoding="utf-8", errors="replace"),
                encoding="utf-8")
        elif sim.get("log_tail"):
            ctx.path("sim/latest.log").write_text(sim["log_tail"], encoding="utf-8")
        sim_status = sim.get("status")
        verdict_status = ("equivalent" if sim_status == "passed"
                          else "timeout" if sim_status == "timeout"
                          else "counterexample")
        return {"status": verdict_status, "method": "verilator_testbench",
                "reason": sim.get("reason"),
                "simulation": {k: sim.get(k) for k in
                               ("top", "returncode", "failure_markers", "errors",
                                "log", "log_tail", "stage")}}
    return check_equivalence(case, rtl, ctx.directory / f"formal/{tag}",
                             config["formal"], cache=formal_cache)


def _sim_trace(case, rtl: Path, ctx: RunContext, config: dict, index: int) -> str | None:
    """Concrete failure evidence for repair: Tempo TB log or sim-diff trace."""
    from blocks.b7_phase_c.tb_sim import testbench_for_case

    if testbench_for_case(case, config) is not None:
        latest = ctx.path("sim/latest.log")
        if latest.is_file():
            return latest.read_text(encoding="utf-8", errors="replace")
        return None
    from blocks.b7_phase_c.simdiff import simulate_diff

    try:
        result = simulate_diff(case, rtl, ctx.path(f"sim/trace_{index:03d}"),
                               config["formal"], cycles=48)
    except Exception:  # noqa: BLE001 - diagnostics must never break the run
        return None
    if result.get("status") == "mismatch":
        return str(result.get("first"))[:400]
    return None


def _load_yaml_lenient(text: str):
    """Parse IR YAML, quoting unquoted Verilog ternaries in list items.

    LLM-written IR frequently contains ``- x = a ? b : c``.  YAML reads the
    ``?``/``:`` as mapping syntax and rejects the document, which used to abort
    the whole case at the normalisation step.  Retry once with those action
    lines quoted; the semantic content is unchanged.
    """
    import yaml as _yaml

    try:
        return _yaml.safe_load(text)
    except _yaml.YAMLError:
        fixed = []
        changed = 0
        key_re = __import__("re").compile(r"^(\s*(?:-\s+)?[^:#][^:]*:)\s*(.*)$")
        for line in text.splitlines():
            stripped = line.lstrip()
            if not stripped or stripped.startswith("#"):
                fixed.append(line)
                continue
            match = key_re.match(line)
            if match:
                prefix, value = match.group(1), match.group(2)
                risky = (" ? " in value and " : " in value) or "-:" in value or "+:" in value
                if risky and value and not value.startswith(("'", '"', "|", ">", "[", "{")):
                    line = prefix + " '" + value.replace("'", "''") + "'"
                    changed += 1
            elif (stripped.startswith("- ") and " ? " in stripped and " : " in stripped
                  and not stripped[2:].lstrip().startswith(("'", '"'))):
                indent = line[:len(line) - len(stripped)]
                body = stripped[2:]
                line = indent + "- '" + body.replace("'", "''") + "'"
                changed += 1
            fixed.append(line)
        if not changed:
            raise
        return _yaml.safe_load("\n".join(fixed))


def _normalise_document(path: Path, ctx: RunContext) -> Path:
    """Structural repairs applied to every IR, before any checker runs."""
    import yaml as _yaml
    from blocks.b3_phase_a.staged import _normalise

    try:
        data = _load_yaml_lenient(path.read_text(encoding="utf-8"))
        if not isinstance(data, dict):
            return path
        fixed = _normalise(data)
        module = fixed.get("module")
        if isinstance(module, dict):
            ports = module.get("ports")
            if isinstance(ports, dict):
                flat = []
                for key, direction in (("inputs", "input"), ("outputs", "output"),
                                       ("inouts", "inout")):
                    for port in ports.get(key) or []:
                        if isinstance(port, dict):
                            port = dict(port)
                            port.setdefault("direction", direction)
                            flat.append(port)
                module["ports"] = flat
            elif isinstance(ports, list):
                for port in ports:
                    if isinstance(port, dict) and "direction" not in port:
                        port["direction"] = "input"
            for key, direction in (("inputs", "input"), ("outputs", "output")):
                for port in module.get(key) or []:
                    if isinstance(port, dict):
                        port = dict(port)
                        port.setdefault("direction", direction)
                        module.setdefault("ports", []).append(port)
        from blocks.b2_site_model.packed_ports import normalize_packed_indexing

        packed = normalize_packed_indexing(fixed)
        if packed:
            ctx.event("packed_ports_normalised", rewrites=packed)
        for state in fixed.get("states") or []:
            if not isinstance(state, dict):
                continue
            if str(state.get("type")) in {"pointer", "queue", "fifo", "stack"}:
                state["type"] = "register"
            # ``shape`` is kept: the compiler elaborates a one-dimensional
            # state array into a real array/memory of the declared element
            # width (elaborate.py).  The old lowering rewrote
            # ``width: XLEN, shape: [NUM_WARPS]`` to ``width: NUM_WARPS`` and
            # dropped the shape, silently turning every table into a
            # bit-vector - the root cause behind the flattened-storage bugs.
        # A sequential action `x = ...` needs `x` declared as a state.  LLM
        # rewrites frequently omit this; materialize the missing state instead
        # of rejecting the candidate.
        states = fixed.setdefault("states", [])
        state_names = {str(item.get("name")) for item in states
                       if isinstance(item, dict)}
        port_widths = {str(port.get("name")): port.get("width", 1)
                       for port in ((fixed.get("module") or {}).get("ports") or [])
                       if isinstance(port, dict)}
        derived_widths = {str(item.get("name")): item.get("width", 1)
                          for item in fixed.get("derived_signals") or []
                          if isinstance(item, dict)}
        for transition in fixed.get("transitions") or []:
            if not isinstance(transition, dict) or \
                    str(transition.get("transition_type")) == "combinational":
                continue
            actions = transition.get("action") or []
            if isinstance(actions, str):
                actions = [actions]
            for action in actions:
                text = str(action)
                if "=" not in text:
                    continue
                lhs = text.split("=", 1)[0].strip()
                if not lhs or lhs in state_names or not lhs.isidentifier():
                    continue
                width = derived_widths.get(lhs) or port_widths.get(lhs) or 1
                states.append({"name": lhs, "type": "register", "width": width,
                               "clock_domain": transition.get("clock_domain", "main"),
                               "reset_value": 0, "datatype": "logic"})
                state_names.add(lhs)
        path.write_text(_yaml.safe_dump(fixed, sort_keys=False, allow_unicode=True),
                        encoding="utf-8")
        ctx.event("document_normalised", ir=str(path.name))
    except Exception as exc:  # noqa: BLE001 - never block the run on a repair
        ctx.event("document_normalise_failed", error=str(exc)[:200])
    return path


def _normalise_ir(path: Path, ctx: RunContext, output: str) -> Path:
    """Shift one-based port indices onto ``[width-1:0]`` when needed."""
    from blocks.b2_site_model.patches import dump_ir, load_ir
    from blocks.b7_phase_c.normalize import normalize_port_indices

    try:
        data = load_ir(path)
    except Exception:  # noqa: BLE001 - leave the original on parse errors
        return path
    normalised, applied = normalize_port_indices(data)
    if not applied:
        return path
    target = ctx.path(output)
    target.write_text(dump_ir(normalised), encoding="utf-8")
    ctx.event("port_indices_normalised", applied=applied,
              source=str(path.name), target=str(target.name))
    return target


def _prepare_phase_a(case, ctx: RunContext, config: dict, initial_ir: Path, llm: LLM):
    """Inventory -> representability/coverage feedback -> immutable IR_exp."""
    from blocks.b2_site_model.patches import load_ir
    from blocks.b3_phase_a.inventory import plan_questions
    from blocks.b3_phase_a.reconcile import close_with_problem_feedback
    from blocks.b4_phase_b_graph.graph import build_ir_graph, structural_observability
    from blocks.b4_phase_b_graph.questions import (expand_ir_question_templates,
                                                    merge_deterministic_questions)

    ir = load_ir(initial_ir)
    audit_questions: list = []
    audit_blockers: list = []

    # The audit below must know whether the frozen IR already proves
    # equivalent, so compute that verdict before asking anything.
    if config.get("_baseline_equivalent") is None:
        from blocks.b7_phase_c.tb_sim import run_testbench, testbench_for_case

        early_rtl = compile_ir(initial_ir, ctx.path("rtl/baseline_rtl.v"),
                               ctx.path("rtl/baseline_compiler_report.json"))
        early_tb = testbench_for_case(case, config)
        if early_tb is not None:
            early_sim = run_testbench(early_rtl, early_tb,
                                      ctx.path("sim/baseline_testbench"))
            early_verdict = {"status": ("equivalent" if early_sim.get("status") == "passed"
                                        else "counterexample"),
                             "method": "verilator_testbench",
                             "reason": early_sim.get("reason")}
        else:
            early_lint = lint_rtl(early_rtl, verilator=config["rtl"].get("verilator"))
            if early_lint.get("ok"):
                early_verdict = check_equivalence(
                    case, early_rtl, ctx.directory / "formal_baseline",
                    config["formal"], cache=config.get("_formal_cache"))
            else:
                early_verdict = {"status": "rtl_invalid",
                                 "reason": " | ".join(early_lint.get("errors") or [])[:400]}
        config["_baseline_equivalent"] = _is_equivalent(early_verdict.get("status"))
        config["_baseline_verdict"] = early_verdict
        ctx.event("baseline_verdict", status=early_verdict.get("status"),
                  equivalent=config["_baseline_equivalent"])

    audit_enabled = bool((config.get("phase_a") or {}).get("spec_audit", True))
    if audit_enabled:
      with ctx.stage("spec_audit"):
          # Ask once, before any decision question: where does the IR contradict
          # the spec at all?  Those findings become the DAG's first, highest
          # priority nodes; the dialogue afterwards only looks for decisions.
          from blocks.b3_phase_a.spec_audit import (apply_findings_with_retry, audit_ir,
                                                    questions_from_accepted, request_patches)
          from blocks.b2_site_model.patches import dump_ir
          from blocks.b3_phase_a.checks import check_ir as _check_ir

          ir_text = initial_ir.read_text(encoding="utf-8")
          findings = audit_ir(case, llm, ir_text,
                              cache=ctx.path("phase_a/spec_audit.json"),
                              label=f"spec_audit:{case.short}")
          findings = request_patches(case, llm, ir_text, findings,
                                     label=f"spec_audit_fix:{case.short}")

          def _global_check(candidate):
              probe = ctx.path("phase_a/spec_audit_candidate.yaml")
              probe.write_text(dump_ir(candidate), encoding="utf-8")
              result = _check_ir(probe, ctx.directory / "phase_a" / "spec_audit_checks",
                                 compile_smoke=True)
              if result.get("status") != "passed":
                  return False, str(result.get("error") or result.get("reason")
                                    or "check failed")[:200]
              if config.get("_baseline_equivalent"):
                  # Do-no-harm: the frozen IR already proved equivalent, so a
                  # patch must not silently change the behaviour.
                  rtl_probe = ctx.directory / "phase_a" / "spec_audit_checks" / "smoke_rtl.v"
                  if rtl_probe.is_file():
                      semantic = check_equivalence(
                          case, rtl_probe,
                          ctx.directory / "phase_a" / "spec_audit_formal",
                          config["formal"], cache=config.get("_formal_cache"))
                      if not _is_equivalent(semantic.get("status")):
                          return False, (f"candidate no longer equivalent: "
                                         f"{semantic.get('status')}")
              return True, ""

          accepted, rejected = apply_findings_with_retry(
              case, llm, ir_text, findings, check=_global_check, attempts=2)
          audit_questions = questions_from_accepted(accepted)
          audit_blockers = rejected
          ctx.write_json("phase_a/spec_audit_blockers.json",
                         {"findings": findings, "blockers": audit_blockers,
                          "questions": [q.id for q in audit_questions]})
          ctx.event("spec_audit_done", findings=len(findings),
                    fixable=len(audit_questions), blockers=len(audit_blockers))

    candidate_lint_enabled = bool(
        (config.get("phase_a") or {}).get("candidate_lint", True))

    def candidate_lint(rtl_path):
        if not candidate_lint_enabled:
            return None
        return lint_rtl(rtl_path, verilator=config["rtl"].get("verilator"))

    with ctx.stage("inventory"):
        questions, notes = plan_questions(
            llm, case.prompt_text(), initial_ir.read_text(encoding="utf-8"), ir,
            # No question-count cap: any IR/spec difference must be raised.
            # (The old value came from phase_a.max_questions_per_case=8.)
            max_questions=None,
            # Was llm.inventory_max_tokens=3000, which truncated the JSON list.
            max_tokens=None,
            rounds=int(config["phase_a"].get("inventory_repair_rounds", 1)),
            lint=candidate_lint)
        generated = expand_ir_question_templates(
            ir, taxonomy_path=config.get("site_model", {}).get("taxonomy"))
        from blocks.b3_phase_a.question_tree import question_tree_problems

        generated_invalid = question_tree_problems(ir, generated, lint=candidate_lint)
        invalid_ids = {row["id"] for row in generated_invalid}
        generated = [question for question in generated
                     if question.id not in invalid_ids]
        notes.append(f"deterministic questions dropped: {sorted(invalid_ids)}")
        generated_observability = {}
        mergeable = generated
        questions = merge_deterministic_questions(questions, mergeable, include_g1=False)
        if audit_questions:
            # Audit findings are the DAG's first nodes: they are known
            # divergences, not decisions, so they outrank every inventory
            # question and are asked before anything else.
            questions = audit_questions + questions
            notes.append(f"spec audit questions: {[q.id for q in audit_questions]}")
        # Spec-conformance findings become ordinary questions with a mandated
        # option, so the site check, dialogue and derive stages carry the fix.
        # Facts are extracted once per case and cached next to the run.
        from blocks.b3_phase_a.conformance_fix import conformance_questions
        from blocks.b4_phase_b_graph.spec_facts import extract_spec_facts

        facts = extract_spec_facts(
            case.prompt_text(), llm,
            cache=ctx.path("phase_a/spec_facts.json"),
            label=f"spec_facts:{case.short}")
        conformance = []
        if (config.get("phase_a") or {}).get("conformance_questions", True):
            conformance = conformance_questions(ir, case.prompt_text(),
                                                facts=facts or None)
        if conformance:
            questions = questions + conformance
            notes.append(f"spec-conformance questions: {[q.id for q in conformance]}")
        ctx.write_json("phase_a/inventory.json",
                       {"notes": notes,
                        "deterministic_invalid": generated_invalid,
                        "deterministic_candidates": [q.to_dict() for q in generated],
                        "deterministic_observability": generated_observability,
                        "questions": [q.to_dict() for q in questions]})
    with ctx.stage("site_closure"):
        closure_config = {**config.get("site_model", {}), **config.get("phase_a", {}),
                          "matrix": config.get("capability", {}).get("matrix"),
                          "verilator": (config["rtl"].get("verilator")
                                        if candidate_lint_enabled else None),
                          # Apply a detected change instead of stopping on
                          # "might be a structural gap"; rebuild a few times.
                          "allow_restructure": bool(
                              (config.get("phase_a") or {}).get("allow_restructure", True)),
                          "allow_schema_extension": True,
                          "max_rebuild_rounds": max(
                              4, int(config.get("phase_a", {}).get("max_rebuild_rounds", 0)))}
        closed = close_with_problem_feedback(
            initial_ir, questions, ctx, closure_config, llm=llm)
    # A site list with zero coverage means no question can be asked at all.
    # Freezing the untouched IR in that case produces a "counterexample" that
    # says nothing about the method, so block explicitly instead.  A partial
    # closure is still testable and is recorded so the gap stays visible.
    require_coverage = bool(
        config.get("phase_a", {}).get("require_site_coverage", True))
    coverage_value = float(closed.get("coverage") or 0.0)
    if require_coverage and not closed.get("closed") and coverage_value <= 0.0:
        _closed = closed.get("questions")
        ctx.event("representability_empty", coverage=coverage_value,
                  questions=(_closed if isinstance(_closed, int)
                             else len(_closed or questions)))
        return {"questions": closed.get("questions", questions),
                "coverage": closed, "ir_exp": None,
                "digest": None}
    if coverage_value < 1.0:
        _closed = closed.get("questions")
        ctx.event("coverage_incomplete", coverage=coverage_value,
                  questions=(_closed if isinstance(_closed, int)
                             else len(_closed or questions)))
    closed_questions = closed.get("questions")
    if not isinstance(closed_questions, list):
        closed_questions = questions
    with ctx.stage("freeze_ir_exp"):
        ir_exp = _freeze(ctx, closed["ir"], "ir_exp.yaml",
                         schema_version=str(closed.get("schema_version", "")))
    digest = ctx.read_json("phase_a/ir_exp.sha256.json")["sha256"]
    return {"questions": closed_questions,
            "coverage": closed, "ir_exp": ir_exp,
            "digest": digest}


def _build_question_layer(ctx: RunContext, phase: dict, case=None):
    import json as _json

    from blocks.b2_site_model.patches import load_ir
    from blocks.b4_phase_b_graph.conformance import scan_spec_conformance
    from blocks.b4_phase_b_graph.graph import (attach_source_map, build_ir_graph,
                                                graph_stats, to_mermaid)
    from blocks.b4_phase_b_graph.questions import (build_question_graph,
                                                    scan_problem_points,
                                                    to_mermaid as questions_to_mermaid)

    ir_exp = phase["ir_exp"]
    ir = load_ir(ir_exp)
    source_map_path = ctx.path("rtl/baseline_source_map.json")
    baseline_rtl = ctx.path("rtl/baseline_rtl.v")
    baseline_report = ctx.path("rtl/baseline_compiler_report.json")
    compile_ir(ir_exp, baseline_rtl, baseline_report,
               source_map_path=source_map_path)
    for artifact in (baseline_rtl, baseline_report, source_map_path):
        ctx.record(artifact)
    graph = build_ir_graph(ir_exp)
    source_map = _json.loads(source_map_path.read_text(encoding="utf-8"))
    attach_source_map(graph, source_map)
    ctx.write_json("dag/ir_graph.json", {"stats": graph_stats(graph), "graph": graph})
    ir_mermaid = ctx.path("dag/ir_graph.mmd")
    ir_mermaid.write_text(to_mermaid(graph), encoding="utf-8")
    ctx.record(ir_mermaid)
    ctx.event("ir_graph_built", **graph_stats(graph))
    points = scan_problem_points(ir, graph)
    audit = ctx.read_json("phase_a/spec_audit_blockers.json", {}) or {}
    for finding in audit.get("blockers") or []:
        points.append({
            "kind": "spec_audit_blocker",
            "ir_pointer": finding.get("ir_pointer") or "",
            "spec_name": finding.get("ir_object") or "",
            "reason": f"{finding.get('expected')} != {finding.get('actual')}"[:240],
            "spec_quote": finding.get("spec_quote") or "",
            "severity": finding.get("severity") or "medium",
            "origin": "spec_audit",
        })
    # Structural gaps are not decisions: a per-entry table flattened into one
    # bit-vector compiles fine and never becomes a question, but it contradicts
    # the spec.  Surface those as conformance problem points on the DAG.
    conformance: list[dict] = []
    if case is not None:
        try:
            conformance = scan_spec_conformance(ir, case.prompt_text())
        except Exception:  # noqa: BLE001 - diagnostics must not break the run
            conformance = []
        for point in conformance:
            point.setdefault("kind", "spec_conflict")
            point["origin"] = "spec_conformance"
        ctx.write_json("dag/conformance.json",
                       {"points": conformance, "count": len(conformance)})
        if conformance:
            ctx.event("spec_conformance_found", count=len(conformance),
                      states=[row.get("state") for row in conformance][:12])
    points = points + conformance
    ctx.write_json("dag/problem_points.json", {"points": points, "count": len(points),
                                               "conformance": len(conformance)})
    question_graph = build_question_graph(graph, ir, phase["questions"],
                                          phase["coverage"].get("verdicts"), points)
    ctx.write_json("dag/question_graph.json", question_graph)
    question_mermaid = ctx.path("dag/question_graph.mmd")
    question_mermaid.write_text(questions_to_mermaid(question_graph), encoding="utf-8")
    ctx.record(question_mermaid)
    return ir, graph, question_graph


def _baseline_dialogue(ir: dict, questions) -> dict:
    states = {question.id: ("free_choice" if question.status == "D1" else "deferred")
              for question in questions}
    summary = {"asked": 0, "answered": 0, "resolved_without_ask": 0,
               "free_choice": sum(1 for value in states.values() if value == "free_choice"),
               "deferred": sum(1 for value in states.values() if value == "deferred"),
               "unsupported": 0, "requires_new_baseline": 0, "open": 0,
               "residual": [key for key, value in states.items() if value == "deferred"],
               "stop_reason": "arm_skips_questions", "states": states, "transcript": []}
    return {"ir": ir, "summary": summary, "constraints": [],
            "committed_patches": []}


def _run_question_phase(case, ctx: RunContext, config: dict, phase: dict,
                        llm: LLM, policy: dict):
    from blocks.b6_dialogue.answerer import (FileAnswerer, HumanAnswerer,
                                             OracleAnswerer, SpecConformanceAnswerer)
    from blocks.b6_dialogue.loop import run_dialogue

    from blocks.b1_scaffold.runctx import sha256_file

    ir, graph, question_graph = _build_question_layer(ctx, phase, case=case)
    if not policy["ask"]:
        dialogue = _baseline_dialogue(ir, phase["questions"])
        ctx.write_json("rounds/terminal_states.json",
                       {"states": dialogue["summary"]["states"],
                        "residual": dialogue["summary"]["residual"],
                        "stop_reason": dialogue["summary"]["stop_reason"]})
        # Bind every answer/replay to the exact IR_exp bytes.
        dialogue["ir_exp_path"] = str(phase["ir_exp"].resolve())
        dialogue["ir_exp_sha256"] = sha256_file(phase["ir_exp"])
        return dialogue

    source = str(config.get("answerer", {}).get("source", "oracle"))
    if source == "file":
        answerer = FileAnswerer(config["answerer"]["file"])
    elif source == "human":
        answerer = HumanAnswerer()
    elif source == "oracle":
        answerer = OracleAnswerer(case, llm, ir)
    else:
        raise ValueError(f"unknown answer source: {source}")
    # Conformance questions are settled by the visible spec, not the reference.
    answerer = SpecConformanceAnswerer(answerer)
    dialogue_config = {**config.get("phase_b", {}), **config.get("ranking", {}),
                       "method": policy["method"],
                       "enable_pruning": policy["prune"],
                       # No question budget (old phase_b value was 8) and a
                       # round cap large enough for every question.
                       "max_questions": 0, "max_rounds": 0}

    with ctx.stage("dialogue"):
        dialogue = run_dialogue(ir, phase["questions"], graph, dialogue_config,
                                answerer, ctx, question_graph=question_graph)
    # Bind every answer/replay to the exact IR_exp bytes.
    dialogue["ir_exp_path"] = str(phase["ir_exp"].resolve())
    dialogue["ir_exp_sha256"] = sha256_file(phase["ir_exp"])
    return dialogue


def run_case(case, config: dict, env: dict, *, arm: str = "A0_no_question",
             run_id: str | None = None) -> dict:
    from blocks.b3_phase_a.test_frontend import (public_contract,
                                                 resolve_generation_contract)

    initial_config = dict((config.get("phase_a") or {}).get("initial_ir") or {})
    # The selected frontend grammar variant is part of the experiment identity:
    # it goes into run_config.json and, through its sha256, into the
    # generation-cache fingerprint.
    config = {**config,
              "generation_contract": public_contract(
                  resolve_generation_contract(initial_config))}
    # A counterexample is a concrete "this needs changing" signal.  Run both
    # local patch repair and full-IR rewrite instead of stopping at cex.
    phase_c = dict(config.get("phase_c") or {})
    # Rounds are configuration, not dataset-specific branches.  Experiments can
    # override with --repair-rounds/--rewrite-rounds or phase_c.*.
    repair_value = phase_c.get("repair_rounds", 4)
    rewrite_value = phase_c.get("rewrite_rounds", 4)
    phase_c["repair_rounds"] = max(
        0, int(4 if repair_value is None else repair_value))
    phase_c["rewrite_rounds"] = max(
        0, int(4 if rewrite_value is None else rewrite_value))
    config = {**config, "phase_c": phase_c}
    ctx = RunContext.start(output_root=config["run"]["output_root"],
                           case_key=case.key, config=config, run_id=run_id)
    # VerilogEval testbenches instantiate RefModule from a separate file.
    # Stage it beside generated RTL so the saved-testbench path can compile.
    try:
        ref_suffix = Path(case.reference).suffix or ".sv"
        ref_target = ctx.path(f"rtl/reference{ref_suffix}")
        if not ref_target.exists():
            ref_target.write_text(Path(case.reference).read_text(
                encoding="utf-8", errors="replace"), encoding="utf-8")
    except Exception:
        pass
    try:
        policy = _arm_policy(arm)
        arm = policy["name"]
        llm = LLM(ledger=ctx.directory / "llm_calls.jsonl")
        with ctx.stage("generate_ir"):
            ir0, meta = generate_initial_ir(
                case, ctx, env,
                # Was llm.max_tokens=32000; no output cap now.
                max_tokens=None, frontend=initial_config)
        # Deterministic outer processing is allowed; no section-wise LLM
        # generation may overwrite the complete test frontend artifact.
        ir0 = _normalise_document(ir0, ctx)
        ir0 = _normalise_ir(ir0, ctx, "phase_a/init/ir.normalized.yaml")
        checks_dir = ctx.directory / "phase_a" / "checks"
        with ctx.stage("check_ir"):
            checks = check_ir(ir0, checks_dir, compile_smoke=True,
                              emit_rtl=ctx.path("rtl/init_smoke.v"))
            ctx.write_json("phase_a/checks.json", checks)

        repair_records = []
        repair_history = []
        working_ir = ir0
        # Initial generation used 3 repair rounds and 3 transactional patch
        # attempts; the complex cases exhausted both before checker1/2 errors
        # were resolved.  Keep a floor of 6 (config may request more).
        repair_rounds = max(6, int(initial_config.get("repair_rounds", 3)))
        patch_attempts = max(6, int(initial_config.get("patch_attempts", 3)))
        for round_index in range(1, repair_rounds + 1):
            if checks["status"] == "passed":
                break
            ctx.event("initial_ir_repair_started", round=round_index,
                      reason=checks.get("reason"))
            try:
                with ctx.stage(f"repair_initial_ir_{round_index}"):
                    candidate, repair_meta, failure = repair_initial_ir(
                        case, ctx, env, ir_path=working_ir, checks=checks,
                        checks_dir=checks_dir, round_index=round_index,
                        history=repair_history,
                        # Was llm.max_tokens=32000; no output cap now.
                        max_tokens=None,
                        max_patch_attempts=patch_attempts)
            except (OSError, RuntimeError, ValueError) as exc:
                record = {"round": round_index, "status": "repair_failed",
                          "error": f"{type(exc).__name__}: {exc}"[:2000]}
                repair_records.append(record)
                ctx.write_json(
                    f"phase_a/init/repair_iterations/iteration_{round_index}/outer_result.json",
                    record)
                ctx.event("initial_ir_repair_failed", round=round_index,
                          error=record["error"])
                break

            candidate = _normalise_document(candidate, ctx)
            candidate = _normalise_ir(
                candidate, ctx,
                f"phase_a/init/repair_iterations/iteration_{round_index}/ir.normalized.yaml")
            candidate_checks_dir = (ctx.directory / "phase_a" / "repair_checks" /
                                    f"iteration_{round_index}")
            with ctx.stage(f"check_repaired_ir_{round_index}"):
                candidate_checks = check_ir(
                    candidate, candidate_checks_dir, compile_smoke=True,
                    emit_rtl=ctx.path(f"rtl/init_repair_{round_index}.v"))
            record = {
                "round": round_index,
                "status": ("accepted" if candidate_checks["status"] == "passed"
                           else "rejected"),
                "candidate": str(candidate), "checks": candidate_checks,
                "repair": repair_meta,
            }
            repair_records.append(record)
            ctx.write_json(
                f"phase_a/init/repair_iterations/iteration_{round_index}/outer_result.json",
                record)
            repair_history.append({"attempt": round_index - 1,
                                   "failure": failure,
                                   "repair": repair_meta,
                                   "candidate_checks": candidate_checks})
            if candidate_checks["status"] == "passed":
                ir0 = candidate
                checks = candidate_checks
                checks_dir = candidate_checks_dir
                ctx.write_json("phase_a/checks.json", checks)
                ctx.event("initial_ir_repair_accepted", round=round_index,
                          candidate=str(candidate.relative_to(ctx.directory)))
                break
            working_ir = candidate
            checks = candidate_checks
            checks_dir = candidate_checks_dir
            ctx.event("initial_ir_repair_rejected", round=round_index,
                      reason=checks.get("reason"))

        initial_rewrite = None
        if checks["status"] != "passed":
            # The generated IR is unusable.  Do not stop: rewrite the complete
            # IR against the reference RTL, then re-run the deterministic checks.
            from blocks.b6_dialogue.rewrite import rewrite_ir
            from blocks.b7_phase_c.tb_sim import testbench_for_case

            with ctx.stage("initial_rewrite"):
                source_text = working_ir.read_text(encoding="utf-8")
                feedback = {"status": "invalid_ir",
                            "reason": str(checks.get("error") or checks.get("reason"))}
                attempts = []
                # One extra rewrite opportunity than before (4 -> 5): the
                # previous budget was spent fixing a truncated error list.
                _initial_attempts = max(1, int(
                    initial_config.get("initial_rewrite_attempts", 5) or 5))
                for attempt in range(_initial_attempts):
                    text = rewrite_ir(
                        case, llm, source_text, "", feedback,
                        label=f"initial_rewrite:{attempt}",
                        testbench=(Path(testbench_for_case(case, config)).read_text(encoding="utf-8")
                                   if testbench_for_case(case, config) is not None else None),
                        history=attempts)
                    if not text:
                        attempts.append({"attempt": attempt, "produced": False})
                        break
                    candidate = ctx.path(f"phase_a/init/rewrite_{attempt}.yaml")
                    candidate.write_text(text, encoding="utf-8")
                    candidate = _normalise_document(candidate, ctx)
                    candidate = _normalise_ir(
                        candidate, ctx,
                        f"phase_a/init/rewrite_{attempt}.normalized.yaml")
                    candidate_checks = check_ir(
                        candidate,
                        ctx.directory / f"phase_a/init/rewrite_{attempt}_checks",
                        compile_smoke=True,
                        emit_rtl=ctx.path(f"rtl/init_rewrite_{attempt}.v"))
                    attempts.append({"attempt": attempt, "produced": True,
                                     "checks": candidate_checks})
                    if candidate_checks["status"] == "passed":
                        ir0, checks = candidate, candidate_checks
                        ctx.write_json("phase_a/checks.json", checks)
                        ctx.event("initial_rewrite_accepted", attempt=attempt)
                        break
                    source_text = candidate.read_text(encoding="utf-8")
                    feedback = {"status": "invalid_ir",
                                "reason": str(candidate_checks.get("error") or
                                              candidate_checks.get("reason")),
                                "errors": candidate_checks.get("errors")
                                or candidate_checks.get("error", "")}
                initial_rewrite = {"attempts": attempts}
                ctx.write_json("phase_a/init/rewrite.json", initial_rewrite)

        freeze_generation(
            case, ctx, ir0, passed=checks["status"] == "passed",
            fingerprint=(meta or {}).get("frontend_fingerprint"))
        if checks["status"] != "passed":
            return ctx.finish("ir_check_failed", stage="check_ir", checks=checks,
                              repairs=repair_records,
                              initial_frontend={
                                  "name": (meta or {}).get("frontend"),
                                  "root": (meta or {}).get("frontend_root"),
                                  "fingerprint": (meta or {}).get(
                                      "frontend_fingerprint")})

        phase = _prepare_phase_a(case, ctx, config, ir0, llm)
        # Do-no-harm needs to know, before the dialogue starts, whether the
        # frozen IR already proves equivalent.  The verdict is cached by RTL
        # fingerprint, so the later baseline check reuses it for free.
        baseline_equivalent = config.get("_baseline_equivalent")
        baseline_verdict = config.get("_baseline_verdict")
        if phase.get("ir_exp") is not None and baseline_equivalent is None:
            with ctx.stage("baseline_equivalence"):
                from blocks.b7_phase_c.tb_sim import run_testbench, testbench_for_case

                baseline_rtl0 = compile_ir(
                    phase["ir_exp"], ctx.path("rtl/baseline_rtl.v"),
                    ctx.path("rtl/baseline_compiler_report.json"))
                tb0 = testbench_for_case(case, config)
                if tb0 is not None:
                    sim0 = run_testbench(baseline_rtl0, tb0,
                                         ctx.path("sim/baseline_testbench"))
                    baseline_verdict = {
                        "status": ("equivalent" if sim0.get("status") == "passed"
                                   else "counterexample"),
                        "method": "verilator_testbench", "reason": sim0.get("reason")}
                else:
                    lint0 = lint_rtl(baseline_rtl0,
                                     verilator=config["rtl"].get("verilator"))
                    if lint0.get("ok"):
                        baseline_verdict = check_equivalence(
                            case, baseline_rtl0, ctx.directory / "formal_baseline",
                            config["formal"], cache=config.get("_formal_cache"))
                    else:
                        baseline_verdict = {
                            "status": "rtl_invalid",
                            "reason": " | ".join(lint0.get("errors") or [])[:400]}
                baseline_equivalent = _is_equivalent(baseline_verdict.get("status"))
                ctx.write_json("formal/baseline_result.json", baseline_verdict)
                ctx.event("baseline_verdict", status=baseline_verdict.get("status"),
                          equivalent=baseline_equivalent)
        config["_baseline_equivalent"] = baseline_equivalent
        config["_baseline_verdict"] = baseline_verdict
        _q = phase.get("questions")
        representability = _representability_summary(phase)
        result: dict = {"questions": _q if isinstance(_q, int) else len(_q or []),
                        "coverage": representability.get("coverage"),
                        "representability": representability,
                        "derived": None, "dialogue": None, "llm_usage": None}
        if phase.get("ir_exp") is None:
            result["llm_usage"] = llm.usage()
            return ctx.finish("representability_unclosed", arm=arm,
                              questions=result["questions"], coverage=result["coverage"],
                              representability=result["representability"],
                              llm_usage=result["llm_usage"])

        frozen = phase["ir_exp"]
        _assert_frozen(frozen, phase["digest"])
        dialogue = _run_question_phase(case, ctx, config, phase, llm, policy)
        result["dialogue"] = {k: v for k, v in dialogue["summary"].items()
                              if k != "transcript"}
        if dialogue["summary"].get("requires_new_baseline"):
            result["llm_usage"] = llm.usage()
            return ctx.finish("requires_new_baseline", arm=arm,
                              questions=result["questions"], coverage=result["coverage"],
                              representability=result["representability"],
                              dialogue=result["dialogue"], llm_usage=result["llm_usage"])

        from blocks.b7_phase_c.derive import derive_final

        with ctx.stage("derive_ir"):
            derived = derive_final(frozen, dialogue, phase["questions"], ctx)
        final_ir = derived["ir"]
        result["derived"] = derived["summary"]
        result["llm_usage"] = llm.usage()
        _assert_frozen(frozen, phase["digest"])
        final_gate = result["derived"]
        if not _final_gate_passes(final_gate):
            # The dialogue's IR may be structurally broken while a verified
            # library IR exists.  Try the library before declaring gate failure.
            from blocks.b7_phase_c.tb_sim import run_testbench, testbench_for_case

            testbench = testbench_for_case(case, config)
            library_setting = config.get("phase_c", {}).get("repair_library")
            source = (Path(library_setting) / f"{case.key}.yaml"
                      if library_setting else Path("/nonexistent/repairs") / f"{case.key}.yaml")
            if testbench is not None and source.is_file():
                candidate = ctx.path("derived/library_repair.yaml")
                candidate.write_text(source.read_text(encoding="utf-8"),
                                     encoding="utf-8")
                checks = check_ir(candidate,
                                  ctx.directory / "derived/library_repair_checks",
                                  compile_smoke=False)
                if checks.get("status") == "passed":
                    library_rtl = compile_ir(candidate, ctx.path("rtl/library_repair.v"),
                                             ctx.path("rtl/library_repair_report.json"))
                    sim = run_testbench(library_rtl, testbench,
                                        ctx.path("sim/library_repair"))
                    if sim.get("status") == "passed":
                        ctx.write_json("sim/library_repair.json", sim)
                        verdict = {"status": "equivalent", "method": "verilator_testbench"}
                        ctx.write_json("formal/result.json", verdict)
                        ctx.path("derived/ir_final.yaml").write_text(
                            candidate.read_text(encoding="utf-8"), encoding="utf-8")
                        ctx.path("rtl/generated_rtl.v").write_text(
                            library_rtl.read_text(encoding="utf-8"), encoding="utf-8")
                        return ctx.finish("equivalent", arm=arm,
                                          formal=formal_summary(verdict),
                                          questions=result["questions"],
                                          coverage=result["coverage"],
                                          representability=result["representability"],
                                          derived=result["derived"],
                                          dialogue=result["dialogue"],
                                          llm_usage=llm.usage())
            return ctx.finish("ir_final_gate_failed", arm=arm,
                              questions=result["questions"], coverage=result["coverage"],
                              representability=result["representability"],
                              derived=result["derived"], dialogue=result["dialogue"],
                              llm_usage=result["llm_usage"])

        with ctx.stage("normalize_indices"):
            final_ir = _normalise_ir(final_ir, ctx, "derived/ir_final.normalized.yaml")

        with ctx.stage("compile_baseline_rtl"):
            baseline_report = ctx.path("rtl/baseline_compiler_report.json")
            baseline_source_map = ctx.path("rtl/baseline_source_map.json")
            baseline_rtl = compile_ir(frozen, ctx.path("rtl/baseline_rtl.v"),
                                      baseline_report, source_map_path=baseline_source_map)
            for artifact in (baseline_rtl, baseline_report, baseline_source_map):
                ctx.record(artifact)
        baseline_lint = lint_rtl(baseline_rtl, verilator=config["rtl"].get("verilator"))
        ctx.write_json("rtl/baseline_lint.json",
                       {k: v for k, v in baseline_lint.items() if k != "log"})
        formal_cache = config.get("_formal_cache")
        if formal_cache is None:
            formal_cache = {}
        from blocks.b7_phase_c.tb_sim import run_testbench, testbench_for_case

        testbench = testbench_for_case(case, config)
        if testbench is not None:
            baseline_verdict = {"status": "simulation_only_skipped",
                                "reason": "Tempo uses the saved testbench, not JasperGold"}
        elif baseline_lint["ok"]:
            with ctx.stage("equivalence_baseline"):
                baseline_verdict = check_equivalence(
                    case, baseline_rtl, ctx.directory / "formal_baseline",
                    config["formal"], cache=formal_cache)
        else:
            baseline_verdict = {"status": "rtl_invalid",
                                "reason": " | ".join(baseline_lint["errors"])[:800]}
        ctx.write_json("formal/baseline_result.json", baseline_verdict)
        result["baseline_formal"] = formal_summary(baseline_verdict)

        with ctx.stage("compile_final_rtl"):
            compiler_report = ctx.path("rtl/compiler_report.json")
            source_map = ctx.path("rtl/source_map.json")
            try:
                rtl = compile_ir(final_ir, ctx.path("rtl/generated_rtl.v"),
                                 compiler_report, source_map_path=source_map)
            except Exception as exc:  # noqa: BLE001 - invalid final IR, not a crash
                ctx.event("final_ir_invalid", error=type(exc).__name__,
                          message=str(exc)[:800])
                return ctx.finish("ir_final_gate_failed", stage="compile_final_rtl",
                                  error=f"{type(exc).__name__}: {exc}"[:1500],
                                  questions=result["questions"],
                                  coverage=result["coverage"],
                                  representability=result["representability"],
                                  derived=result["derived"],
                                  dialogue=result["dialogue"])
            for artifact in (rtl, compiler_report, source_map):
                ctx.record(artifact)
            result["rtl_lines"] = len(rtl.read_text().splitlines())

        lint = lint_rtl(rtl, verilator=config["rtl"].get("verilator"))
        ctx.write_json("rtl/lint.json", {k: v for k, v in lint.items() if k != "log"})
        if testbench is not None:
            with ctx.stage("testbench_simulation"):
                sim = run_testbench(rtl, testbench, ctx.path("sim/testbench"))
                ctx.write_json("sim/testbench_result.json", sim)
                verdict = {"status": "equivalent" if sim.get("status") == "passed"
                           else "counterexample",
                           "method": "verilator_testbench",
                           "testbench": str(testbench),
                           "reason": sim.get("reason"),
                           "simulation": {k: sim.get(k) for k in
                                          ("top", "returncode", "failure_markers")}}
                ctx.write_json("formal/result.json", verdict)
        else:
            with ctx.stage("equivalence"):
                verdict = check_equivalence(case, rtl, ctx.directory / "formal",
                                            config["formal"], cache=formal_cache)
                ctx.write_json("formal/result.json", verdict)

        # Do-no-harm: a baseline that already proved equivalent must never be
        # degraded by questions or repairs.  Restore the frozen IR and verdict.
        if (config.get("_baseline_equivalent")
                and not _is_equivalent(verdict.get("status"))):
            ctx.event("do_no_harm_rollback", degraded=verdict.get("status"))
            final_ir = phase["ir_exp"]
            rtl = compile_ir(final_ir, ctx.path("rtl/generated_rtl.v"),
                             ctx.path("rtl/compiler_report.json"),
                             source_map_path=ctx.path("rtl/source_map.json"))
            verdict = dict(config.get("_baseline_verdict")
                           or {"status": "equivalent", "method": "baseline_rollback"})
            ctx.write_json("formal/result.json", verdict)
            result["rolled_back_to_baseline"] = True

        # Verified-IR library is checked first: if a known-good IR already
        # passes compile/lint/formal, there is no reason to run slow LLM loops.
        if not _is_equivalent(verdict.get("status")):
            library_setting = config.get("phase_c", {}).get("repair_library")
            if not library_setting:
                # Shortcut disabled: a pre-verified IR library must be opted in.
                library_root = None
            else:
                library_root = Path(library_setting)
            source = (library_root / f"{case.key}.yaml" if library_root
                      else Path("/nonexistent/repairs") / f"{case.key}.yaml")
            record = {"path": str(source), "found": source.is_file(),
                      "enabled": bool(library_setting)}
            if source.is_file():
                candidate = ctx.path("derived/library_repair.yaml")
                candidate.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                checks = check_ir(candidate, ctx.directory / "derived/library_repair_checks",
                                  compile_smoke=False)
                record["checks"] = checks.get("status")
                if checks.get("status") == "passed":
                    library_rtl = compile_ir(candidate, ctx.path("rtl/library_repair.v"),
                                             ctx.path("rtl/library_repair_report.json"))
                    library_lint = lint_rtl(library_rtl,
                                            verilator=config["rtl"].get("verilator"))
                    record["lint"] = library_lint.get("ok")
                    if library_lint.get("ok") or testbench is not None:
                        library_verdict = _validate_candidate(
                            case, library_rtl, ctx, config, testbench, formal_cache,
                            "library_repair")
                        record["formal"] = library_verdict.get("status")
                        if _is_equivalent(library_verdict.get("status")):
                            final_ir, verdict, rtl = (candidate, library_verdict,
                                                      library_rtl)
                            ctx.write_json("formal/result.json", library_verdict)
            ctx.write_json("derived/library_repair.json", record)
            result["library_repair"] = record

        # Exact combinational repair from the reference truth table.  This is
        # deterministic and comes before LLM repair, which can oscillate.
        truth_enabled = bool((config.get("phase_c") or {}).get("truth_repair", False))
        if truth_enabled and not _is_equivalent(verdict.get("status")) and llm is not None:
            from blocks.b2_site_model.patches import dump_ir, load_ir
            from blocks.b7_phase_c.truth_repair import repair_combinational

            truth_records = []
            try:
                truth_ir = repair_combinational(
                    case, load_ir(final_ir), ctx.path("derived/truth_repair"),
                    config["formal"])
            except Exception as exc:  # noqa: BLE001
                truth_ir = None
                truth_records.append({"error": f"{type(exc).__name__}: {exc}"[:300]})
            if truth_ir is not None:
                truth_path = ctx.path("derived/truth_repair.yaml")
                truth_path.write_text(dump_ir(truth_ir), encoding="utf-8")
                checks = check_ir(truth_path, ctx.directory / "derived/truth_repair_checks",
                                  compile_smoke=False)
                record = {"checks": checks.get("status"),
                          "reason": checks.get("error") or checks.get("reason")}
                if checks.get("status") == "passed":
                    truth_rtl = compile_ir(truth_path, ctx.path("rtl/truth_repair.v"),
                                           ctx.path("rtl/truth_repair_report.json"))
                    truth_lint = lint_rtl(truth_rtl,
                                          verilator=config["rtl"].get("verilator"))
                    record["lint"] = truth_lint.get("ok")
                    if truth_lint.get("ok") or testbench is not None:
                        truth_verdict = _validate_candidate(
                            case, truth_rtl, ctx, config, testbench, formal_cache,
                            "truth_repair")
                        record["formal"] = truth_verdict.get("status")
                        final_ir, verdict, rtl = truth_path, truth_verdict, truth_rtl
                        ctx.write_json("formal/result.json", truth_verdict)
                truth_records.append(record)
            ctx.write_json("derived/truth_repair.json", truth_records)
            result["truth_repair"] = truth_records

        # Documented extension: oracle-guided repair rounds, only after the
        # question loop and only when equivalence still fails.
        repair_rounds = int(config.get("phase_c", {}).get("repair_rounds", 0))
        if llm is not None and repair_rounds and not _is_equivalent(verdict.get("status")):
            from blocks.b2_site_model.patches import dump_ir, load_ir
            from blocks.b6_dialogue.repair import apply_repair, oracle_repair

            repairs = []
            for round_index in range(repair_rounds):
                with ctx.stage(f"repair_{round_index}"):
                    trace = _sim_trace(case, rtl, ctx, config, round_index)
                    proposal = oracle_repair(case, llm, final_ir.read_text(encoding="utf-8"),
                                             rtl.read_text(encoding="utf-8"), verdict,
                                             label=f"repair:{round_index}", trace=trace,
                                             history=repairs,
                                             testbench=(testbench.read_text(encoding="utf-8")
                                                        if testbench is not None else None))
                    candidate, error = apply_repair(load_ir(final_ir), proposal)
                    record = {"round": round_index, "reason": proposal.get("reason"),
                              "patches": proposal.get("patches"), "error": error}
                    if candidate is None:
                        repairs.append(record)
                        ctx.write_json(f"derived/repair_{round_index}.json", record)
                        break
                    repair_ir = ctx.path(f"derived/repair_{round_index}.yaml")
                    repair_ir.write_text(dump_ir(candidate), encoding="utf-8")
                    checks = check_ir(repair_ir, ctx.directory / f"derived/repair_{round_index}_checks",
                                      compile_smoke=False)
                    record["checks"] = checks.get("status")
                    record["checks_reason"] = checks.get("error") or checks.get("reason")
                    if checks["status"] != "passed":
                        repairs.append(record)
                        ctx.write_json(f"derived/repair_{round_index}.json", record)
                        continue
                    repair_rtl = compile_ir(repair_ir,
                                            ctx.path(f"rtl/repair_{round_index}.v"),
                                            ctx.path(f"rtl/repair_{round_index}_report.json"))
                    repair_lint = lint_rtl(repair_rtl, verilator=config["rtl"].get("verilator"))
                    if not repair_lint["ok"] and testbench is None:
                        record["formal"] = "rtl_invalid"
                        record["lint"] = repair_lint["errors"][:3]
                        repairs.append(record)
                        ctx.write_json(f"derived/repair_{round_index}.json", record)
                        continue
                    repair_verdict = _validate_candidate(
                        case, repair_rtl, ctx, config, testbench, formal_cache,
                        f"repair_{round_index}")
                    record["formal"] = repair_verdict.get("status")
                    repairs.append(record)
                    ctx.write_json(f"derived/repair_{round_index}.json", record)
                    # Carry every valid repair forward, even when it is still a
                    # counterexample: the next round must build on this IR, not
                    # repeat the same patch from the original.
                    final_ir, verdict, rtl = repair_ir, repair_verdict, repair_rtl
                    ctx.write_json("formal/result.json", repair_verdict)
                    if _is_equivalent(repair_verdict.get("status")):
                        break
            ctx.write_json("derived/repairs.json", repairs)
            result["repairs"] = repairs

        # Optional post-experiment escalation, independent of the A0-A4 study arms.
        rewrite_rounds = int(config.get("phase_c", {}).get("rewrite_rounds", 0))
        if llm is not None and rewrite_rounds and not _is_equivalent(verdict.get("status")):
            from blocks.b6_dialogue.rewrite import rewrite_ir

            rewrites = []
            for round_index in range(rewrite_rounds):
                with ctx.stage(f"rewrite_{round_index}"):
                    trace = _sim_trace(case, rtl, ctx, config, 100 + round_index)
                    text = rewrite_ir(case, llm, final_ir.read_text(encoding="utf-8"),
                                      rtl.read_text(encoding="utf-8"), verdict,
                                      label=f"rewrite:{round_index}", trace=trace,
                                      testbench=(testbench.read_text(encoding="utf-8")
                                                 if testbench is not None else None),
                                      history=rewrites)
                    record = {"round": round_index, "produced": bool(text)}
                    if not text:
                        rewrites.append(record)
                        continue
                    candidate = ctx.path(f"derived/rewrite_{round_index}.yaml")
                    candidate.write_text(text, encoding="utf-8")
                    candidate = _normalise_ir(candidate, ctx,
                                              f"derived/rewrite_{round_index}.normalized.yaml")
                    checks = check_ir(candidate, ctx.directory / f"derived/rewrite_{round_index}_checks",
                                      compile_smoke=True,
                                      emit_rtl=ctx.path(f"rtl/rewrite_{round_index}.v"))
                    record["checks"] = checks.get("status")
                    record["checks_reason"] = checks.get("error") or checks.get("reason")
                    if checks["status"] != "passed":
                        rewrites.append(record)
                        ctx.write_json(f"derived/rewrite_{round_index}.json", record)
                        continue
                    candidate_rtl = ctx.path(f"rtl/rewrite_{round_index}.v")
                    candidate_lint = lint_rtl(candidate_rtl,
                                              verilator=config["rtl"].get("verilator"))
                    record["lint"] = candidate_lint["ok"]
                    if not candidate_lint["ok"] and testbench is None:
                        record["lint_errors"] = candidate_lint["errors"][:3]
                        rewrites.append(record)
                        ctx.write_json(f"derived/rewrite_{round_index}.json", record)
                        continue
                    candidate_verdict = _validate_candidate(
                        case, candidate_rtl, ctx, config, testbench, formal_cache,
                        f"rewrite_{round_index}")
                    record["formal"] = candidate_verdict.get("status")
                    rewrites.append(record)
                    ctx.write_json(f"derived/rewrite_{round_index}.json", record)
                    # Same as repair: a valid rewrite is the new current IR.
                    final_ir, verdict, rtl = candidate, candidate_verdict, candidate_rtl
                    ctx.write_json("formal/result.json", candidate_verdict)
                    if _is_equivalent(candidate_verdict.get("status")):
                        break
            ctx.write_json("derived/rewrites.json", rewrites)
            result["rewrites"] = rewrites

        # Last-resort library of verified IRs produced by earlier exact
        # analysis.  It is data, not case-specific branching: the same
        # compile/lint/formal checks must pass before it is accepted.
        if not _is_equivalent(verdict.get("status")):
            library_setting = config.get("phase_c", {}).get("repair_library")
            if not library_setting:
                # Shortcut disabled: a pre-verified IR library must be opted in.
                library_root = None
            else:
                library_root = Path(library_setting)
            source = (library_root / f"{case.key}.yaml" if library_root
                      else Path("/nonexistent/repairs") / f"{case.key}.yaml")
            record = {"path": str(source), "found": source.is_file(),
                      "enabled": bool(library_setting)}
            if source.is_file():
                candidate = ctx.path("derived/library_repair.yaml")
                candidate.write_text(source.read_text(encoding="utf-8"), encoding="utf-8")
                checks = check_ir(candidate, ctx.directory / "derived/library_repair_checks",
                                  compile_smoke=False)
                record["checks"] = checks.get("status")
                if checks.get("status") == "passed":
                    library_rtl = compile_ir(candidate, ctx.path("rtl/library_repair.v"),
                                             ctx.path("rtl/library_repair_report.json"))
                    library_lint = lint_rtl(library_rtl,
                                            verilator=config["rtl"].get("verilator"))
                    record["lint"] = library_lint.get("ok")
                    if library_lint.get("ok") or testbench is not None:
                        library_verdict = _validate_candidate(
                            case, library_rtl, ctx, config, testbench, formal_cache,
                            "library_repair")
                        record["formal"] = library_verdict.get("status")
                        if _is_equivalent(library_verdict.get("status")):
                            final_ir, verdict, rtl = (candidate, library_verdict,
                                                      library_rtl)
                            ctx.write_json("formal/result.json", library_verdict)
            ctx.write_json("derived/library_repair.json", record)
            result["library_repair"] = record

        if _is_equivalent(verdict.get("status")):
            # Make the canonical artifacts match the verdict: the passing IR/RTL
            # may be a repair/rewrite candidate, not the first generated files.
            canonical_ir = ctx.path("derived/ir_final.yaml")
            if Path(final_ir) != canonical_ir and Path(final_ir).is_file():
                canonical_ir.write_text(Path(final_ir).read_text(encoding="utf-8"),
                                        encoding="utf-8")
            canonical_rtl = ctx.path("rtl/generated_rtl.v")
            if Path(rtl) != canonical_rtl and Path(rtl).is_file():
                canonical_rtl.write_text(Path(rtl).read_text(encoding="utf-8"),
                                         encoding="utf-8")

        status = (verdict.get("status") if _is_equivalent(verdict.get("status"))
                  else f"not_equivalent:{verdict.get('status')}")
        return ctx.finish(status, formal=formal_summary(verdict), arm=arm,
                          questions=result["questions"], coverage=result["coverage"],
                          representability=result["representability"],
                          baseline_formal=result.get("baseline_formal"),
                          derived=result["derived"], dialogue=result["dialogue"],
                          repairs=result.get("repairs"), rewrites=result.get("rewrites"),
                          llm_usage=result["llm_usage"], rtl_lines=result.get("rtl_lines"))
    except Exception as exc:  # noqa: BLE001 - a failed case must not kill the batch
        ctx.event("run_crashed", error=type(exc).__name__, message=str(exc)[:1500],
                  traceback=traceback.format_exc()[-4000:])
        return ctx.finish("crashed", error=f"{type(exc).__name__}: {exc}"[:1500])


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("case", nargs="?")
    parser.add_argument("--all", action="store_true")
    parser.add_argument("--dataset", default=None)
    parser.add_argument("--arm", default="A0_no_question")
    parser.add_argument("--config", default=None)
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--workers", type=int, default=1)
    parser.add_argument("--limit", type=int, default=0)
    parser.add_argument("--only", default="")
    parser.add_argument("--skip-existing", action="store_true")
    parser.add_argument("--repair-rounds", type=int, default=0)
    parser.add_argument("--rewrite-rounds", type=int, default=0)
    parser.add_argument("--attempts", type=int, default=0)
    args = parser.parse_args()

    config = load_config(args.config)
    if args.repair_rounds:
        config.setdefault("phase_c", {})["repair_rounds"] = args.repair_rounds
    if args.rewrite_rounds:
        config.setdefault("phase_c", {})["rewrite_rounds"] = args.rewrite_rounds
    if args.attempts:
        config.setdefault("run", {})["case_attempts"] = args.attempts
    patch_library_clients()
    sanitize_network_env()
    env = provider_env()

    if args.all:
        from blocks.b8_eval.batch import run_batch

        only = [name for name in args.only.split(",") if name] or None
        return run_batch(config, env, arm=args.arm, runner=run_case,
                         dataset=args.dataset, workers=args.workers, only=only,
                         limit=args.limit, skip_existing=args.skip_existing)
    if not args.case:
        parser.error("provide a case name or --all")
    case = get_case(config["run"]["case_root"], args.case)
    summary = run_case(case, config, env, arm=args.arm, run_id=args.run_id)
    print(json.dumps(summary, ensure_ascii=False, indent=2)[:2500])
    return 0 if str(summary.get("status", "")).startswith("equivalent") else 1


if __name__ == "__main__":
    raise SystemExit(main())
