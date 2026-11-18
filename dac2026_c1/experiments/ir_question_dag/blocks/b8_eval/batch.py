"""Batch execution with a formal-run semaphore and a compact results table."""

from __future__ import annotations

import json
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any

_SUMMARY_LOCK = threading.Lock()


def _load_previous(output_root: Path) -> dict[str, dict[str, Any]]:
    """Latest result.json per case, so a rerun can skip finished cases."""
    latest: dict[str, dict[str, Any]] = {}
    for result in sorted(output_root.glob("*/2*/result.json")):
        try:
            data = json.loads(result.read_text())
        except (OSError, json.JSONDecodeError):
            continue
        key = data.get("case")
        if key and (key not in latest or result.stat().st_mtime > latest[key]["_mtime"]):
            data["_mtime"] = result.stat().st_mtime
            latest[key] = data
    return latest


def run_batch(config: dict, env: dict, *, arm: str, runner=None,
              dataset: str | None = None, workers: int = 1,
              only: list[str] | None = None, skip_existing: bool = False,
              limit: int = 0) -> int:
    from blocks.b1_scaffold import load_cases
    from blocks.b1_scaffold.config import get

    if runner is None:
        raise ValueError("run_batch requires a runner callable")
    output_root = Path(config["run"]["output_root"])
    cases = load_cases(config["run"]["case_root"], dataset=dataset, names=only)
    if limit:
        cases = cases[:limit]
    previous = _load_previous(output_root) if skip_existing else {}
    from blocks.b7_phase_c.equivalence import set_formal_parallelism

    set_formal_parallelism(int(get(config, "formal.max_parallel", 1)))
    cache: dict[str, str] = {}
    config = dict(config, _formal_cache=cache)

    attempts = int(get(config, "run.case_attempts", 1) or 1)

    def task(case):
        if case.key in previous:
            return case.key, previous[case.key]
        best = None
        for attempt in range(attempts):
            run_id = None if attempt == 0 else time.strftime("%Y%m%dT%H%M%S") + f"-try{attempt + 1}"
            summary = runner(case, config, env, arm=arm, run_id=run_id)
            summary["attempts_used"] = attempt + 1
            best = summary
            if str(summary.get("status", "")).startswith("equivalent"):
                break
        return case.key, best

    started = time.time()
    rows: list[dict[str, Any]] = []
    with ThreadPoolExecutor(max_workers=max(1, workers)) as pool:
        futures = {pool.submit(task, case): case for case in cases}
        for future in as_completed(futures):
            case = futures[future]
            try:
                key, summary = future.result()
            except Exception as exc:  # noqa: BLE001
                key, summary = case.key, {"case": case.key, "status": "crashed",
                                          "error": f"{type(exc).__name__}: {exc}"}
            formal = (summary.get("formal") or {})
            row = {"case": key, "status": summary.get("status"),
                   "attempts": summary.get("attempts_used"),
                   "formal": formal.get("status"), "duration_s": summary.get("duration_s"),
                   "rtl_lines": summary.get("rtl_lines"), "llm_usage": summary.get("llm_usage"),
                   "run_dir": summary.get("run_dir")}
            rows.append(row)
            with _SUMMARY_LOCK:
                print(f"[{len(rows):2d}/{len(cases)}] {key:34s} "
                      f"{str(row['status'])[:34]:34s} {row['formal']}", flush=True)
    rows.sort(key=lambda row: row["case"])
    elapsed = round(time.time() - started, 1)
    passed = sum(1 for row in rows if str(row["status"]).startswith("equivalent"))
    bounded = sum(1 for row in rows if row["status"] == "equivalent_bounded")
    payload = {"arm": arm, "cases": len(rows), "equivalent": passed,
               "bounded_equivalent": bounded,
               "rate": round(passed / len(rows), 4) if rows else 0.0,
               "elapsed_s": elapsed, "rows": rows}
    batch_dir = output_root / "_batch"
    batch_dir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%dT%H%M%S")
    (batch_dir / f"{arm}_{stamp}.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n{arm}: {passed}/{len(rows)} equivalent ({payload['rate']:.1%}) "
          f"in {elapsed}s -> {batch_dir}/{arm}_{stamp}.json")
    return 0