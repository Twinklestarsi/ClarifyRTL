#!/usr/bin/env python3
"""Build long/paired/summary tables from the running matrices."""
from __future__ import annotations
import csv, json, os, re
from collections import defaultdict
from pathlib import Path

HERE = Path(__file__).resolve().parent
RUNS = HERE / "runs"
TAG = "run1"
OUT = RUNS / "analysis"
OUT.mkdir(parents=True, exist_ok=True)

FAMILY = {"RTLLM": "rtllm", "RTLLM_AMBIG": "rtllm",
          "VERILOGEVAL": "verilogeval", "VERILOGEVAL_AMBIG": "verilogeval",
          "TEMPO": "tempo", "TEMPO_AMBIG": "tempo"}
CLEAN = {"RTLLM", "VERILOGEVAL", "TEMPO"}


def tokens(usage) -> int:
    if not isinstance(usage, dict):
        return 0
    if usage.get("total_tokens") is not None:
        return int(usage.get("total_tokens") or 0)
    return (int(usage.get("input_tokens_estimate", 0) or 0)
            + int(usage.get("output_tokens_estimate", 0) or 0))


def tool_rows():
    rows = []
    base = RUNS / "tools_matrix" / TAG
    for path in base.glob("*/*/*/r*/*/result.json"):
        parts = path.parts
        try:
            tool, model, label = parts[-6], parts[-5], parts[-4]
            repeat = int(parts[-3][1:])
            short = path.parent.name
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        usage = data.get("llm_usage") or {}
        if not usage:
            lp = (data.get("autochip") or {}).get("log")
            if lp and Path(lp).is_file():
                usage = {"total_tokens": max(1, len(Path(lp).read_text(
                    encoding="utf-8", errors="replace")) // 4)}
        rows.append({
            "system": tool, "model": model, "label": label,
            "family": FAMILY.get(label, label.lower()), "short": short,
            "repeat": repeat, "success": int(data.get("status") == "passed"),
            "status": data.get("status"), "tokens": tokens(usage),
            "questions": 0, "elapsed_s": data.get("elapsed_s"),
            "path": str(path), "mtime": path.stat().st_mtime,
        })
    return rows


def ours_rows():
    rows = []
    base = RUNS / "ours_matrix" / TAG
    for path in base.glob("*/*/*/r*/*/*/result.json"):
        parts = path.parts
        if parts[-2] == "formal":
            continue
        try:
            system, model = parts[-7], parts[-6]
            repeat = int(parts[-4][1:])
            label, short = path.parents[1].name.split("__", 1)
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception:
            continue
        # RTLLM was rerun with the local testbench after the .v resolution
        # fix; ignore stale JasperGold/transport results until replaced.
        if label.startswith("RTLLM"):
            method = (data.get("formal") or {}).get("method")
            if method == "jaspergold":
                continue
        dialogue = data.get("dialogue") or {}
        tok = tokens(data.get("llm_usage"))
        if not tok:
            # Newer runs do not embed llm_usage in result.json; sum the
            # per-call log instead so token accounting stays complete.
            lp = path.parent / "llm_calls.jsonl"
            if lp.is_file():
                for line in lp.read_text(encoding="utf-8", errors="replace").splitlines():
                    try:
                        call = json.loads(line)
                    except Exception:
                        continue
                    tok += (int(call.get("prompt_tokens", 0) or 0)
                            + int(call.get("completion_tokens", 0) or 0))
        rows.append({
            "system": system, "model": model, "label": label,
            "family": FAMILY.get(label, label.lower()), "short": short,
            "repeat": repeat,
            "success": int(str(data.get("status", "")).startswith("equivalent")),
            "status": data.get("status"), "tokens": tok,
            "questions": int(dialogue.get("asked", 0) or 0),
            "elapsed_s": data.get("duration_s"), "path": str(path),
            "mtime": path.stat().st_mtime,
        })
    return rows


def latest(rows):
    best = {}
    for row in rows:
        key = (row["system"], row["model"], row["label"], row["short"],
               row["repeat"])
        if key not in best or row["mtime"] > best[key]["mtime"]:
            best[key] = row
    return list(best.values())


def main():
    max_repeat = int(os.environ.get("MAX_REPEAT", "99"))
    rows = [r for r in latest(tool_rows() + ours_rows())
            if int(r.get("repeat", 0)) <= max_repeat]
    rows.sort(key=lambda r: (r["system"], r["model"], r["label"],
                             r["repeat"], r["short"]))
    fields = ["system", "model", "family", "label", "short", "repeat",
              "success", "status", "tokens", "questions", "elapsed_s", "path"]
    with (OUT / "results_long.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=fields)
        w.writeheader()
        w.writerows({k: r[k] for k in fields} for r in rows)

    grouped = defaultdict(lambda: {"n": 0, "pass": 0, "tok": 0, "q": 0})
    for r in rows:
        key = (r["system"], r["model"], r["label"])
        g = grouped[key]
        g["n"] += 1; g["pass"] += r["success"]
        g["tok"] += r["tokens"]; g["q"] += r["questions"]
    with (OUT / "summary.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["system", "model", "label", "pass", "total", "rate",
                    "avg_tokens", "avg_questions"])
        for (s, m, l), g in sorted(grouped.items()):
            w.writerow([s, m, l, g["pass"], g["n"],
                        round(100 * g["pass"] / max(g["n"], 1), 2),
                        round(g["tok"] / max(g["n"], 1), 1),
                        round(g["q"] / max(g["n"], 1), 2)])

    # paired clean/ambig by system/model/family/repeat/short
    by_case = defaultdict(dict)
    for r in rows:
        side = "clean" if r["label"] in CLEAN else "ambig"
        by_case[(r["system"], r["model"], r["family"], r["repeat"],
                 r["short"])][side] = r
    pair_rows = []
    for key, sides in by_case.items():
        if "clean" not in sides or "ambig" not in sides:
            continue
        c, a = sides["clean"], sides["ambig"]
        pair_rows.append({
            "system": key[0], "model": key[1], "family": key[2],
            "repeat": key[3], "short": key[4],
            "clean": c["success"], "ambig": a["success"],
            "delta": a["success"] - c["success"],
            "clean_tokens": c["tokens"], "ambig_tokens": a["tokens"],
        })
    with (OUT / "paired_cases.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=list(pair_rows[0]) if pair_rows else [])
        if pair_rows:
            w.writeheader(); w.writerows(pair_rows)

    pair_sum = defaultdict(lambda: {"n": 0, "clean": 0, "ambig": 0,
                                    "ct": 0, "at": 0})
    for r in pair_rows:
        g = pair_sum[(r["system"], r["model"], r["family"], r["repeat"])]
        g["n"] += 1; g["clean"] += r["clean"]; g["ambig"] += r["ambig"]
        g["ct"] += r["clean_tokens"]; g["at"] += r["ambig_tokens"]
    with (OUT / "paired_summary.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["system", "model", "family", "repeat", "pairs",
                    "clean_pass", "ambig_pass", "clean_rate", "ambig_rate",
                    "delta", "clean_tokens", "ambig_tokens"])
        for (s, m, f, rep), g in sorted(pair_sum.items()):
            w.writerow([s, m, f, rep, g["n"], g["clean"], g["ambig"],
                        round(100 * g["clean"] / max(g["n"], 1), 2),
                        round(100 * g["ambig"] / max(g["n"], 1), 2),
                        g["ambig"] - g["clean"], g["ct"], g["at"]])

    # per-repeat rates and pass@any over repeats
    by_rep = defaultdict(lambda: {"n": 0, "p": 0})
    by_case_rep = defaultdict(lambda: {"n": 0, "p": 0})
    for r in rows:
        k = (r["system"], r["model"], r["label"], r["repeat"])
        by_rep[k]["n"] += 1; by_rep[k]["p"] += r["success"]
        kc = (r["system"], r["model"], r["label"], r["short"])
        by_case_rep[kc]["n"] += 1; by_case_rep[kc]["p"] += r["success"]
    with (OUT / "summary_by_repeat.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["system", "model", "label", "repeat", "pass", "total", "rate"])
        for (s, m, l, rep), g in sorted(by_rep.items()):
            w.writerow([s, m, l, rep, g["p"], g["n"],
                        round(100 * g["p"] / max(g["n"], 1), 2)])

    rep_stats = defaultdict(lambda: {"rates": [], "any": 0, "cases": 0})
    for (s, m, l, rep), g in by_rep.items():
        rep_stats[(s, m, l)]["rates"].append(100 * g["p"] / max(g["n"], 1))
    for (s, m, l, short), g in by_case_rep.items():
        rep_stats[(s, m, l)]["cases"] += 1
        rep_stats[(s, m, l)]["any"] += int(g["p"] > 0)
    with (OUT / "repeat_stats.csv").open("w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["system", "model", "label", "repeats_done", "mean_rate",
                    "std_rate", "min_rate", "max_rate", "pass_any", "cases",
                    "pass_any_rate"])
        for (s, m, l), g in sorted(rep_stats.items()):
            rates = g["rates"]
            mean = sum(rates) / len(rates)
            var = sum((x - mean) ** 2 for x in rates) / len(rates)
            w.writerow([s, m, l, len(rates), round(mean, 2),
                        round(var ** 0.5, 2), round(min(rates), 2),
                        round(max(rates), 2), g["any"], g["cases"],
                        round(100 * g["any"] / max(g["cases"], 1), 2)])

    lines = ["# Results analysis", "",
             f"Rows: {len(rows)}; paired cases: {len(pair_rows)}", "",
             "## Paired clean vs ambiguous", "",
             "| system | model | family | pairs | clean | ambig | delta |",
             "|---|---|---|---:|---:|---:|---:|"]
    agg = defaultdict(lambda: {"n": 0, "c": 0, "a": 0})
    for r in pair_rows:
        g = agg[(r["system"], r["model"], r["family"])]
        g["n"] += 1; g["c"] += r["clean"]; g["a"] += r["ambig"]
    for (s, m, f), g in sorted(agg.items()):
        lines.append(f"| {s} | {m} | {f} | {g['n']} | "
                     f"{100*g['c']/max(g['n'],1):.1f}% | "
                     f"{100*g['a']/max(g['n'],1):.1f}% | "
                     f"{100*(g['a']-g['c'])/max(g['n'],1):+.1f}pp |")
    lines += ["", "## All groups", "",
              "| system | model | label | pass | total | rate | avg Q | avg tokens |",
              "|---|---|---|---:|---:|---:|---:|---:|"]
    for (s, m, l), g in sorted(grouped.items()):
        lines.append(f"| {s} | {m} | {l} | {g['pass']} | {g['n']} | "
                     f"{100*g['pass']/max(g['n'],1):.1f}% | "
                     f"{g['q']/max(g['n'],1):.2f} | "
                     f"{g['tok']/max(g['n'],1):.0f} |")
    lines += ["", "## Repeat statistics", "",
              "| system | model | label | repeats | mean | std | min | max | pass@any |",
              "|---|---|---|---:|---:|---:|---:|---:|---:|"]
    for (s, m, l), g in sorted(rep_stats.items()):
        rates = g["rates"]
        mean = sum(rates) / len(rates)
        var = sum((x - mean) ** 2 for x in rates) / len(rates)
        lines.append(f"| {s} | {m} | {l} | {len(rates)} | {mean:.1f}% | "
                     f"{var**0.5:.1f} | {min(rates):.1f}% | {max(rates):.1f}% | "
                     f"{100*g['any']/max(g['cases'],1):.1f}% |")
    (OUT / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"rows={len(rows)} paired={len(pair_rows)} -> {OUT}")


if __name__ == "__main__":
    main()
