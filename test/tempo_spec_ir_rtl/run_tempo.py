#!/usr/bin/env python3
"""Run the extracted dac2026_c1 Spec -> HW-IR -> RTL pipeline on Tempo26 cases."""

from __future__ import annotations

import argparse
from concurrent.futures import ThreadPoolExecutor, as_completed
import hashlib
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent
MANIFEST = ROOT / "tempo_cases.json"
DEFAULT_DATA_ROOT = Path(
    "/home/2531930/nas/2531930-2/ONE-TURN/tempo26/vortex"
)
EDA_CONDA_BIN = Path(
    "/home/2531930/nas/2531930-2/eda_tools/conda/bin"
)


def read_json(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def write_json(path: Path, value) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(value, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def load_env_file(path: Path) -> dict[str, str]:
    values: dict[str, str] = {}
    for number, raw in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[7:].strip()
        key, separator, value = line.partition("=")
        key = key.strip()
        if not separator or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", key):
            raise ValueError(f"{path}:{number}: expected KEY=VALUE")
        value = value.strip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in {"'", '"'}:
            value = value[1:-1]
        values[key] = value
    return values


def load_cases(data_root: Path) -> list[dict]:
    payload = read_json(MANIFEST)
    rows = payload.get("cases")
    if payload.get("schema_version") != 1 or not isinstance(rows, list):
        raise ValueError(f"invalid case manifest: {MANIFEST}")
    names: set[str] = set()
    resolved = []
    for row in rows:
        name = str(row.get("name", ""))
        if not re.fullmatch(r"[a-z][a-z0-9_]*", name) or name in names:
            raise ValueError(f"invalid or duplicate Tempo case name: {name!r}")
        names.add(name)
        directory = data_root / str(row["directory"])
        paths = {
            "spec": directory / str(row["spec"]),
            "reference": directory / str(row["reference"]),
            "testbench": directory / str(row["testbench"]),
        }
        missing = [label for label, path in paths.items() if not path.is_file()]
        if missing:
            raise FileNotFoundError(
                f"{name}: missing {', '.join(missing)} under {directory}"
            )
        resolved.append({**row, "directory_path": directory, **paths})
    if len(resolved) != 10:
        raise ValueError(f"expected exactly 10 Tempo cases, found {len(resolved)}")
    return resolved


def select_cases(rows: list[dict], requested: list[str], run_all: bool) -> list[dict]:
    by_name = {row["name"]: row for row in rows}
    if run_all:
        return rows
    names = []
    for item in requested:
        names.extend(part.strip() for part in item.split(",") if part.strip())
    if not names:
        raise ValueError("select at least one --case, or pass --all explicitly")
    unknown = sorted(set(names) - set(by_name))
    if unknown:
        raise ValueError("unknown Tempo case(s): " + ", ".join(unknown))
    return [by_name[name] for name in dict.fromkeys(names)]


def build_command(
    args,
    spec_snapshot: Path,
    testbench_snapshot: Path,
    generation_dir: Path,
) -> list[str]:
    command = [
        sys.executable,
        str(ROOT / "main.py"),
        str(spec_snapshot),
        "--generation-dir",
        str(generation_dir),
        "--repair",
        "--testbench",
        str(testbench_snapshot),
        "--no-assumption-analysis",
        "--skip-pre-ir-analysis",
        "--max-hw-ir-repair-iterations",
        str(args.max_ir_repairs),
        "--temperature",
        str(args.temperature),
        "--max-tokens",
        str(args.max_tokens),
    ]
    if args.model:
        command.extend(["--model", args.model])
    if args.base_url:
        command.extend(["--base-url", args.base_url])
    return command


def run_one(row: dict, run_root: Path, args, environment: dict[str, str]) -> dict:
    case_root = run_root / row["name"]
    if case_root.exists():
        raise FileExistsError(f"refusing to overwrite existing case directory: {case_root}")
    input_dir = case_root / "input"
    reference_dir = case_root / "reference"
    generation_dir = case_root / "generation"
    input_dir.mkdir(parents=True)
    reference_dir.mkdir(parents=True)

    spec_snapshot = input_dir / "design_description.txt"
    testbench_snapshot = reference_dir / row["testbench"].name
    shutil.copy2(row["spec"], spec_snapshot)
    shutil.copy2(row["reference"], reference_dir / row["reference"].name)
    shutil.copy2(row["testbench"], testbench_snapshot)

    config = {
        "case": row["name"],
        "source_directory": str(row["directory_path"]),
        "input_spec": str(spec_snapshot),
        "input_spec_sha256": sha256(spec_snapshot),
        "reference_rtl": str(reference_dir / row["reference"].name),
        "reference_rtl_sha256": sha256(row["reference"]),
        "testbench": str(testbench_snapshot),
        "testbench_sha256": sha256(row["testbench"]),
        "pipeline": "spec_to_hw_ir_to_deterministic_rtl",
        "generator_sees_reference_or_testbench": False,
        "verification_strategy": "verilator_rtl_first_sva_on_failure",
        "model": args.model or environment.get("OPENAI_MODEL"),
        "base_url": args.base_url or environment.get("OPENAI_BASE_URL"),
        "api_key_configured": bool(environment.get("OPENAI_API_KEY")),
        "max_ir_repairs": args.max_ir_repairs,
        "temperature": args.temperature,
        "max_tokens": args.max_tokens,
    }
    write_json(case_root / "case_config.json", config)
    command = build_command(
        args, spec_snapshot, testbench_snapshot, generation_dir
    )
    started = datetime.now(timezone.utc).isoformat()
    with (case_root / "pipeline.log").open("w", encoding="utf-8") as log:
        completed = subprocess.run(
            command,
            cwd=ROOT,
            env=environment,
            stdout=log,
            stderr=subprocess.STDOUT,
            text=True,
        )
    pipeline_manifest = generation_dir / "pipeline_manifest.json"
    pipeline = read_json(pipeline_manifest) if pipeline_manifest.is_file() else None
    result = {
        "case": row["name"],
        "status": "passed" if completed.returncode == 0 else "failed",
        "returncode": completed.returncode,
        "started_at": started,
        "finished_at": datetime.now(timezone.utc).isoformat(),
        "generation_dir": str(generation_dir),
        "ir": str(generation_dir / "ir.yaml") if (generation_dir / "ir.yaml").is_file() else None,
        "rtl": str(generation_dir / "generated_rtl.v") if (generation_dir / "generated_rtl.v").is_file() else None,
        "pipeline_manifest": str(pipeline_manifest) if pipeline_manifest.is_file() else None,
        "pipeline_status": pipeline.get("status") if isinstance(pipeline, dict) else None,
        "log": str(case_root / "pipeline.log"),
    }
    write_json(case_root / "result.json", result)
    return result


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(
        description="Tempo26 Spec -> HW-IR -> deterministic RTL experiment"
    )
    parser.add_argument("--list", action="store_true", help="list and validate the 10 cases")
    parser.add_argument("--case", action="append", default=[], help="case name; repeat or use commas")
    parser.add_argument("--all", action="store_true", help="run all 10 cases explicitly")
    parser.add_argument("--dry-run", action="store_true", help="print commands without creating outputs")
    parser.add_argument("--data-root", type=Path, default=DEFAULT_DATA_ROOT)
    parser.add_argument("--output-root", type=Path, default=ROOT / "runs")
    parser.add_argument("--run-id", default=None)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--model")
    parser.add_argument("--base-url")
    parser.add_argument("--max-ir-repairs", type=int, default=3, choices=range(0, 4))
    parser.add_argument("--temperature", type=float, default=0.2)
    parser.add_argument("--max-tokens", type=int, default=32000)
    parser.add_argument(
        "--jobs", type=int, default=10, choices=range(1, 11),
        help="number of cases to run concurrently (default: 10)",
    )
    parser.add_argument("--fail-fast", action="store_true")
    args = parser.parse_args(argv)

    rows = load_cases(args.data_root.resolve())
    if args.list:
        for row in rows:
            print(f"{row['name']:14s} spec={row['spec']} reference={row['reference'].name}")
        if not args.case and not args.all:
            return 0
    try:
        selected = select_cases(rows, args.case, args.all)
    except ValueError as exc:
        parser.error(str(exc))
    if args.max_tokens < 1:
        parser.error("--max-tokens must be positive")
    if args.fail_fast and args.jobs != 1:
        parser.error("--fail-fast requires --jobs 1")

    run_id = args.run_id or datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    run_root = args.output_root.resolve() / run_id
    if args.dry_run:
        for row in selected:
            planned = run_root / row["name"]
            command = build_command(
                args,
                planned / "input" / "design_description.txt",
                planned / "reference" / row["testbench"].name,
                planned / "generation",
            )
            print(f"[{row['name']}] {shlex.join(command)}")
        return 0

    environment = dict(os.environ)
    if args.env_file:
        if not args.env_file.is_file():
            parser.error(f"env file not found: {args.env_file}")
        environment.update(load_env_file(args.env_file.resolve()))
    if not EDA_CONDA_BIN.is_dir():
        parser.error(f"EDA Conda bin directory not found: {EDA_CONDA_BIN}")
    environment["PATH"] = (
        str(EDA_CONDA_BIN)
        + os.pathsep
        + environment.get("PATH", "")
    )
    eda_tools = {
        name: shutil.which(name, path=environment["PATH"])
        for name in ("verilator", "iverilog", "vvp")
    }
    missing_tools = [name for name, path in eda_tools.items() if not path]
    if missing_tools:
        parser.error(
            "missing EDA tool(s) after activating Conda bin: "
            + ", ".join(missing_tools)
        )
    if args.model:
        environment["OPENAI_MODEL"] = args.model
    if args.base_url:
        environment["OPENAI_BASE_URL"] = args.base_url
    missing = [
        name for name in ("OPENAI_API_KEY", "OPENAI_BASE_URL", "OPENAI_MODEL")
        if not environment.get(name)
    ]
    if missing:
        parser.error("missing API configuration: " + ", ".join(missing))
    if run_root.exists():
        parser.error(f"run directory already exists: {run_root}")
    run_root.mkdir(parents=True)
    write_json(
        run_root / "run_config.json",
        {
            "run_id": run_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
            "data_root": str(args.data_root.resolve()),
            "cases": [row["name"] for row in selected],
            "model": environment.get("OPENAI_MODEL"),
            "base_url": environment.get("OPENAI_BASE_URL"),
            "api_key_configured": True,
            "reference_is_not_generation_input": True,
            "verification_strategy": "verilator_rtl_first_sva_on_failure",
            "parallel_workers": min(args.jobs, len(selected)),
            "eda_conda_bin": str(EDA_CONDA_BIN),
            "verilator": eda_tools["verilator"],
            "iverilog": eda_tools["iverilog"],
            "vvp": eda_tools["vvp"],
        },
    )

    def execute_row(row: dict) -> dict:
        try:
            return run_one(row, run_root, args, environment)
        except Exception as exc:
            result = {
                "case": row["name"],
                "status": "crashed",
                "error_type": type(exc).__name__,
                "error": str(exc),
            }
            write_json(run_root / row["name"] / "result.json", result)
            return result

    results_by_case: dict[str, dict] = {}
    if args.fail_fast:
        for row in selected:
            print(f"[tempo] {row['name']}: spec -> IR -> RTL + Verilator", flush=True)
            result = execute_row(row)
            results_by_case[row["name"]] = result
            print(f"[tempo] {row['name']}: {result['status']}", flush=True)
            if result["status"] != "passed":
                break
    else:
        worker_count = min(args.jobs, len(selected))
        print(
            f"[tempo] launching {len(selected)} case(s) with "
            f"{worker_count} parallel worker(s)",
            flush=True,
        )
        with ThreadPoolExecutor(max_workers=worker_count) as executor:
            futures = {
                executor.submit(execute_row, row): row
                for row in selected
            }
            for future in as_completed(futures):
                row = futures[future]
                result = future.result()
                results_by_case[row["name"]] = result
                print(
                    f"[tempo] {row['name']}: {result['status']}",
                    flush=True,
                )

    results = [
        results_by_case[row["name"]]
        for row in selected
        if row["name"] in results_by_case
    ]

    summary = {
        "run_id": run_id,
        "cases_requested": len(selected),
        "cases_finished": len(results),
        "passed": sum(row["status"] == "passed" for row in results),
        "failed": sum(row["status"] != "passed" for row in results),
        "results": results,
    }
    write_json(run_root / "summary.json", summary)
    print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
