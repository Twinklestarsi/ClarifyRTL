#!/usr/bin/env python3
"""Validate a question tree; the only repair branch is a full rewrite.

Usage:
    python src/check_questions.py --run-dir runs/<case>/<run_id>
    python src/check_questions.py --run-dir ... --rewrite
    python src/check_questions.py --spec spec.md --ir ir.yaml --questions q.json
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

HERE = Path(__file__).resolve()
DAEMON_ROOT = HERE.parents[1]
PROJECT_ROOT = HERE.parents[3]
for path in (str(DAEMON_ROOT), str(PROJECT_ROOT)):
    if path not in sys.path:
        sys.path.insert(0, path)

from blocks.b2_site_model.patches import load_ir
from blocks.b2_site_model.schema import Question
from blocks.b3_phase_a.question_tree import (question_tree_problems,
                                             rewrite_question_tree)
from blocks.b7_phase_c.verilog import lint_rtl


def _load_questions(path: Path) -> list[Question]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data.get("questions", data) if isinstance(data, dict) else data
    return [Question.from_dict(row) for row in rows]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-dir")
    parser.add_argument("--spec")
    parser.add_argument("--ir")
    parser.add_argument("--questions")
    parser.add_argument("--config")
    parser.add_argument("--rewrite", action="store_true")
    args = parser.parse_args()

    from blocks.b1_scaffold import get_case, load_config, load_dotenv, provider_env
    from blocks.b1_scaffold.llm import LLM

    config = load_config(args.config)
    verilator = config["rtl"].get("verilator")

    def lint(rtl_path):
        return lint_rtl(rtl_path, verilator=verilator)

    if args.run_dir:
        run_dir = Path(args.run_dir).resolve()
        # The tree is generated against the initial IR; IR_exp may have been
        # rewritten by closure and is not the file the anchors were checked on.
        ir_path = run_dir / "phase_a" / "init" / "ir.yaml"
        if not ir_path.is_file():
            ir_path = run_dir / "phase_a" / "ir_exp.yaml"
        questions_path = run_dir / "phase_a" / "inventory.json"
        run_config = json.loads((run_dir / "run_config.json").read_text(encoding="utf-8"))
        case_key = str(run_config.get("case", ""))
        case = get_case(config["run"]["case_root"], case_key) if case_key else None
        spec_text = case.prompt_text() if case else ""
        output = run_dir / "phase_a" / "question_tree_check.json"
    else:
        if not (args.spec and args.ir and args.questions):
            parser.error("provide --run-dir or --spec/--ir/--questions")
        ir_path = Path(args.ir).resolve()
        questions_path = Path(args.questions).resolve()
        spec_text = Path(args.spec).read_text(encoding="utf-8")
        output = questions_path.with_name("question_tree_check.json")

    ir = load_ir(ir_path)
    questions = _load_questions(questions_path)
    problems = question_tree_problems(ir, questions, lint=lint)
    result = {"ir": str(ir_path), "questions": len(questions),
              "ok": not problems, "problems": problems}
    if problems and args.rewrite:
        load_dotenv()
        env = provider_env()
        llm = LLM(ledger=output.parent / "question_tree_llm.jsonl")
        rewritten = rewrite_question_tree(
            llm, spec_text, ir_path.read_text(encoding="utf-8"), ir,
            questions, problems)
        if rewritten is None:
            result["rewrite"] = "failed"
        else:
            rewritten_path = output.with_name("question_tree_rewritten.json")
            rewritten_path.write_text(json.dumps(
                {"questions": [question.to_dict() for question in rewritten]},
                ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
            after = question_tree_problems(ir, rewritten, lint=lint)
            result["rewrite"] = {"path": str(rewritten_path),
                                 "questions": len(rewritten),
                                 "ok": not after, "problems": after}
    output.write_text(json.dumps(result, ensure_ascii=False, indent=2) + "\n",
                      encoding="utf-8")
    print(json.dumps(result, ensure_ascii=False, indent=2)[:4000])
    return 0 if result["ok"] or result.get("rewrite", {}).get("ok") else 1


if __name__ == "__main__":
    raise SystemExit(main())
