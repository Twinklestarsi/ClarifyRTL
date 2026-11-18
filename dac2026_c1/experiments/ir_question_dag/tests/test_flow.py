from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import yaml

HERE = Path(__file__).resolve()
for location in (HERE.parents[1], HERE.parents[3]):
    if str(location) not in sys.path:
        sys.path.insert(0, str(location))

from Compiler.rtl_compiler.elaborate import ElaborationError, elaborate
from blocks.b2_site_model.detect import check_candidate_text, detect_site
from blocks.b2_site_model.capability import assess, load_matrix
from blocks.b2_site_model.patches import (apply_patch, dump_ir,
                                          normalise_question_pointers,
                                          structural_diff, within_site)
from blocks.b2_site_model.schema import Option, Question, SiteVerdict
from blocks.b3_phase_a.checks import (check_ir, generate_initial_ir,
                                       repair_initial_ir)
from blocks.b3_phase_a.closure import _stale_pointers
from blocks.b1_scaffold.llm import LLM
from blocks.b3_phase_a.inventory import (build_messages,
                                         option_compile_problems,
                                         plan_questions)
from blocks.b3_phase_a.reconcile import close_with_problem_feedback
from blocks.b3_phase_a.test_frontend import (frontend_fingerprint,
                                             generate_with_test_frontend,
                                             resolve_generation_contract)
from blocks.b4_phase_b_graph.graph import (build_ir_graph,
                                            resolve_anchor_nodes,
                                            structural_observability)
from blocks.b4_phase_b_graph.questions import (_clear_on_false_candidate,
                                                build_question_graph,
                                                expand_ir_question_templates,
                                                scan_problem_points)
from blocks.b5_ranking.rank import cost, rank, unlock
from blocks.b6_dialogue.answerer import OracleAnswerer
from blocks.b6_dialogue.loop import run_dialogue
from blocks.b7_phase_c.derive import (_call_check_conformance,
                                      _priority_contract, derive_final)
from blocks.b7_phase_c.verilog import DEFAULT_VERILATOR, lint_rtl
from Compiler.rtl_compiler import compile_ir
from src.run_case import _final_gate_passes, _representability_summary


class FakeLLM:
    def __init__(self, value):
        self.value = value

    def json(self, *args, **kwargs):
        return self.value


class FakeContext:
    def __init__(self, directory: Path):
        self.directory = directory
        self.events = []

    def path(self, relative):
        path = self.directory / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        return path

    def write_json(self, relative, value):
        path = self.path(relative)
        path.write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
        return path

    def event(self, kind, **payload):
        self.events.append((kind, payload))

    def record(self, path, **extra):
        return None


class FixedAnswerer:
    def __init__(self, value):
        self.value = value

    def answer(self, question):
        return {"value": self.value, "reason": "test", "source": "file"}


def template_ir():
    return {
        "module": {"ports": [{"name": "ready", "direction": "input", "width": 1},
                              {"name": "valid", "direction": "input", "width": 1},
                              {"name": "out", "direction": "output", "width": 8}]},
        "clock_domains": [{"name": "clk", "reset_type": "synchronous"}],
        "states": [{"name": "count", "width": 8},
                   {"name": "mem", "type": "memory", "width": 8, "depth": 16}],
        "derived_signals": [
            {"name": "full", "expression": "count == 8'd255"},
            {"name": "fire", "expression": "valid && ready"},
        ],
        "transitions": [{"name": "advance", "guard": "enable", "latency": 1,
                         "action": ["count = count + 1"]}],
        "priority": [{"higher": "clear", "lower": "advance"}],
    }


def question(identifier="Q1", *, anchors=None, patches=None, status="S0"):
    patches = patches if patches is not None else [[], [{"op": "replace", "path": "/x", "value": 1}]]
    item = Question(id=identifier, category="C6", template="hold_on_pause",
                    text=f"question {identifier}", anchors=anchors or ["/x"],
                    options=[Option("keep", patch=patches[0]),
                             Option("change", patch=patches[1])],
                    current="keep", status=status)
    return item


def priority_question(identifier="QP"):
    reverse = {"higher": "advance", "lower": "clear"}
    return Question(
        id=identifier, category="C1", template="priority_pick", text="which wins?",
        anchors=["/priority/0"], current="declared_order", status="S0",
        options=[
            Option("declared_order", patch=[]),
            Option("reverse_order", patch=[
                {"op": "replace", "path": "/priority/0", "value": reverse}]),
        ])


class FlowTests(unittest.TestCase):
    @staticmethod
    def _test_frontend_root(root: Path) -> Path:
        frontend = root / "test_frontend"
        files = {
            "IRgeneration/ir_generate/gen_petrinet_ir.py": "# generator\n",
            "IRgeneration/llm_stream.py": "# stream\n",
        }
        for relative, text in files.items():
            path = frontend / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
        return frontend

    def test_test_frontend_fingerprint_tracks_generator_source(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._test_frontend_root(Path(tmp))
            before = frontend_fingerprint(root)
            source = root / "IRgeneration/ir_generate/gen_petrinet_ir.py"
            source.write_text("# changed generator\n", encoding="utf-8")
            after = frontend_fingerprint(root)
        self.assertTrue(before.startswith("test-monolithic-ir-v2:"))
        self.assertNotEqual(before, after)

    def test_initial_ir_uses_one_complete_test_frontend_artifact(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frontend = self._test_frontend_root(root)
            prompt = root / "spec.txt"
            prompt.write_text("module specification", encoding="utf-8")
            ctx = FakeContext(root / "runs" / "Case" / "run1")
            ctx.directory.mkdir(parents=True)
            case = SimpleNamespace(prompt=prompt)

            def fake_generate(spec, output, env, **kwargs):
                Path(output).write_text("module:\n  name: DUT\n", encoding="utf-8")
                return Path(output), {
                    "model": "fake", "usage": {"total_tokens": 7},
                    "frontend": "test_monolithic",
                    "frontend_root": str(frontend),
                    "frontend_fingerprint": frontend_fingerprint(frontend),
                }

            with patch("blocks.b3_phase_a.checks.generate_with_test_frontend",
                       side_effect=fake_generate) as generate:
                ir, metadata = generate_initial_ir(
                    case, ctx, {"OPENAI_API_KEY": "x", "OPENAI_BASE_URL": "y",
                                "OPENAI_MODEL": "z"},
                    frontend={"frontend": "test_monolithic",
                              "test_root": str(frontend)})
                ir_text = ir.read_text(encoding="utf-8")
        self.assertEqual(ir_text, "module:\n  name: DUT\n")
        self.assertEqual(metadata["frontend"], "test_monolithic")
        self.assertEqual(generate.call_count, 1)
        self.assertNotIn("staged_ir_assembled", [kind for kind, _ in ctx.events])

    def test_initial_ir_cache_rejects_old_frontend_fingerprint(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frontend = self._test_frontend_root(root)
            prompt = root / "spec.txt"
            prompt.write_text("spec", encoding="utf-8")
            ctx = FakeContext(root / "runs" / "Case" / "run1")
            ctx.directory.mkdir(parents=True)
            cache = ctx.directory.parent / "_generation_cache"
            cache.mkdir()
            (cache / "ir.yaml").write_text("old: true\n", encoding="utf-8")
            (cache / "status.json").write_text(
                json.dumps({"passed": True, "attempted": True,
                            "frontend_fingerprint": "old"}), encoding="utf-8")
            case = SimpleNamespace(prompt=prompt)

            def fake_generate(spec, output, env, **kwargs):
                Path(output).write_text("new: true\n", encoding="utf-8")
                return Path(output), {
                    "frontend": "test_monolithic",
                    "frontend_root": str(frontend),
                    "frontend_fingerprint": frontend_fingerprint(frontend),
                }

            with patch("blocks.b3_phase_a.checks.generate_with_test_frontend",
                       side_effect=fake_generate) as generate:
                ir, _ = generate_initial_ir(
                    case, ctx, {"OPENAI_API_KEY": "x", "OPENAI_BASE_URL": "y",
                                "OPENAI_MODEL": "z"},
                    frontend={"test_root": str(frontend)})
                ir_text = ir.read_text(encoding="utf-8")
        self.assertEqual(ir_text, "new: true\n")
        self.assertEqual(generate.call_count, 1)

    def test_initial_ir_repair_passes_full_checker_feedback(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ctx = FakeContext(root / "run")
            ctx.directory.mkdir(parents=True)
            prompt = root / "spec.txt"
            prompt.write_text("spec", encoding="utf-8")
            ir = root / "ir.yaml"
            ir.write_text("module: {}\n", encoding="utf-8")
            checks_dir = root / "checks"
            checks_dir.mkdir()
            report = checks_dir / "checker2_report.json"
            report.write_text('{"errors":[{"message":"undriven"}]}', encoding="utf-8")
            candidate = root / "candidate.yaml"
            candidate.write_text("module: {}\n", encoding="utf-8")
            case = SimpleNamespace(prompt=prompt)
            checks = {"status": "failed", "reason": "Checker2Error",
                      "error": "checker2 failed"}
            env = {"OPENAI_API_KEY": "x", "OPENAI_BASE_URL": "y",
                   "OPENAI_MODEL": "z"}
            with patch("iteration.hw_ir_repair.build_repair_context",
                       return_value="complete feedback") as context, patch(
                           "iteration.hw_ir_repair.repair_hw_ir",
                           return_value=(candidate, {"status": "validated"})) as repair:
                result, metadata, failure = repair_initial_ir(
                    case, ctx, env, ir_path=ir, checks=checks,
                    checks_dir=checks_dir, round_index=1, history=[],
                    max_tokens=1234, max_patch_attempts=2)
        self.assertEqual(result, candidate)
        self.assertEqual(metadata["status"], "validated")
        self.assertEqual(failure["failed_step"], "Checker2Error")
        artifacts = context.call_args.kwargs["artifacts"]
        self.assertIn(("CHECKER2 REPORT", report), artifacts)
        self.assertEqual(repair.call_args.kwargs["context_text"], "complete feedback")
        self.assertEqual(repair.call_args.kwargs["max_patch_attempts"], 2)

    def test_deterministic_expansion_only_emits_actionable_questions(self):
        ir = template_ir()
        questions = expand_ir_question_templates(ir)
        # C7 (memory read-during-write) has no concrete patch and is therefore
        # no longer a question at all: only keep/change pairs are emitted.
        self.assertEqual({q.category for q in questions}, {f"C{i}" for i in (1, 2, 3, 4, 5, 6, 8)})
        for item in questions:
            self.assertTrue(item.anchors, item.category)
            self.assertEqual(len([o for o in item.options if not o.patch]), 1)
            self.assertTrue(any(o.patch for o in item.options), item.category)
            for option in item.options:
                candidate = apply_patch(ir, option.patch)
                if option.patch:
                    self.assertNotEqual(candidate, ir, item.category)

    def test_deterministic_expansion_does_not_invent_sites_for_empty_ir(self):
        self.assertEqual(expand_ir_question_templates({}), [])

    def test_unactionable_problem_point_does_not_block_the_freeze(self):
        def fake_close(path, questions, ctx, config, llm=None):
            return {"closed": True, "ir": path, "coverage": 1.0, "verdicts": []}

        graph = {"nodes": [{"id": "IR:state:mem", "attributes": {}}], "edges": []}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ir_path = root / "ir.yaml"
            ir_path.write_text("states:\n- name: mem\n  type: memory\n  width: 8\n  depth: 16\n",
                               encoding="utf-8")
            ctx = FakeContext(root / "run")
            result = close_with_problem_feedback(
                ir_path, [], ctx, {"problem_feedback_rounds": 2},
                close_fn=fake_close, graph_builder=lambda _: graph)
        self.assertTrue(result["closed"])
        self.assertEqual(result["feedback_status"], "closed_with_unactionable")
        self.assertEqual(result["unactionable_problem_points"], 1)
        self.assertFalse(any("gap_type" == key for key in result))

    def test_problem_feedback_recloses_after_safe_template_is_added(self):
        calls = []

        def fake_close(path, questions, ctx, config, llm=None):
            calls.append(len(questions))
            return {"closed": True, "ir": path, "coverage": 1.0, "verdicts": []}

        point = {"kind": "missing_else_path", "ir_node": "IR:transition:advance",
                 "ir_pointer": "/transitions/0/else_action"}
        graph = {"nodes": [{"id": "IR:transition:advance", "attributes": {}}],
                 "edges": []}
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ir_path = root / "ir.yaml"
            ir_path.write_text(
                "clock_domains:\n- name: main\n  clock: clk\n  reset: reset\n"
                "  reset_type: synchronous\n  reset_polarity: active_high\n"
                "states:\n- name: count\n  type: counter\n  width: 8\n"
                "  clock_domain: main\n  reset_value: 0\n"
                "transitions:\n- name: advance\n  clock_domain: main\n"
                "  guard: enable\n  latency: 1\n  transition_type: sequential\n"
                "  update_semantics: non_blocking\n"
                "  action:\n  - count = count + 1\n"
                "sequential_processes:\n- name: main_proc\n  clock_domain: main\n"
                "  transitions: [advance]\npriority: []\n",
                encoding="utf-8")
            result = close_with_problem_feedback(
                ir_path, [], FakeContext(root / "run"), {"problem_feedback_rounds": 2},
                close_fn=fake_close, graph_builder=lambda _: graph,
                scanner=lambda _ir, _graph: [point])
            source_text = ir_path.read_text(encoding="utf-8")
        self.assertTrue(result["closed"])
        self.assertEqual(result["uncovered_problem_points"], 0)
        self.assertEqual(calls, [0, 1])
        self.assertEqual(len(result["questions"]), 1)
        added = result["questions"][0]
        self.assertEqual(added.category, "C6")
        self.assertEqual(added.source_point, "missing_else_path")
        self.assertEqual(added.anchors[0], "/transitions/0")
        paths = [operation["path"] for operation in added.options[1].patch]
        self.assertEqual(paths, ["/transitions/-",
                                 "/sequential_processes/0/transitions/-",
                                 "/priority/-"])
        source = yaml.safe_load(source_text)
        patched = apply_patch(source, added.options[1].patch)
        self.assertEqual(len(patched["transitions"]), 2)
        added_transition = patched["transitions"][-1]
        self.assertEqual(added_transition["guard"], "!(enable)")
        self.assertEqual(added_transition["action"], ["count = 0"])
        new_name = added_transition["name"]
        self.assertTrue(new_name.startswith("advance__"))
        self.assertEqual(patched["sequential_processes"][0]["transitions"],
                         ["advance", new_name])
        self.assertEqual(patched["priority"][-1],
                         {"higher": "advance", "lower": new_name})

    def test_memory_scan_emits_explicit_g1_problem_point(self):
        points = scan_problem_points(template_ir(), {"nodes": [], "edges": []})
        point = next(item for item in points
                     if item["kind"] == "memory_read_during_write_policy")
        self.assertEqual(point["gap_type"], "G1")

    def test_observability_is_diagnostic_not_a_gate(self):
        q = question()
        with tempfile.TemporaryDirectory() as tmp, patch(
                "blocks.b2_site_model.detect.check_candidate_text",
                return_value={"ok": True}):
            missing = detect_site({"x": 0}, q, Path(tmp) / "missing", compile_rtl=False,
                                  observable=lambda _: None, require_observable=True)
            hidden = detect_site({"x": 0}, q, Path(tmp) / "hidden", compile_rtl=False,
                                 observable=lambda _: False, require_observable=True)
            visible = detect_site({"x": 0}, q, Path(tmp) / "visible", compile_rtl=False,
                                  observable=lambda _: True, require_observable=True)
        self.assertEqual(missing.status, "S0")
        self.assertEqual(hidden.status, "S0")
        self.assertEqual(visible.status, "S0")
        self.assertIsNone(missing.detail.get("observable"))
        self.assertFalse(hidden.detail.get("observable"))
        self.assertTrue(visible.detail.get("observable"))

    def test_structural_observability_follows_dependency_influence_direction(self):
        ir = {"states": [{"name": "x"}]}
        graph = {
            "nodes": [
                {"id": "IR:state:x", "attributes": {}},
                {"id": "IR:derived:out", "attributes": {}},
                {"id": "IR:port:out", "attributes": {"roles": ["output"]}},
            ],
            "edges": [
                {"source": "IR:derived:out", "target": "IR:state:x", "relation": "reads"},
                {"source": "IR:port:out", "target": "IR:derived:out",
                 "relation": "depends-on"},
            ],
        }
        result = structural_observability(graph, ir, ["/states/0"], horizon=4)
        self.assertTrue(result["observable"])
        self.assertEqual(result["min_distance"], 2)

    def test_intrinsic_memory_policy_g1_stays_blocked(self):
        matrix = load_matrix()
        construct = "read_during_write_policy"
        q = Question(id=construct, category="C7", template="gap", text="gap",
                     anchors=[], options=[Option("a"), Option("b")],
                     required_construct=construct, gap_hypothesis="G1")
        verdict = SiteVerdict("S3", [], [], False, False,
                              {"reason": "no schema carrier"})
        result = assess(q, verdict, matrix, allow_schema_extension=True)
        self.assertFalse(result["ok"])
        self.assertTrue(result["blocked"])
        self.assertFalse(result["local_extension_safe"])

    def test_parameterized_iteration_is_a_safe_versioned_restructure(self):
        matrix = load_matrix()
        extension = matrix["local_extensions"]["parameterized_multi_cycle_latency"]
        self.assertTrue(extension["safe"])
        self.assertEqual(extension["lowering"], "parameterized_generate_restructure")
        self.assertEqual(
            matrix["categories"]["C2"]["parameterized_multi_cycle_latency"],
            "instance_restructure")

    def test_parameterized_generate_expands_nested_compile_time_expressions(self):
        result = elaborate({
            "module": {"parameters": {"BANKS": 2, "LANES": 3, "WIDTH": 8}},
            "generate": [{
                "name": "pairs",
                "for": [
                    {"var": "bank", "start": 0, "stop": "BANKS"},
                    {"var": "lane", "start": 0, "stop": "LANES"},
                ],
                "emit": [{"derived_signal": {
                    "name": "pair_{bank}_{lane + 1}", "width": "WIDTH",
                    "expression": "request[{bank} * LANES + {lane}] | {WIDTH{1'b0}}",
                }}],
            }],
        })
        signals = result["derived_signals"]
        self.assertEqual(len(signals), 6)
        self.assertEqual(signals[-1]["name"], "pair_1_3")
        self.assertEqual(
            signals[-1]["expression"],
            "request[1 * LANES + 2] | {WIDTH{1'b0}}")

    def test_parameterized_circular_scan_passes_complete_ir_battery(self):
        document = {
            "module": {
                "name": "circular_scan",
                "parameters": {"LANES": 4, "INDEXW": 2},
                "ports": [
                    {"name": "eligible", "direction": "input", "width": "LANES"},
                    {"name": "start", "direction": "input", "width": "INDEXW"},
                    {"name": "found", "direction": "output", "width": 1},
                    {"name": "chosen", "direction": "output", "width": "INDEXW"},
                ],
            },
            "generate": [
                {
                    "name": "seed",
                    "for": {"var": "unused", "start": 0, "stop": 1},
                    "emit": [
                        {"derived_signal": {
                            "name": "found_0", "width": 1, "expression": "1'b0"}},
                        {"derived_signal": {
                            "name": "chosen_0", "width": "INDEXW", "expression": "0"}},
                    ],
                },
                {
                    "name": "scan",
                    "for": {"var": "step", "start": 0, "stop": "LANES"},
                    "emit": [
                        {"derived_signal": {
                            "name": "candidate_{step}", "width": 1,
                            "expression": "eligible[(start + {step}) % LANES]"}},
                        {"derived_signal": {
                            "name": "found_{step + 1}", "width": 1,
                            "expression": "found_{step} | candidate_{step}"}},
                        {"derived_signal": {
                            "name": "chosen_{step + 1}", "width": "INDEXW",
                            "expression": (
                                "found_{step} ? chosen_{step} : "
                                "(candidate_{step} ? ((start + {step}) % LANES) : "
                                "chosen_{step})")}},
                    ],
                },
                {
                    "name": "result",
                    "for": {"var": "unused", "start": 0, "stop": 1},
                    "emit": [
                        {"derived_signal": {
                            "name": "found", "width": 1,
                            "expression": "found_{LANES}"}},
                        {"derived_signal": {
                            "name": "chosen", "width": "INDEXW",
                            "expression": "chosen_{LANES}"}},
                    ],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = check_candidate_text(
                yaml.safe_dump(document, sort_keys=False), root, compile_rtl=True)
            rtl = (root / "rtl.v").read_text(encoding="utf-8")
        self.assertTrue(result["ok"], result)
        self.assertIn("assign candidate_3 = eligible[(start + 3) % LANES];", rtl)
        self.assertIn("assign chosen = chosen_4;", rtl)

    def test_parameterized_slice_merge_passes_complete_ir_battery(self):
        document = {
            "module": {
                "name": "slice_merge",
                "parameters": {"DATAW": 32, "BYTEENW": 4},
                "ports": [
                    {"name": "old_word", "direction": "input", "width": "DATAW"},
                    {"name": "new_word", "direction": "input", "width": "DATAW"},
                    {"name": "byte_enable", "direction": "input", "width": "BYTEENW"},
                    {"name": "merged_word", "direction": "output", "width": "DATAW"},
                ],
            },
            "generate": [
                {
                    "name": "seed",
                    "for": {"var": "unused", "start": 0, "stop": 1},
                    "emit": [{"derived_signal": {
                        "name": "merged_0", "width": "DATAW",
                        "expression": "old_word"}}],
                },
                {
                    "name": "merge_bytes",
                    "for": {"var": "byte", "start": 0, "stop": "BYTEENW"},
                    "emit": [{"derived_signal": {
                        "name": "merged_{byte + 1}", "width": "DATAW",
                        "expression": (
                            "byte_enable[{byte}] ? "
                            "((merged_{byte} & ~(32'hff << ({byte} * 8))) | "
                            "(new_word & (32'hff << ({byte} * 8)))) : merged_{byte}"),
                    }}],
                },
                {
                    "name": "result",
                    "for": {"var": "unused", "start": 0, "stop": 1},
                    "emit": [{"derived_signal": {
                        "name": "merged_word", "width": "DATAW",
                        "expression": "merged_{BYTEENW}"}}],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = check_candidate_text(
                yaml.safe_dump(document, sort_keys=False), root, compile_rtl=True)
            rtl = (root / "rtl.v").read_text(encoding="utf-8")
        self.assertTrue(result["ok"], result)
        self.assertIn("assign merged_4 = byte_enable[3]", rtl)
        self.assertIn("assign merged_word = merged_4;", rtl)

    def test_parameterized_pipeline_passes_complete_ir_battery(self):
        document = {
            "module": {
                "name": "generated_pipeline",
                "parameters": {"LATENCY": 3, "WIDTH": 8},
                "ports": [
                    {"name": "clk", "direction": "input", "width": 1},
                    {"name": "reset", "direction": "input", "width": 1},
                    {"name": "din", "direction": "input", "width": "WIDTH"},
                    {"name": "dout", "direction": "output", "width": "WIDTH"},
                ],
            },
            "clock_domains": [{
                "name": "main", "clock": "clk", "edge": "posedge",
                "reset": "reset", "reset_type": "synchronous",
                "reset_polarity": "active_high",
            }],
            "generate": [
                {
                    "name": "first_stage",
                    "for": {"var": "unused", "start": 0, "stop": 1},
                    "emit": [
                        {"state": {
                            "name": "stage_0", "type": "pipeline_register",
                            "width": "WIDTH", "clock_domain": "main", "reset_value": 0}},
                        {"transition": {
                            "name": "stage_0_update", "clock_domain": "main",
                            "guard": "1'b1", "action": "stage_0 = din", "latency": 1}},
                    ],
                },
                {
                    "name": "remaining_stages",
                    "for": {"var": "stage", "start": 1, "stop": "LATENCY"},
                    "emit": [
                        {"state": {
                            "name": "stage_{stage}", "type": "pipeline_register",
                            "width": "WIDTH", "clock_domain": "main", "reset_value": 0}},
                        {"transition": {
                            "name": "stage_{stage}_update", "clock_domain": "main",
                            "guard": "1'b1",
                            "action": "stage_{stage} = stage_{stage - 1}", "latency": 1}},
                    ],
                },
                {
                    "name": "pipeline_output",
                    "for": {"var": "unused", "start": 0, "stop": 1},
                    "emit": [{"derived_signal": {
                        "name": "dout", "width": "WIDTH",
                        "expression": "stage_{LATENCY - 1}"}}],
                },
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            result = check_candidate_text(
                yaml.safe_dump(document, sort_keys=False), root, compile_rtl=True)
            rtl = (root / "rtl.v").read_text(encoding="utf-8")
        self.assertTrue(result["ok"], result)
        self.assertIn("reg [(WIDTH)-1:0] stage_2;", rtl)
        self.assertIn("assign dout = stage_2;", rtl)

    def test_parameterized_generate_rejects_unresolved_bounds(self):
        document = {"module": {}, "generate": [{
            "name": "bad",
            "for": {"var": "i", "start": 0, "stop": "MISSING"},
            "emit": [],
        }]}
        with self.assertRaisesRegex(ElaborationError, "unknown parameter MISSING"):
            elaborate(document)

    def test_inventory_drops_unmaterialized_questions(self):
        raw = {"questions": [{"id": "Q1", "category": "C7", "template": "mem_conflict",
                              "text": "same-address read returns old or new data?",
                              "anchors": [], "required_construct": "read_during_write_policy",
                              "gap_hypothesis": "G1", "depends_on": [],
                              "options": [{"value": "old", "patch": []},
                                          {"value": "new", "patch": []}]}]}
        questions, notes = plan_questions(FakeLLM(raw), "spec", "x: 0", {"x": 0},
                                          rounds=1)
        self.assertEqual(questions, [])
        self.assertTrue(any("dropped invalid questions" in note for note in notes))

    def test_missing_anchor_is_a_gap_not_a_degenerate_question(self):
        q = question(anchors=[])
        q.anchors = []
        q.gap_hypothesis = "G2"
        with tempfile.TemporaryDirectory() as tmp:
            verdict = detect_site({"x": 0}, q, Path(tmp), compile_rtl=False)
        self.assertEqual(verdict.status, "S1")
        self.assertIn("no concrete IR anchor", verdict.detail["reason"])

    def test_fixed_ranking_does_not_fallback_when_graph_feature_is_missing(self):
        q2, q1 = question("Q2"), question("Q1")
        result = rank([q2, q1], {"nodes": [], "edges": []}, {"x": 0},
                      {"method": "fixed", "weights": {"O": 0.7}}, 0,
                      states={"Q1": "ready", "Q2": "ready"})
        self.assertEqual(result["effective_method"], "fixed")
        self.assertEqual([row["id"] for row in result["rows"]], ["Q1", "Q2"])
        self.assertNotIn("P", result["rows"][0])

    def test_invalid_answer_does_not_prune_related_question(self):
        q1, q2 = question("Q1"), question("Q2")
        with tempfile.TemporaryDirectory() as tmp:
            ctx = FakeContext(Path(tmp))
            result = run_dialogue({"x": 0}, [q1, q2], {"nodes": [], "edges": []},
                                  {"method": "fixed", "weights": {}, "max_questions": 2,
                                   "max_rounds": 2, "enable_pruning": True},
                                  FixedAnswerer("not-an-option"), ctx)
        self.assertEqual(result["summary"]["states"]["Q1"], "requires_new_baseline")
        self.assertEqual(result["summary"]["states"]["Q2"], "deferred")

    def test_deferred_prerequisite_does_not_unlock_dependent_question(self):
        q1, q2 = question("Q1"), question("Q2")
        q2.depends_on = ["Q1"]
        question_graph = build_question_graph({"nodes": [], "edges": []},
                                              {"x": 0}, [q1, q2])
        with tempfile.TemporaryDirectory() as tmp:
            ctx = FakeContext(Path(tmp))
            result = run_dialogue(
                {"x": 0}, [q1, q2], {"nodes": [], "edges": []},
                {"method": "fixed", "weights": {}, "max_questions": 2,
                 "max_rounds": 2, "enable_pruning": True},
                FixedAnswerer("undecided"), ctx, question_graph=question_graph)
        self.assertEqual(result["summary"]["asked"], 1)
        self.assertEqual(result["summary"]["states"]["Q1"], "deferred")
        self.assertEqual(result["summary"]["states"]["Q2"], "deferred")

    def test_conditional_dependency_becomes_ready_for_matching_answer(self):
        q1, q2 = question("Q1"), question("Q2")
        q2.active_when = [{"question": "Q1", "values": ["keep"]}]
        question_graph = build_question_graph({"nodes": [], "edges": []},
                                              {"x": 0}, [q1, q2])
        with tempfile.TemporaryDirectory() as tmp, patch(
                "blocks.b6_dialogue.loop.check_candidate_text", return_value={"ok": True}):
            result = run_dialogue(
                {"x": 0}, [q1, q2], {"nodes": [], "edges": []},
                {"method": "fixed", "weights": {}, "max_questions": 2,
                 "max_rounds": 2, "enable_pruning": False},
                FixedAnswerer("keep"), FakeContext(Path(tmp)), question_graph=question_graph)
        self.assertEqual(result["summary"]["asked"], 2)
        self.assertEqual(result["summary"]["states"]["Q2"], "answered")

    def test_conditional_dependency_is_pruned_for_nonmatching_answer(self):
        q1, q2 = question("Q1"), question("Q2")
        q2.active_when = [{"question": "Q1", "values": ["keep"]}]
        question_graph = build_question_graph({"nodes": [], "edges": []},
                                              {"x": 0}, [q1, q2])
        with tempfile.TemporaryDirectory() as tmp, patch(
                "blocks.b6_dialogue.loop.check_candidate_text", return_value={"ok": True}):
            result = run_dialogue(
                {"x": 0}, [q1, q2], {"nodes": [], "edges": []},
                {"method": "fixed", "weights": {}, "max_questions": 2,
                 "max_rounds": 2, "enable_pruning": True},
                FixedAnswerer("change"), FakeContext(Path(tmp)), question_graph=question_graph)
        self.assertEqual(result["summary"]["asked"], 1)
        self.assertEqual(result["summary"]["states"]["Q2"], "pruned_inapplicable")
        self.assertIn("not in", result["summary"]["state_reasons"]["Q2"])

    def test_ranking_uses_fixed_denominators_and_missing_feature_fallback(self):
        q1, q2 = question("Q1"), question("Q2")
        q2.depends_on = ["Q1"]
        self.assertEqual(unlock(q1, [q1, q2], {"Q1": "ready", "Q2": "blocked"},
                                denominator=8)["denominator"], 8)
        self.assertEqual(cost(q1, denominator=20)["denominator"], 20)
        ranked = rank([q1], {"nodes": [], "edges": []}, {"x": 0},
                      {"method": "ir_structural_proxy",
                       "weights": {"O": 1, "D": 0, "U": 0, "C": 0}}, 0,
                      states={"Q1": "ready"})
        self.assertEqual(ranked["effective_method"], "random")
        self.assertIn("Q1:O", ranked["fallback_reason"])
        self.assertIn("normalization", ranked)

    def test_shared_parent_anchor_is_not_enough_to_prune(self):
        q1 = question("Q1", anchors=["/x/a"],
                      patches=[[], [{"op": "replace", "path": "/x/a", "value": 1}]])
        q2 = question("Q2", anchors=["/x"],
                      patches=[[], [{"op": "replace", "path": "/x/b", "value": 1}]])
        with tempfile.TemporaryDirectory() as tmp:
            ctx = FakeContext(Path(tmp))
            with patch("blocks.b6_dialogue.loop.check_candidate_text",
                       return_value={"ok": True}):
                result = run_dialogue(
                    {"x": {"a": 0, "b": 0}}, [q1, q2], {"nodes": [], "edges": []},
                    {"method": "fixed", "weights": {}, "max_questions": 1,
                     "max_rounds": 2, "enable_pruning": True},
                    FixedAnswerer("keep"), ctx)
        self.assertEqual(result["summary"]["states"]["Q2"], "deferred")

    def test_other_answer_with_replacement_is_applied(self):
        q = question("Q1", anchors=["/x"],
                     patches=[[], [{"op": "replace", "path": "/x", "value": 1}]])

        class OtherAnswerer:
            def answer(self, question):
                return {"value": "other", "reason": "spec",
                        "source": "oracle_reference_rtl",
                        "replacement": {"pointer": "/x", "value": 7}}

        with tempfile.TemporaryDirectory() as tmp:
            ctx = FakeContext(Path(tmp))
            with patch("blocks.b6_dialogue.loop.check_candidate_text",
                       return_value={"ok": True}):
                result = run_dialogue(
                    {"x": 0}, [q], {"nodes": [], "edges": []},
                    {"method": "fixed", "weights": {}, "max_questions": 0,
                     "max_rounds": 0, "enable_pruning": False},
                    OtherAnswerer(), ctx)
        self.assertEqual(result["summary"]["states"]["Q1"], "answered")
        self.assertEqual(result["committed_patches"],
                         [{"op": "replace", "path": "/x", "value": 7}])

    def test_dialogue_has_no_question_budget(self):
        qs = [question(f"Q{i}") for i in range(1, 5)]
        with tempfile.TemporaryDirectory() as tmp:
            ctx = FakeContext(Path(tmp))
            with patch("blocks.b6_dialogue.loop.check_candidate_text",
                       return_value={"ok": True}):
                result = run_dialogue(
                    {"x": 0}, qs, {"nodes": [], "edges": []},
                    {"method": "fixed", "weights": {}, "max_questions": 0,
                     "max_rounds": 0, "enable_pruning": False},
                    FixedAnswerer("keep"), ctx)
        self.assertEqual(result["summary"]["asked"], 4)
        self.assertEqual(result["summary"]["stop_reason"], "no_open_questions")

    def test_derive_replays_committed_patch_on_frozen_ir(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frozen = root / "ir.yaml"
            frozen.write_text("x: 0\n", encoding="utf-8")
            ctx = FakeContext(root / "run")
            dialogue = {"ir": {"x": 99},
                        "committed_patches": [{"op": "replace", "path": "/x", "value": 1}],
                        "constraints": [{"id": "C:Q1", "question": "Q1", "patch": [
                            {"op": "replace", "path": "/x", "value": 1}]}],
                        "summary": {"states": {"Q1": "answered"}}}
            with patch("blocks.b7_phase_c.derive.check_ir",
                       return_value={"status": "passed"}):
                result = derive_final(frozen, dialogue, [question()], ctx)
            self.assertIn("x: 1", result["ir"].read_text())
            coverage = json.loads((ctx.directory / "derived/constraint_coverage.json").read_text())
            self.assertEqual(coverage["covered"], 1)

    def test_conformance_failure_is_advisory_at_gate(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            frozen = root / "ir.yaml"
            frozen.write_text("priority: []\nx: 0\n", encoding="utf-8")
            ctx = FakeContext(root / "run")
            q = priority_question("Q1")
            dialogue = {"committed_patches": [],
                        "constraints": [{"id": "C:Q1", "question": "Q1",
                                         "value": "not_an_option", "patch": []}],
                        "summary": {"states": {"Q1": "answered"}}}
            with patch("blocks.b7_phase_c.derive.check_ir",
                       return_value={"status": "passed"}) as checker:
                result = derive_final(frozen, dialogue, [q], ctx)
            report = json.loads((ctx.directory / "derived/conformance.json").read_text())
        self.assertFalse(result["summary"]["conformance_ok"])
        # Conformance is recorded for diagnostics; compile+constraints decide.
        self.assertTrue(_final_gate_passes(result["summary"]))
        self.assertFalse(checker.call_args.kwargs["compile_smoke"])
        self.assertEqual(report["checker"], "local_c1_priority_v1")
        self.assertTrue(any("selected_option_exists" in item
                            for item in report["violations"]))

    def test_local_c1_conformance_accepts_replayed_priority_option(self):
        base = {"priority": [{"higher": "clear", "lower": "advance"}]}
        q = priority_question()
        selected = q.options[1]
        dialogue = {
            "constraints": [{"question": q.id, "value": selected.value,
                             "patch": selected.patch}],
            "summary": {"states": {q.id: "answered"}},
        }
        contract = _priority_contract([q], dialogue, base)
        with tempfile.TemporaryDirectory() as tmp:
            final_path = Path(tmp) / "ir.yaml"
            final_path.write_text(dump_ir(apply_patch(base, selected.patch)),
                                  encoding="utf-8")
            result = _call_check_conformance(final_path, contract)
        self.assertTrue(result["ok"])
        self.assertEqual(result["status"], "conformant")
        self.assertEqual(result["checker"], "local_c1_priority_v1")

    def test_local_c1_conformance_rejects_unreplayed_patch(self):
        base = {"priority": [{"higher": "clear", "lower": "advance"}]}
        q = priority_question()
        selected = q.options[1]
        dialogue = {
            "constraints": [{"question": q.id, "value": selected.value,
                             "patch": selected.patch}],
            "summary": {"states": {q.id: "answered"}},
        }
        contract = _priority_contract([q], dialogue, base)
        with tempfile.TemporaryDirectory() as tmp:
            final_path = Path(tmp) / "ir.yaml"
            final_path.write_text(dump_ir(base), encoding="utf-8")
            result = _call_check_conformance(final_path, contract)
        self.assertFalse(result["ok"])
        self.assertTrue(any("option_patch_replayed" in item
                            for item in result["violations"]))
        self.assertTrue(any("priority_anchor_value" in item
                            for item in result["violations"]))

    def test_local_c1_conformance_rejects_non_priority_anchor(self):
        base = {"priority": [{"higher": "clear", "lower": "advance"}], "x": 0}
        q = priority_question()
        q.anchors = ["/x"]
        selected = q.options[1]
        x_patch = [{"op": "replace", "path": "/x", "value": 1}]
        dialogue = {"constraints": [{"question": q.id, "value": selected.value,
                                     "patch": x_patch}],
                    "summary": {"states": {q.id: "answered"}}}
        contract = _priority_contract([q], dialogue, base)
        contract[0]["option_patch"] = x_patch
        contract[0]["constraint_patch"] = x_patch
        selected.patch = x_patch
        with tempfile.TemporaryDirectory() as tmp:
            final_path = Path(tmp) / "ir.yaml"
            final_path.write_text(dump_ir(apply_patch(base, x_patch)),
                                  encoding="utf-8")
            result = _call_check_conformance(final_path, contract)
        self.assertFalse(result["ok"])
        self.assertTrue(any("priority_anchor_present" in item
                            for item in result["violations"]))

    def test_local_c1_conformance_accepts_transition_action_evidence(self):
        base = {"priority": [{"higher": "clear", "lower": "advance"}],
                "transitions": [{"name": "advance", "action": ["count = 1"]}]}
        q = priority_question()
        q.anchors = ["/transitions/0/action/0"]
        patch = [{"op": "replace", "path": "/transitions/0/action/0",
                  "value": "count = 2"}]
        q.options[1].patch = patch
        dialogue = {"constraints": [{"question": q.id, "value": q.options[1].value,
                                     "patch": patch}],
                    "summary": {"states": {q.id: "answered"}}}
        contract = _priority_contract([q], dialogue, base)
        with tempfile.TemporaryDirectory() as tmp:
            final_path = Path(tmp) / "ir.yaml"
            final_path.write_text(dump_ir(apply_patch(base, patch)),
                                  encoding="utf-8")
            result = _call_check_conformance(final_path, contract)
        self.assertTrue(result["ok"], result)
        self.assertTrue(any(check["check"] == "priority_anchor_present"
                            and check["ok"] for row in result["questions"]
                            for check in row["checks"]))

    def test_question_graph_keeps_relations_separate(self):
        q = question()
        q.evidence = ["spec clause"]
        q.depends_on = []
        ir_graph = {"nodes": [{"id": "IR:state:x", "attributes": {}}], "edges": []}
        graph = build_question_graph(ir_graph, {"x": 0}, [q])
        self.assertIn("anchors_to", graph["relations"])
        self.assertNotIn("edges", graph)
        self.assertEqual(graph["counts"]["questions"], 1)


class RegressionTests(unittest.TestCase):
    """Unit coverage for the generic mechanisms added after the G1 commit.

    Fixtures are synthetic; no test names or IR depend on a dataset case.
    """

    @staticmethod
    def compiler_ir(*, guard="1'b1", derived="acc"):
        return {
            "module": {"name": "dut", "parameters": {"W": 8}, "ports": [
                {"name": "clk", "direction": "input", "width": 1},
                {"name": "reset", "direction": "input", "width": 1},
                {"name": "din", "direction": "input", "width": "W"},
                {"name": "dout", "direction": "output", "width": "W"}]},
            "clock_domains": [{
                "name": "main", "clock": "clk", "edge": "posedge",
                "reset": "reset", "reset_type": "synchronous",
                "reset_polarity": "active_high"}],
            "states": [{"name": "acc", "type": "register", "width": "W",
                        "clock_domain": "main", "reset_value": "0"}],
            "derived_signals": [{"name": "dout", "width": "W",
                                 "expression": derived}],
            "transitions": [{
                "name": "update", "clock_domain": "main", "trigger": "clk",
                "guard": guard, "action": "acc = din", "latency": 0,
                "transition_type": "sequential",
                "update_semantics": "non_blocking"}],
            "sequential_processes": [{"name": "main_proc",
                                      "clock_domain": "main",
                                      "transitions": ["update"]}],
            "priority": [],
        }

    @staticmethod
    def synthetic_question(anchors=None, patch=None, **extra):
        patch = patch if patch is not None else [
            {"op": "replace", "path": (anchors or ["/derived_signals/0/expression"])[0],
             "value": "din"}]
        values = dict(
            id="Q1", category="C8", template="width_overflow", text="?",
            anchors=list(anchors or ["/derived_signals/0/expression"]),
            options=[Option("current", patch=[]), Option("new", patch=patch)],
            current="current")
        values.update(extra)
        return Question(**values)

    # ---------------------------------------------------------------- patches
    def test_flatten_keeps_scalar_list_elements_separate(self):
        left = {"transitions": [{"action": ["x = 1", "y = 2"]}]}
        right = {"transitions": [{"action": ["x = 1", "y = 3"]}]}
        diffs = structural_diff(left, right)
        self.assertEqual(diffs, ["/transitions/0/action/1"])
        self.assertTrue(within_site(diffs, ["/transitions/0/action/1"]))
        self.assertTrue(within_site(diffs, ["/transitions/0/action"]))
        self.assertFalse(within_site(diffs, ["/transitions/0/action/0"]))

    def test_empty_list_stays_a_leaf(self):
        self.assertEqual(structural_diff({"x": []}, {"x": []}), [])
        # Growing an empty list reports the old list leaf and the new element;
        # both stay inside the list anchor, an element anchor is too narrow.
        diffs = structural_diff({"x": []}, {"x": [1]})
        self.assertEqual(set(diffs), {"/x", "/x/0"})
        self.assertTrue(within_site(diffs, ["/x"]))
        self.assertFalse(within_site(diffs, ["/x/0"]))

    def test_name_addressed_pointer_is_rewritten_to_an_index(self):
        ir = {"derived_signals": [
            {"name": "other", "expression": "1'b0"},
            {"name": "read_data", "expression": "a"},
        ]}
        q = self.synthetic_question(
            anchors=["/derived_signals/read_data/expression"],
            patch=[{"op": "replace",
                    "path": "/derived_signals/read_data/expression",
                    "value": "b"}])
        self.assertEqual(normalise_question_pointers(ir, q), [])
        self.assertEqual(q.anchors, ["/derived_signals/1/expression"])
        self.assertEqual(q.options[1].patch[0]["path"],
                         "/derived_signals/1/expression")

    def test_ambiguous_name_pointer_is_reported(self):
        ir = {"states": [{"name": "dup"}, {"name": "dup"}]}
        q = self.synthetic_question(anchors=["/states/dup"],
                                    patch=[{"op": "replace",
                                            "path": "/states/dup",
                                            "value": 1}])
        self.assertTrue(normalise_question_pointers(ir, q))
        self.assertEqual(q.anchors, ["/states/dup"])

    def test_add_path_keeps_a_new_field(self):
        ir = {"states": [{"name": "s"}]}
        q = self.synthetic_question(
            anchors=["/states/0"],
            patch=[{"op": "add", "path": "/states/0/depth", "value": 16}])
        self.assertEqual(normalise_question_pointers(ir, q), [])
        self.assertEqual(q.options[1].patch[0]["path"], "/states/0/depth")

    # ---------------------------------------------------------------- closure
    def test_stale_pointers_are_detected_after_reindex(self):
        ir = {"transitions": [{"name": "a", "action": ["x = 1"]},
                              {"name": "b", "action": ["x = 2"]}]}
        stale = self.synthetic_question(
            anchors=["/transitions/2/action"],
            patch=[{"op": "replace", "path": "/transitions/2/action",
                    "value": ["x = 3"]}])
        self.assertTrue(_stale_pointers(ir, stale))
        fresh_ir = {"transitions": ir["transitions"] + [
            {"name": "c", "action": ["x = 3"]}]}
        fresh = self.synthetic_question(
            anchors=["/transitions/2/action"],
            patch=[{"op": "replace", "path": "/transitions/2/action",
                    "value": ["x = 4"]}])
        self.assertFalse(_stale_pointers(fresh_ir, fresh))

    # ---------------------------------------------------------------- graph
    def _graph(self, document):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ir.yaml"
            path.write_text(yaml.safe_dump(document, sort_keys=False),
                            encoding="utf-8")
            return build_ir_graph(path)

    def test_verified_dependency_path_is_observable(self):
        document = self.compiler_ir()
        graph = self._graph(document)
        result = structural_observability(graph, document, ["/transitions/0"])
        self.assertTrue(result["observable"], result)
        self.assertEqual(result["basis"], "verified")

    def test_inferred_edge_cannot_prove_observability(self):
        document = self.compiler_ir(derived="$signed(acc)")
        graph = self._graph(document)
        inferred = [edge for edge in graph["edges"]
                    if edge.get("verification_state") == "inferred"]
        self.assertTrue(inferred)
        result = structural_observability(graph, document, ["/transitions/0"])
        self.assertFalse(result["known"])
        self.assertIsNone(result["observable"])
        self.assertEqual(result["basis"], "inferred")

    def test_generate_anchor_maps_to_expanded_instances(self):
        document = {
            "module": {"name": "gen", "parameters": {"N": 2}, "ports": [
                {"name": "din", "direction": "input", "width": 1},
                {"name": "out", "direction": "output", "width": 1}]},
            "generate": [
                {"name": "lanes", "for": {"var": "i", "start": 0, "stop": "N"},
                 "emit": [{"derived_signal": {
                     "name": "lane_{i}", "width": 1, "expression": "din"}}]},
                {"name": "pack", "for": {"var": "unused", "start": 0, "stop": 1},
                 "emit": [{"derived_signal": {
                     "name": "out", "width": 1, "expression": "lane_0"}}]},
            ],
        }
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "ir.yaml"
            path.write_text(yaml.safe_dump(document, sort_keys=False),
                            encoding="utf-8")
            graph = build_ir_graph(path)
        anchor = "/generate/0/emit/0/derived_signal/expression"
        self.assertEqual(resolve_anchor_nodes(graph, document, [anchor]),
                         ["IR:derived:lane_0", "IR:derived:lane_1"])
        result = structural_observability(graph, document, [anchor])
        self.assertTrue(result["observable"], result)
        self.assertEqual(result["basis"], "verified")

    def test_constant_select_keeps_a_verified_path(self):
        document = self.compiler_ir(derived="acc[0]")
        graph = self._graph(document)
        result = structural_observability(graph, document, ["/transitions/0"])
        self.assertTrue(result["observable"], result)
        self.assertEqual(result["basis"], "verified")

    # ---------------------------------------------------------------- answerer
    def _answer(self, payload, anchors=None):
        case = SimpleNamespace(prompt_text=lambda: "spec",
                               reference_text=lambda: "module m; endmodule")
        ir = {"derived_signals": [{"name": "fire",
                                   "expression": "valid && ready"}]}
        question = self.synthetic_question(
            anchors=anchors or ["/derived_signals/0/expression"])
        answerer = OracleAnswerer(case, FakeLLM(payload), ir)
        return answerer.answer(question)

    def test_oracle_other_replacement_is_used_directly(self):
        answer = self._answer({"value": "other", "reason": "x",
                               "replacement": {"pointer": "/derived_signals/0",
                                               "value": 1}})
        self.assertEqual(answer["value"], "other")
        self.assertEqual(answer["replacement"]["pointer"], "/derived_signals/0")

    def test_oracle_other_on_anchor_is_accepted(self):
        answer = self._answer({"value": "other", "reason": "x",
                               "replacement": {
                                   "pointer": "/derived_signals/0/expression",
                                   "value": "valid"}})
        self.assertEqual(answer["value"], "other")
        self.assertEqual(answer["replacement"]["value"], "valid")

    # ---------------------------------------------------------------- questions
    def test_handshake_candidate_drops_a_declared_ready_only(self):
        declared = {
            "module": {"ports": [
                {"name": "valid", "direction": "input", "width": 1},
                {"name": "ready", "direction": "input", "width": 1}]},
            "derived_signals": [{"name": "fire", "expression": "valid && ready"}],
        }
        questions = [q for q in expand_ir_question_templates(declared)
                     if q.category == "C5"]
        self.assertEqual(len(questions), 1)
        self.assertEqual(questions[0].options[1].patch[0]["value"], "valid")
        undeclared = {"derived_signals": [
            {"name": "fire", "expression": "valid && ready"}]}
        self.assertEqual([q for q in expand_ir_question_templates(undeclared)
                          if q.category == "C5"], [])

    def test_complex_handshake_is_left_to_generic_materialization(self):
        document = {
            "module": {"ports": [
                {"name": "valid", "direction": "input", "width": 1},
                {"name": "ready", "direction": "input", "width": 1},
                {"name": "grant", "direction": "input", "width": 1}]},
            "derived_signals": [{"name": "fire",
                                 "expression": "(valid && ready) && grant"}],
        }
        self.assertEqual([q for q in expand_ir_question_templates(document)
                          if q.category == "C5"], [])

    def test_c6_clear_candidate_is_mechanically_checked(self):
        document = self.compiler_ir(guard="en")
        candidate = _clear_on_false_candidate(document, 0)
        self.assertIsNotNone(candidate)
        patched = apply_patch(document, candidate["patch"])
        new_name = candidate["patch"][0]["value"]["name"]
        self.assertTrue(new_name.startswith("update__"))
        self.assertEqual(patched["transitions"][-1]["guard"], "!(en)")
        self.assertEqual(patched["transitions"][-1]["action"], ["acc = 0"])
        self.assertIn(new_name, patched["sequential_processes"][0]["transitions"])
        self.assertEqual(patched["priority"][-1],
                         {"higher": "update", "lower": new_name})

    def test_c6_clear_candidate_covers_every_target(self):
        document = self.compiler_ir(guard="en")
        document["states"].append({"name": "tag", "type": "register",
                                   "width": 1, "clock_domain": "main",
                                   "reset_value": "1'b1"})
        document["transitions"][0]["action"] = ["acc = din", "tag = din[0]"]
        candidate = _clear_on_false_candidate(document, 0)
        self.assertIsNotNone(candidate)
        new_transition = candidate["patch"][0]["value"]
        self.assertEqual(new_transition["action"], ["acc = 0", "tag = 1'b1"])
        patched = apply_patch(document, candidate["patch"])
        self.assertEqual(len(patched["transitions"]), 2)

    def test_c6_skips_writers_in_another_process(self):
        document = self.compiler_ir(guard="en")
        document["transitions"].append({
            "name": "other_update", "clock_domain": "main", "trigger": "clk",
            "guard": "1'b1", "action": "acc = din", "latency": 0,
            "transition_type": "sequential", "update_semantics": "non_blocking"})
        document["sequential_processes"].append({
            "name": "other_proc", "clock_domain": "main",
            "transitions": ["other_update"]})
        self.assertIsNone(_clear_on_false_candidate(document, 0))

    def test_c6_skips_memory_target_and_constant_guard(self):
        memory = self.compiler_ir(guard="en")
        memory["states"][0].update({"type": "memory", "depth": 16})
        self.assertIsNone(_clear_on_false_candidate(memory, 0))
        self.assertIsNone(_clear_on_false_candidate(self.compiler_ir(), 0))

    # ---------------------------------------------------------------- prompts
    def test_inventory_prompt_requires_an_ir_spec_difference_audit(self):
        messages = build_messages("spec text", "derived_signals: []")
        system = messages[0]["content"]
        user = messages[1]["content"]
        self.assertIn("逐条对照", system)
        self.assertIn("发现任何语义差异都要提问", system)
        self.assertNotIn("规格已经明确写出的，不要问", system)
        self.assertIn("发现任何差异都要提出问题", user)

    def test_llm_chat_omits_max_tokens_when_uncapped(self):
        captured = {}

        class Completions:
            def create(self, **kwargs):
                captured.update(kwargs)
                return SimpleNamespace(
                    choices=[SimpleNamespace(message=SimpleNamespace(content="ok"),
                                             finish_reason="stop")],
                    usage=SimpleNamespace(prompt_tokens=1, completion_tokens=1))

        llm = LLM.__new__(LLM)
        llm.client = SimpleNamespace(chat=SimpleNamespace(completions=Completions()))
        llm.model = "fake"
        llm.ledger = None
        llm.calls = []
        llm.prompt_tokens = 0
        llm.completion_tokens = 0
        llm.chat([{"role": "user", "content": "hi"}], max_tokens=None)
        self.assertNotIn("max_tokens", captured)
        llm.chat([{"role": "user", "content": "hi"}], max_tokens=123)
        self.assertEqual(captured["max_tokens"], 123)

    # ---------------------------------------------------------------- frontend
    def _frontend_root(self, root):
        for relative in ("IRgeneration/ir_generate/gen_petrinet_ir.py",
                         "IRgeneration/llm_stream.py"):
            path = root / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("# frontend\n", encoding="utf-8")
        return root

    def test_generation_contract_is_versioned_and_fingerprinted(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._frontend_root(Path(tmp))
            original = resolve_generation_contract({"grammar_contract": "none"})
            contract = resolve_generation_contract({})
            self.assertEqual(contract["version"], "ir-grammar-v1")
            self.assertTrue(contract["enabled"])
            self.assertNotEqual(
                frontend_fingerprint(root, contract=original),
                frontend_fingerprint(root, contract=contract))
            self.assertTrue(frontend_fingerprint(root).startswith(
                "test-monolithic-ir-v2:"))
            with self.assertRaises(ValueError):
                resolve_generation_contract({"grammar_contract": "v9"})

    def test_contract_file_and_variant_are_recorded(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = self._frontend_root(Path(tmp))
            spec = root / "spec.txt"
            spec.write_text("module specification", encoding="utf-8")
            output = root / "out" / "ir.yaml"
            captured = {}

            def fake_run(command, **kwargs):
                captured["command"] = command
                Path(command[command.index("--output") + 1]).write_text(
                    "module:\n  name: X\n", encoding="utf-8")
                Path(command[command.index("--metadata") + 1]).write_text(
                    "{}", encoding="utf-8")
                return SimpleNamespace(returncode=0, stdout="")

            with patch("blocks.b3_phase_a.test_frontend.subprocess.run",
                       side_effect=fake_run):
                _, metadata = generate_with_test_frontend(
                    spec, output, {"OPENAI_API_KEY": "x", "OPENAI_BASE_URL": "y",
                                   "OPENAI_MODEL": "z"}, test_root=root)
            contract_file = Path(metadata["frontend_contract_file"])
            self.assertTrue(contract_file.is_file())
            self.assertIn("generate loops",
                          contract_file.read_text(encoding="utf-8"))
        command = captured["command"]
        self.assertIn("--contract", command)
        self.assertEqual(command[command.index("--contract-version") + 1],
                         "ir-grammar-v1")
        self.assertEqual(metadata["frontend_variant"], "ir-grammar-v1")

    # ---------------------------------------------------------------- inventory
    def _inventory_ir(self):
        return {
            "module": {"name": "inventory", "ports": [
                {"name": "in_op", "direction": "input", "width": 3},
                {"name": "y", "direction": "output", "width": 1}]},
            "derived_signals": [
                {"name": "is_mul", "width": 1, "expression": "in_op <= 3'd3"},
                {"name": "y", "width": 1, "expression": "is_mul"},
            ],
        }

    @staticmethod
    def _passing_lint(_path):
        return {"ok": True, "status": "passed", "errors": []}

    def test_candidate_option_must_pass_full_battery_and_lint(self):
        ir = self._inventory_ir()
        broken = self.synthetic_question(patch=[{
            "op": "replace", "path": "/derived_signals/0/expression",
            "value": "invented_signal & in_op"}])
        with tempfile.TemporaryDirectory() as tmp:
            problems = option_compile_problems(ir, broken, Path(tmp),
                                               lint=self._passing_lint)
        self.assertTrue(problems)
        self.assertIn("checker2", problems[0])

    def test_candidate_outside_anchors_is_allowed(self):
        ir = self._inventory_ir()
        question = self.synthetic_question(
            anchors=["/derived_signals/0/expression"],
            patch=[{"op": "replace",
                    "path": "/derived_signals/1/expression",
                    "value": "in_op == 3'd4"}])
        with tempfile.TemporaryDirectory() as tmp:
            problems = option_compile_problems(ir, question, Path(tmp),
                                               lint=self._passing_lint)
        self.assertEqual(problems, [])

    def test_candidate_lint_failure_is_a_problem(self):
        ir = self._inventory_ir()
        question = self.synthetic_question(patch=[{
            "op": "replace", "path": "/derived_signals/0/expression",
            "value": "in_op == 3'd4"}])
        with tempfile.TemporaryDirectory() as tmp:
            problems = option_compile_problems(
                ir, question, Path(tmp),
                lint=lambda _path: {"ok": False, "status": "failed",
                                    "errors": ["WIDTH"]})
        self.assertTrue(any("lint failed" in item for item in problems))

    def test_inventory_rewrites_the_whole_tree_when_invalid(self):
        ir = self._inventory_ir()
        ir_text = yaml.safe_dump(ir, sort_keys=False)
        broken = {"questions": [{
            "id": "Q1", "category": "C8", "template": "width_overflow",
            "text": "width?", "anchors": ["/derived_signals/0/expression"],
            "given": "MULHSU must use signed*lhs", "options": [
                {"value": "current", "label": "keep", "patch": []},
                {"value": "new", "label": "new", "patch": [{
                    "op": "replace", "path": "/derived_signals/0/expression",
                    "value": "invented_signal & in_op"}]}],
            "current": "current"}]}
        repaired = {"questions": [{
            "id": "Q1", "category": "C8", "template": "width_overflow",
            "text": "width?", "anchors": ["/derived_signals/0/expression"],
            "options": [
                {"value": "current", "label": "keep", "patch": []},
                {"value": "new", "label": "new", "patch": [{
                    "op": "replace", "path": "/derived_signals/0/expression",
                    "value": "in_op == 3'd3"}]}],
            "current": "current"}]}

        class SequenceLLM:
            def __init__(self, values):
                self.values = list(values)
                self.labels = []

            def json(self, messages, **kwargs):
                self.labels.append(kwargs.get("label"))
                return self.values.pop(0)

        llm = SequenceLLM([broken, repaired])
        questions, notes = plan_questions(llm, "spec", ir_text, ir, rounds=2,
                                          max_tokens=100,
                                          lint=self._passing_lint)
        self.assertEqual([q.id for q in questions], ["Q1"])
        self.assertEqual(questions[0].options[1].patch[0]["value"],
                         "in_op == 3'd3")
        self.assertTrue(any("question tree invalid" in note for note in notes))
        self.assertIn("question_tree_rewrite", llm.labels)

    def test_question_tree_rewrite_keeps_earlier_differences(self):
        ir = self._inventory_ir()
        ir_text = yaml.safe_dump(ir, sort_keys=False)

        def question(qid, ref):
            return {
                "id": qid, "category": "C8", "template": "expression_choice",
                "text": f"difference {qid}",
                "anchors": [f"/derived_signals/{ref}/expression"],
                "options": [
                    {"value": "current", "label": "keep", "patch": []},
                    {"value": "new", "label": "new", "patch": [{
                        "op": "replace",
                        "path": f"/derived_signals/{ref}/expression",
                        "value": "in_op == 3'd4"}]}],
                "current": "current"}

        broken = {"questions": [question("Q1", 0),
                                {"id": "Q2", "category": "C8",
                                 "template": "expression_choice", "text": "bad",
                                 "anchors": ["/derived_signals/1/expression"],
                                 "options": [{"value": "current", "patch": []}],
                                 "current": "current"}]}
        rewritten = {"questions": [question("Q1", 0), question("Q2", 1)]}

        class SequenceLLM:
            def __init__(self, values):
                self.values = list(values)

            def json(self, messages, **kwargs):
                return self.values.pop(0)

        questions, _ = plan_questions(SequenceLLM([broken, rewritten]), "spec",
                                      ir_text, ir, rounds=2,
                                      lint=self._passing_lint)
        self.assertEqual([q.id for q in questions], ["Q1", "Q2"])

    def test_inventory_drops_questions_without_a_modify_option(self):
        ir = self._inventory_ir()
        ir_text = yaml.safe_dump(ir, sort_keys=False)
        broken = {"questions": [{
            "id": "Q1", "category": "C7", "template": "mem_conflict",
            "text": "no concrete change", "anchors": [],
            "options": [{"value": "current", "patch": []},
                        {"value": "new", "patch": []}],
            "current": "current"}]}

        class BrokenLLM:
            def json(self, messages, **kwargs):
                return broken

        questions, notes = plan_questions(BrokenLLM(), "spec", ir_text, ir,
                                          rounds=1, max_tokens=100,
                                          lint=self._passing_lint)
        self.assertEqual(questions, [])
        self.assertTrue(any("dropped invalid questions" in note for note in notes))

    # ---------------------------------------------------------------- derive
    def test_derive_rejects_an_ir_changed_after_answering(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ir_path = root / "ir.yaml"
            ir_path.write_text("module:\n  name: X\n", encoding="utf-8")
            ctx = FakeContext(root / "run")
            ctx.directory.mkdir(parents=True)
            with self.assertRaises(RuntimeError):
                derive_final(ir_path, {"ir_exp_sha256": "deadbeef",
                                       "committed_patches": []}, [], ctx)

    def test_candidate_lint_failure_is_rejected(self):
        document = self.compiler_ir() if hasattr(self, "compiler_ir") else None
        if document is None:
            with tempfile.TemporaryDirectory() as tmp:
                document = yaml.safe_load((Path(tmp) / "x").read_text()) if False else None
        text = yaml.safe_dump(self.compiler_ir(), sort_keys=False) if hasattr(self, "compiler_ir") else ""
        with tempfile.TemporaryDirectory() as tmp:
            result = check_candidate_text(
                text, Path(tmp), compile_rtl=True,
                lint=lambda _path: {"ok": False, "status": "failed",
                                    "errors": ["WIDTH"]})
        self.assertFalse(result["ok"])
        self.assertEqual(result.get("stage"), "lint")

    def test_check_ir_runs_lint_when_verilator_is_given(self):
        document = self.compiler_ir()
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            ir = root / "ir.yaml"
            ir.write_text(yaml.safe_dump(document, sort_keys=False),
                          encoding="utf-8")
            with patch("blocks.b7_phase_c.verilog.lint_rtl",
                       return_value={"ok": False, "status": "failed",
                                     "errors": ["%" + "Error-WIDTH"]}):
                report = check_ir(ir, root / "checks", compile_smoke=True,
                                  emit_rtl=root / "rtl.v", verilator="verilator")
        self.assertEqual(report["status"], "failed")
        self.assertEqual(report["reason"], "RTL lint")

    # ---------------------------------------------------------------- result
    def test_result_counts_questions_instead_of_serializing_them(self):
        first = self.synthetic_question()
        second = self.synthetic_question()
        phase = {"questions": [first, second],
                 "coverage": {"closed": True, "coverage": 1.0,
                              "questions": [first, second]}}
        summary = _representability_summary(phase)
        self.assertEqual(summary["questions"], 2)
        self.assertEqual(summary["coverage"], 1.0)

    # ---------------------------------------------------------------- compiler
    def test_derived_less_equal_is_a_comparison_not_an_assignment(self):
        document = {
            "module": {"name": "cmp", "ports": [
                {"name": "in_op", "direction": "input", "width": 3},
                {"name": "is_mul", "direction": "output", "width": 1}]},
            "derived_signals": [
                {"name": "is_mul", "width": 1, "expression": "in_op <= 3'd3"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            ir = Path(tmp) / "ir.yaml"
            ir.write_text(yaml.safe_dump(document, sort_keys=False),
                          encoding="utf-8")
            rtl = Path(tmp) / "rtl.v"
            compile_ir(ir, rtl, Path(tmp) / "report.json")
            text = rtl.read_text(encoding="utf-8")
        self.assertIn("assign is_mul = in_op <= 3'd3;", text)

    def test_redundant_assignment_prefix_is_still_stripped(self):
        document = {
            "module": {"name": "cmp2", "ports": [
                {"name": "in_op", "direction": "input", "width": 3}]},
            "derived_signals": [
                {"name": "is_mul", "width": 1,
                 "expression": "is_mul = in_op <= 3'd3"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            ir = Path(tmp) / "ir.yaml"
            ir.write_text(yaml.safe_dump(document, sort_keys=False),
                          encoding="utf-8")
            rtl = Path(tmp) / "rtl.v"
            compile_ir(ir, rtl, Path(tmp) / "report.json")
            text = rtl.read_text(encoding="utf-8")
        self.assertIn("assign is_mul = in_op <= 3'd3;", text)

    def test_function_local_select_is_rejected(self):
        document = {
            "functions": [{
                "name": "half", "params": [{"name": "a", "width": 8},
                                           {"name": "b", "width": 8}],
                "returns": {"width": 8},
                "body": [
                    {"assign": {"target": "prod", "expr": "a * b"}},
                    {"return": "prod[15:8]"},
                ],
            }],
        }
        with self.assertRaises(ElaborationError):
            elaborate(document)

    def test_select_on_expression_is_rejected(self):
        document = {
            "module": {"name": "sel", "ports": [
                {"name": "a", "direction": "input", "width": 8},
                {"name": "b", "direction": "input", "width": 8},
                {"name": "y", "direction": "output", "width": 8}]},
            "derived_signals": [
                {"name": "y", "width": 8, "expression": "(a * b)[15:8]"}],
        }
        with tempfile.TemporaryDirectory() as tmp:
            result = check_candidate_text(
                yaml.safe_dump(document, sort_keys=False), Path(tmp),
                compile_rtl=False)
        self.assertFalse(result["ok"])
        self.assertIn("bit/part-select", result.get("error", ""))

    def test_lint_reports_invalid_verilog(self):
        if not Path(DEFAULT_VERILATOR).is_file():
            self.skipTest("verilator is not installed in this checkout")
        with tempfile.TemporaryDirectory() as tmp:
            rtl = Path(tmp) / "bad.v"
            rtl.write_text("module bad;\nassign x = ;\nendmodule\n",
                           encoding="utf-8")
            result = lint_rtl(rtl, verilator=DEFAULT_VERILATOR)
        self.assertFalse(result["ok"])


if __name__ == "__main__":
    unittest.main()
