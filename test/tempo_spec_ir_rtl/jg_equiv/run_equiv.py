#!/usr/bin/env python3
"""JasperGold sequential equivalence check for the Tempo Spec -> IR -> RTL flow.

For every case produced by ``run_tempo.py`` this driver:

1. re-parses the generated RTL exactly as emitted (``analyze`` only), to record
   whether the pipeline output is legal SystemVerilog at all;
2. legalizes parameter default expressions with ``normalize_rtl.py`` and renames
   the implementation module so spec and implementation can live in one
   JasperGold session;
3. runs the JasperGold SEC app (``check_sec``) comparing the Tempo reference RTL
   (spec) against the generated RTL (imp): same clock, same synchronous reset,
   identical primary inputs, and all primary outputs compared;
4. records the proof result per case in ``reports/summary.json`` plus a
   human-readable ``reports/summary.md``.

JasperGold needs the HTTP/SOCKS proxy variables removed, otherwise its internal
session server fails to start, so the driver always sanitizes the environment.
"""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
WORK_DEFAULT = ROOT / "jg_equiv" / "work"
REPORT_DEFAULT = ROOT / "jg_equiv" / "reports"

JG_BIN = Path("/tools/Cadence/JASPERGOLD23/bin/jg")
DEFAULT_LICENSE = "27010@lic.tongjieda.cn:/tools/Cadence/Cadence.dat:/tools/Cadence/H3C_Cadence.dat"
PROXY_VARIABLES = (
    "all_proxy", "http_proxy", "https_proxy", "no_proxy",
    "ALL_PROXY", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from normalize_rtl import normalize  # noqa: E402


def cleaned_env() -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if key not in PROXY_VARIABLES}
    env["PATH"] = f"{JG_BIN.parent}:{env.get('PATH', '')}"
    env.setdefault("CDS_LIC_FILE", DEFAULT_LICENSE)
    env.setdefault("LANG", "C.UTF-8")
    return env


def run_jaspergold(work: Path, tcl_name: str, project: str, timeout: int, log_path: Path) -> tuple[int, str, bool]:
    """Run jg on a Tcl script. Returns (returncode, log text, timed_out)."""
    command = [
        str(JG_BIN), "-sec", "-batch", "-allow_unsupported_OS",
        "-proj", project, "-tcl", tcl_name,
    ]
    started = time.time()
    timed_out = False
    try:
        completed = subprocess.run(
            command,
            cwd=work,
            env=cleaned_env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            timeout=timeout,
        )
        returncode, output = completed.returncode, completed.stdout
    except subprocess.TimeoutExpired as exc:
        timed_out = True
        returncode = 124
        output = (exc.stdout or "") if isinstance(exc.stdout, str) else (exc.stdout or b"").decode("utf-8", "replace")
    log_path.write_text(output, encoding="utf-8")
    command_log = [
        f"# command: {' '.join(command)}",
        f"# cwd: {work}",
        f"# elapsed: {time.time() - started:.1f}s",
        "",
    ]
    log_path.write_text("\n".join(command_log) + output, encoding="utf-8")
    return returncode, output, timed_out


def parse_interface(log: str) -> dict:
    result: dict = {}
    for match in re.finditer(r"^(\w[\w ]*?)\s*-\s*(PASSED|FAILED)\s*$", log, re.M):
        key = match.group(1).strip()
        if key in {"Overall interface check result"} or key.startswith("Interface"):
            continue
        result[key] = match.group(2)
    overall = re.search(r"Overall interface check result\s*-\s*(\w+)", log)
    result["overall"] = overall.group(1) if overall else "UNKNOWN"
    unmapped = re.search(r"found (\d+) unmapped signal\(s\) in (spec|imp)", log)
    result["unmapped_spec"] = 0
    result["unmapped_imp"] = 0
    if unmapped:
        result[f"unmapped_{unmapped.group(2)}"] = int(unmapped.group(1))
    return result


def parse_sec_summary(log: str) -> dict[str, int]:
    counts: dict[str, int] = {}
    block = re.search(r"SUMMARY - SEC [A-Z ]+\n=+\n(.*?)(?:\n\S|\Z)", log, re.S)
    if not block:
        return counts
    for raw in block.group(1).splitlines():
        match = re.match(r"\s*:?\s*([A-Za-z_ ()\-]+?)\s*:\s*(\d+)\s*$", raw)
        if not match:
            continue
        key = match.group(1).strip()
        if key.startswith("-"):
            key = key.lstrip("- ").strip()
        counts[key] = int(match.group(2))
    return counts


def parse_targets(log: str) -> dict[str, list[str]]:
    """Collect the per-output verdicts printed by the SEC engine."""
    cex: list[str] = []
    proven: list[str] = []
    for match in re.finditer(r'A counterexample \(cex\) with \d+ cycles was found for the property "([^"]+)"', log):
        name = match.group(1).replace("<embedded>::", "")
        if name not in cex:
            cex.append(name)
    for match in re.finditer(r'The property "([^"]+)" was proven', log):
        name = match.group(1).replace("<embedded>::", "")
        if name not in proven:
            proven.append(name)
    return {"cex_targets": cex, "proven_targets": proven}


def parse_signoff(log: str) -> dict[str, str]:
    detail: dict[str, str] = {}
    pattern = re.compile(
        r"^(Boundary outputs mapping|Boundary outputs proof|User cutpoints proof|"
        r"Imported assertions proof|Imported covers proof|Reset value abstraction|"
        r"X signals and undrivens|Mapped stopats|Assumption management|Overall SEC status)"
        r"\s*-\s*(\w+)\s*$",
        re.M,
    )
    for match in pattern.finditer(log):
        detail[match.group(1).strip()] = match.group(2)
    return detail


_ERROR_LINE = re.compile(r"ERROR \((?P<code>(?:VERI|VDB|ENL)-\d+)\)\]?\s*(?P<message>[^\n]*)")


def first_error(log: str) -> dict | None:
    match = _ERROR_LINE.search(log)
    if not match:
        return None
    return {"code": match.group("code"), "message": match.group("message").strip()}


def classify(returncode: int, log: str, counts: dict[str, int], timed_out: bool) -> tuple[str, str]:
    if timed_out:
        return "timeout", "jG run exceeded the time budget"
    error = first_error(log)
    reason = f"{error['code']}: {error['message']}" if error else None
    if re.search(r"ERROR \(ESEC045\): Unable to elaborate imp top module", log):
        return "impl_elaboration_error", reason or "impl top could not be elaborated"
    if re.search(r"ERROR \(ESEC0\d+\): Unable to elaborate spec top module", log):
        return "spec_elaboration_error", reason or "spec top could not be elaborated"
    if re.search(r"ERROR \(ENL034\): \d+ errors detected in the design file\(s\)", log):
        return "parse_error", reason or "design files failed to analyze/elaborate"
    if "check_sec -prove" in log and not counts:
        return "jg_error", "proof command did not produce a SEC summary"
    if counts:
        if counts.get("cex", 0) > 0:
            return "not_equivalent", "counterexample(s) found on compared outputs"
        if counts.get("undetermined", 0) > 0:
            return "undetermined", "property remained undetermined"
        if counts.get("proven", 0) > 0:
            return "equivalent", "all compared outputs proven equivalent"
    if returncode != 0:
        return "jg_error", "JasperGold exited with an error"
    return "unknown", "no SEC summary found in the log"


def error_source_line(case_work: Path, message: str) -> dict | None:
    """Map a ``impl.v(123): ...`` diagnostic back to the generated source line."""
    match = re.search(r"impl\.v\((\d+)\)", message)
    if not match:
        return None
    line = int(match.group(1))
    impl = case_work / "impl.v"
    if not impl.is_file():
        return None
    lines = impl.read_text(encoding="utf-8").splitlines()
    if not 1 <= line <= len(lines):
        return None
    return {"line": line, "text": lines[line - 1].strip()[:240]}


def raw_parse_check(case_work: Path, impl_source: Path, timeout: int) -> dict:
    target = case_work / "raw"
    target.mkdir(exist_ok=True)
    shutil.copy2(impl_source, target / "impl_raw.v")
    (target / "analyze.tcl").write_text("analyze -sv impl_raw.v\n", encoding="utf-8")
    returncode, log, timed_out = run_jaspergold(target, "analyze.tcl", "jgproj", timeout, target / "analyze.log")
    errors: list[str] = []
    for match in _ERROR_LINE.finditer(log):
        entry = f"{match.group('code')}: {match.group('message').strip()}"
        if entry not in errors:
            errors.append(entry)
    ok = returncode == 0 and not re.search(r"errors detected in the design file", log)
    return {
        "raw_analyze_ok": bool(ok),
        "raw_analyze_returncode": returncode,
        "raw_analyze_errors": errors[:5],
        "raw_analyze_log": str(target / "analyze.log"),
    }


def run_selfcheck(case_work: Path, reference: Path, args) -> dict:
    """Negative control: the reference RTL compared against itself.

    A harness that cannot prove a design equivalent to its own copy would make
    every ``not_equivalent`` verdict meaningless.
    """
    target = case_work / "selfcheck"
    target.mkdir(exist_ok=True)
    text = reference.read_text(encoding="utf-8")
    match = re.search(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_]*)", text)
    if not match:
        return {"selfcheck_status": "driver_error", "selfcheck_reason": "no module found"}
    spec_top = match.group(1)
    impl_top = f"{spec_top}_impl"
    (target / "spec.sv").write_text(text, encoding="utf-8")
    (target / "impl.v").write_text(
        re.sub(r"\bmodule\s+" + re.escape(spec_top) + r"\b", f"module {impl_top}", text, count=1),
        encoding="utf-8",
    )
    (target / "sec.tcl").write_text(
        sec_tcl(spec_top, impl_top, "2m", getattr(args, "init_nonresettable", "0")), encoding="utf-8")
    shutil.rmtree(target / "jgproj", ignore_errors=True)
    returncode, log, timed_out = run_jaspergold(target, "sec.tcl", "jgproj", args.timeout, target / "run.log")
    counts = parse_sec_summary(log)
    status, reason = classify(returncode, log, counts, timed_out)
    return {"selfcheck_status": status, "selfcheck_counts": counts, "selfcheck_log": str(target / "run.log")}


def prepare_case(case_work: Path, reference: Path, generated: Path) -> dict:
    spec = case_work / "spec.sv"
    shutil.copy2(reference, spec)
    impl_raw = case_work / "impl_raw.v"
    shutil.copy2(generated, impl_raw)
    reference_text = reference.read_text(encoding="utf-8")
    generated_text = generated.read_text(encoding="utf-8")
    spec_top_match = re.search(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_]*)", reference_text)
    impl_top_match = re.search(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_]*)", generated_text)
    if not spec_top_match or not impl_top_match:
        raise ValueError("could not determine module names")
    spec_top = spec_top_match.group(1)
    impl_top_raw = impl_top_match.group(1)
    impl_top = f"{impl_top_raw}_impl"
    normalized, steps = normalize(generated_text, reference_text, impl_top_raw, impl_top)
    (case_work / "impl.v").write_text(normalized, encoding="utf-8")
    (case_work / "normalization.json").write_text(
        json.dumps({"module_raw": impl_top_raw, "module_normalized": impl_top, "steps": steps}, indent=2) + "\n",
        encoding="utf-8",
    )
    return {"spec_top": spec_top, "impl_top": impl_top, "impl_top_raw": impl_top_raw,
            "normalization_steps": steps, "reference": reference, "generated": generated}


def sec_tcl(spec_top: str, impl_top: str, prove_limit: str, init_nonresettable: str | None = "0") -> str:
    reset_line = "reset -expression reset"
    if init_nonresettable is not None:
        reset_line += f" -non_resettable_regs {init_nonresettable}"
    return "\n".join([
        "check_sec -setup "
        f"-spec_top {spec_top} -spec_analyze_opts {{-sv spec.sv}} "
        f"-imp_top {impl_top} -imp_analyze_opts {{-sv impl.v}} "
        "-auto_map_reset_x_values",
        "clock clk",
        reset_line,
        "check_sec -interface",
        f"check_sec -prove -strategy basic -time_limit {prove_limit}",
        "check_sec -list -mapping -proof_status cex -global -return text",
        "check_sec -signoff -summary",
        "",
    ])


def run_case(case: str, case_work: Path, reference: Path, generated: Path | None, args) -> dict:
    case_work.mkdir(parents=True, exist_ok=True)
    started = datetime.now(timezone.utc).isoformat()
    record: dict = {
        "case": case,
        "reference": str(reference) if reference else None,
        "generated_rtl": str(generated) if generated else None,
        "started_at": started,
    }
    if generated is None:
        record.update({
            "status": "no_generated_rtl",
            "reason": "the spec -> IR -> RTL pipeline did not produce generated_rtl.v for this case",
            "sec_counts": {},
            "finished_at": datetime.now(timezone.utc).isoformat(),
        })
        (case_work / "result.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
        return record
    prepared = prepare_case(case_work, reference, generated)
    record.update({key: prepared[key] for key in ("spec_top", "impl_top", "impl_top_raw")})
    record["normalization_steps"] = prepared["normalization_steps"]

    if not args.skip_raw:
        record.update(raw_parse_check(case_work, generated, args.timeout))
    else:
        record["raw_analyze_ok"] = None

    (case_work / "sec.tcl").write_text(
        sec_tcl(prepared["spec_top"], prepared["impl_top"], args.prove_limit, args.init_nonresettable),
        encoding="utf-8")
    shutil.rmtree(case_work / "jgproj", ignore_errors=True)
    returncode, log, timed_out = run_jaspergold(case_work, "sec.tcl", "jgproj", args.timeout, case_work / "run.log")
    counts = parse_sec_summary(log)
    status, reason = classify(returncode, log, counts, timed_out)
    record.update({
        "status": status,
        "reason": reason,
        "defect_line": error_source_line(case_work, reason) if status.endswith("error") else None,
        "jg_returncode": returncode,
        "jg_timed_out": timed_out,
        "sec_counts": counts,
        "interface": parse_interface(log),
        "signoff": parse_signoff(log),
        **parse_targets(log),
        "log": str(case_work / "run.log"),
        "finished_at": datetime.now(timezone.utc).isoformat(),
    })
    if getattr(args, "selfcheck", False):
        record.update(run_selfcheck(case_work, reference, args))
    (case_work / "result.json").write_text(json.dumps(record, indent=2) + "\n", encoding="utf-8")
    return record


def discover(run_dir: Path, only: list[str]) -> list[dict]:
    rows = []
    for case_dir in sorted(path for path in run_dir.iterdir() if path.is_dir()):
        if only and case_dir.name not in only:
            continue
        reference_dir = case_dir / "reference"
        references = sorted(reference_dir.glob("*.sv")) if reference_dir.is_dir() else []
        if not references:
            continue
        generated = case_dir / "generation" / "generated_rtl.v"
        rows.append({
            "case": case_dir.name,
            "reference": references[0],
            "generated": generated if generated.is_file() else None,
        })
    return rows


def write_report(reports: Path, run_dir: Path, records: list[dict]) -> None:
    summary = {
        "run_dir": str(run_dir),
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "tool": "Cadence JasperGold SEC 2023.09p001",
        "cases_total": len(records),
        "status_counts": {},
        "records": records,
    }
    for record in records:
        summary["status_counts"][record["status"]] = summary["status_counts"].get(record["status"], 0) + 1
    reports.mkdir(parents=True, exist_ok=True)
    (reports / "summary.json").write_text(json.dumps(summary, indent=2) + "\n", encoding="utf-8")

    lines = [
        "# Tempo Spec -> IR -> RTL: JasperGold equivalence check",
        "",
        f"- run directory: `{run_dir}`",
        f"- tool: {summary['tool']}",
        f"- generated at: {summary['generated_at']}",
        "",
        "| case | module | raw analyze | SEC result | proven | cex | undetermined | interface | signoff |",
        "| --- | --- | --- | --- | --- | --- | --- | --- | --- |",
    ]
    for record in records:
        counts = record.get("sec_counts") or {}
        signoff = record.get("signoff") or {}
        iface = record.get("interface") or {}
        unmapped = iface.get("unmapped_spec", 0), iface.get("unmapped_imp", 0)
        iface_text = iface.get("overall", "-")
        if any(unmapped):
            iface_text += f" (unmapped {unmapped[0]}sp/{unmapped[1]}im)"
        lines.append(
            "| {case} | {module} | {raw} | {status} | {proven} | {cex} | {undet} | {iface} | {sign} |".format(
                case=record["case"],
                module=record.get("impl_top_raw", "?"),
                raw="ok" if record.get("raw_analyze_ok") else ("fail" if record.get("raw_analyze_ok") is False else "-"),
                status=record["status"],
                proven=counts.get("proven", "-"),
                cex=counts.get("cex", "-"),
                undet=counts.get("undetermined", "-"),
                iface=iface_text,
                sign=signoff.get("Overall SEC status", "-"),
            )
        )

    lines += ["", "## 失败/反例明细", ""]
    for record in records:
        if record["status"] == "equivalent":
            continue
        detail = f"- **{record['case']}** — {record['status']}: {record.get('reason', '')}"
        if record.get("defect_line"):
            detail += f"\n  - 生成 RTL 第 {record['defect_line']['line']} 行: `{record['defect_line']['text']}`"
        if record.get("cex_targets"):
            detail += f"\n  - 反例输出({len(record['cex_targets'])}): " + ", ".join(record["cex_targets"][:12])
            if len(record["cex_targets"]) > 12:
                detail += " …"
        lines.append(detail)

    controls = [record for record in records if record.get("selfcheck_status")]
    if controls:
        lines += ["", "## 方法学对照：参考 RTL 与自身比较", ""]
        for record in controls:
            lines.append(
                f"- {record['case']}: {record['selfcheck_status']}"
                f"（proven={record.get('selfcheck_counts', {}).get('proven', '-')}, "
                f"cex={record.get('selfcheck_counts', {}).get('cex', '-')}）"
            )
    (reports / "summary.md").write_text("\n".join(lines) + "\n", encoding="utf-8")


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description="JasperGold SEC over Tempo pipeline results")
    parser.add_argument("--run-dir", type=Path, required=True)
    parser.add_argument("--cases", nargs="*", default=[])
    parser.add_argument("--work-root", type=Path, default=WORK_DEFAULT)
    parser.add_argument("--report-root", type=Path, default=REPORT_DEFAULT)
    parser.add_argument("--timeout", type=int, default=1800)
    parser.add_argument("--prove-limit", default="10m")
    parser.add_argument("--skip-raw", action="store_true")
    parser.add_argument("--init-nonresettable", default="0",
                        help="initial value for non-resettable registers (default 0; pass 'none' to disable)")
    parser.add_argument("--selfcheck", action="store_true",
                        help="also compare each reference RTL against itself (methodology control)")
    parser.add_argument("--clean", action="store_true", help="delete the work root before running")
    args = parser.parse_args(argv)
    if args.init_nonresettable.strip().lower() == "none":
        args.init_nonresettable = None

    run_dir = args.run_dir.resolve()
    rows = discover(run_dir, args.cases)
    if not rows:
        parser.error(f"no usable cases found under {run_dir}")

    if args.clean and args.work_root.exists():
        shutil.rmtree(args.work_root)

    records = []
    for row in rows:
        print(f"[jg] {row['case']}: {row['reference'].name} vs generated_rtl.v", flush=True)
        try:
            record = run_case(row["case"], args.work_root / row["case"], row["reference"], row["generated"], args)
        except Exception as exc:  # keep the sweep going
            record = {"case": row["case"], "status": "driver_error",
                      "reason": f"{type(exc).__name__}: {exc}"}
            print(f"[jg] {row['case']}: driver error: {exc}", flush=True)
        records.append(record)
        print(f"[jg] {row['case']}: {record['status']}"
              + (f" (proven={record['sec_counts'].get('proven')}, cex={record['sec_counts'].get('cex')})"
                 if record.get("sec_counts") else ""), flush=True)

    write_report(args.report_root, run_dir, records)
    print(json.dumps({"status_counts": {s: sum(r["status"] == s for r in records) for s in {r["status"] for r in records}}}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())