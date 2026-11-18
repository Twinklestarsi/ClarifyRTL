#!/usr/bin/env python3
"""Generate controlled spec ambiguities for the version2 comparison experiments.

For every clean case we ask DeepSeek to edit the specification at one or more
sites.  Each site must:

* keep the original intended behavior as a valid interpretation;
* admit a plausible alternative that is observably different;
* be resolvable by one clarification question answerable from the original
  spec / reference RTL;
* leave the module name, ports, widths and parameter defaults untouched.

A second LLM pass audits the result.  The edited spec is written to
``experiments/fuzz/out/<dataset>/<short>/edited_spec.md`` together with the
site annotations.  ``build_ambiguous.py`` later assembles the dataset copies.

Usage examples::

    python3 fuzz_specs.py --dataset tempo --cases dp_ram,coalescer --workers 1
    python3 fuzz_specs.py --dataset rtllm --cases accu,fsm --dry-run
    python3 fuzz_specs.py --dataset all --workers 4
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
EXPERIMENTS = HERE.parent
VERSION2 = EXPERIMENTS.parent
WORKSPACE = VERSION2.parent
DATA = EXPERIMENTS / "data"
OUT = HERE / "out"
LOGS = HERE / "logs"

DATASETS = {
    "rtllm": DATA / "group1/rtllm",
    "verilogeval": DATA / "group1/verilogeval",
    "tempo": DATA / "group2/tempo",
}

DATASET_LABEL = {
    "rtllm": "RTLLM",
    "verilogeval": "VerilogEval",
    "tempo": "Tempo26",
}

CATEGORIES = {
    "C1": "concurrency / priority: two control conditions active in the same cycle and their relative priority is removed or weakened",
    "C2": "timing / latency: same-cycle vs next-cycle vs fixed-N-cycle visibility, or the origin of a latency count",
    "C3": "boundary: full/empty/overflow/underflow, and reads/writes that happen on the boundary cycle",
    "C4": "reset: sync/async, polarity, reset values, outputs during reset, first cycle after reset",
    "C5": "handshake: valid/ready hold and drop rules, data stability under backpressure",
    "C6": "state hold: whether counters/outputs/FSM state hold, clear or advance on pause/error/idle",
    "C7": "memory conflict: same-address read/write visibility, multi-port write conflict, merge/priority rule",
    "C8": "width / parameter / numeric: width, max value, wrap vs saturate, legal parameter range, default branch",
}

FORBIDDEN_ADDED = [
    "ambiguous", "ambiguity", "clarify", "clarification", "unspecified",
    "not specified", "tbd", "to be determined", "designer should",
    "ask the user", "intentionally", "deliberately", "hidden requirement",
    "this is unclear", "missing information", "not necessarily",
    "may or may not", "depends on interpretation", "open to interpretation",
]

_lock = threading.Lock()
LOG_PATH: Path | None = None


# --------------------------------------------------------------------------
# environment / HTTP
# --------------------------------------------------------------------------

def load_env() -> None:
    for path in (WORKSPACE / "DAG/.env", WORKSPACE / ".env"):
        if not path.is_file():
            continue
        for raw in path.read_text(encoding="utf-8", errors="replace").splitlines():
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, value = line.split("=", 1)
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key and key not in os.environ:
                os.environ[key] = value


def api_config() -> tuple[str, str, str]:
    base = os.environ.get("DAG_BASE_URL", "https://aic.tongjieda.cn/v1").rstrip("/")
    model = os.environ.get("DAG_MODEL", "deepseek-v4.1-flash-1017").strip()
    key = os.environ.get("DAG_API_KEY", "").strip()
    if not key:
        raise SystemExit("DAG_API_KEY is missing (expected in DAG/.env)")
    return base, model, key


def chat(messages: list[dict[str, str]], *, temperature: float,
         max_tokens: int = 12000) -> tuple[dict[str, Any], dict[str, Any]]:
    base, model, key = api_config()
    payload = {
        "model": model,
        "messages": messages,
        "temperature": temperature,
        "max_tokens": max_tokens,
        "response_format": {"type": "json_object"},
    }
    data = json.dumps(payload).encode("utf-8")
    last_error = None
    for attempt in range(1, 6):
        req = urllib.request.Request(
            f"{base}/chat/completions", data=data,
            headers={"Content-Type": "application/json",
                     "Authorization": f"Bearer {key}"},
            method="POST",
        )
        try:
            with urllib.request.urlopen(req, timeout=600) as resp:
                body = json.loads(resp.read().decode("utf-8"))
            content = body["choices"][0]["message"]["content"]
            finish = body["choices"][0].get("finish_reason")
            usage = body.get("usage") or {}
            if finish == "length":
                raise RuntimeError("LLM output truncated (finish_reason=length)")
            return {"content": content, "raw": body}, usage
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")[:500]
            last_error = f"HTTP {exc.code}: {detail}"
            if exc.code in (429, 500, 502, 503, 504):
                time.sleep(min(2 ** attempt, 20))
                continue
            raise RuntimeError(last_error) from exc
        except Exception as exc:  # network / timeout
            last_error = f"{type(exc).__name__}: {exc}"
            time.sleep(min(2 ** attempt, 20))
    raise RuntimeError(last_error or "LLM call failed")


def parse_json(text: str) -> dict[str, Any]:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z0-9_-]*\s*", "", text)
        text = re.sub(r"\s*```$", "", text)
    start = text.find("{")
    end = text.rfind("}")
    if start < 0 or end < start:
        raise ValueError("no JSON object in response")
    return json.loads(text[start:end + 1])


# --------------------------------------------------------------------------
# case loading
# --------------------------------------------------------------------------

def load_cases(dataset: str, names: list[str] | None = None,
               limit: int | None = None) -> list[dict[str, Any]]:
    root = DATASETS[dataset]
    manifest = json.loads((root / "manifest.json").read_text(encoding="utf-8"))
    wanted = {n.lower() for n in names} if names else None
    cases = []
    for row in manifest["cases"]:
        if wanted and row["short"].lower() not in wanted:
            continue
        meta = json.loads((root / row["meta"]).read_text(encoding="utf-8"))
        cases.append({
            "dataset": dataset,
            "label": DATASET_LABEL[dataset],
            "short": row["short"],
            "case_id": row["case_id"],
            "top_module": row["top_module"],
            "spec": (root / row["prompt"]).read_text(encoding="utf-8"),
            "reference": (root / row["reference"]).read_text(
                encoding="utf-8", errors="replace"),
            "testbench": (root / row["testbench"]).read_text(
                encoding="utf-8", errors="replace"),
            "meta": meta,
            "root": root,
        })
        if limit and len(cases) >= limit:
            break
    return cases


# --------------------------------------------------------------------------
# prompts
# --------------------------------------------------------------------------

def generator_prompt(case: dict[str, Any], target: int | str,
                     feedback: str | None = None) -> list[dict[str, str]]:
    if target == "adaptive":
        count_text = (
            "Produce between 1 and 8 sites, using the minimum number of genuinely "
            "independent ambiguities. Simple combinational modules should use 1-3; "
            "complex FSMs, pipelines or memory subsystems may use up to 8. "
            "Do not pad the list to 8."
        )
    elif target == "ve_candidates":
        count_text = (
            "Propose 3 to 6 candidate sites. The reviewer will retain the valid "
            "ones (at least 1, at most 8). If the spec genuinely supports only "
            "one genuine ambiguity, propose one; do not pad with weak sites."
        )
    else:
        count_text = (
            "Propose 10 to 12 candidate sites. The reviewer will retain EXACTLY 8 "
            "of them for the final dataset, so extra candidates are allowed. Each "
            "candidate must be independent, non-overlapping and genuinely "
            "observable; do not pad with weak or duplicate sites."
        )

    categories = "\n".join(f"{k}: {v}" for k, v in CATEGORIES.items())
    feedback_block = ""
    if feedback:
        feedback_block = (
            "\n\nPREVIOUS ATTEMPT WAS REJECTED. Fix these problems and return a "
            "complete new JSON object:\n" + feedback.strip() + "\n"
        )

    system = (
        "You are constructing a controlled ambiguity dataset for an RTL "
        "specification clarification experiment. You edit specs minimally and "
        "return strict JSON only."
    )
    user = f"""DATASET: {case['label']}
CASE: {case['short']}
TOP_MODULE: {case['top_module']}

You receive the authoritative specification, a correct reference implementation,
and the fixed testbench. Use the reference only to understand intended behavior;
do NOT copy implementation details (internal signal names, pipeline stages,
encodings) into the specification. Use the testbench to identify behaviors that
are observable.

OBJECTIVE
Edit ORIGINAL_SPEC at the requested number of sites. For every site:
1. The original intended behavior must remain a valid interpretation of the
   edited text.
2. At least one other plausible RTL-engineer interpretation must exist and must
   produce observably different behavior.
3. The ambiguity must be resolvable by one specific clarification question whose
   answer is stated or directly entailed by ORIGINAL_SPEC / REFERENCE_RTL.
4. Prefer omission or weakening of a necessary condition. Do NOT write a rule
   that directly contradicts the original.
5. Do NOT add meta-language about ambiguity, missing information, or clarification.

ALLOWED CATEGORIES (use the id exactly):
{categories}

SITE COUNT
{count_text}

GROUNDING RULES
- The intended behavior must be explicitly stated or unambiguously entailed by
  ORIGINAL_SPEC. Do not rely only on REFERENCE_RTL.
- Before using a site, verify that REFERENCE_RTL implements the intended
  behavior. If the reference and the spec disagree at that clause, do not use
  it; choose another clause.
- Prefer clauses exercised by TESTBENCH. A site whose witness is not exercised
  by the testbench is weak unless the reference clearly implements it.

EDITING RULES
- `original_text` must be an exact substring of ORIGINAL_SPEC and occur exactly
  once. Keep it short: one clause to at most three sentences.
- `replacement_text` must be minimal. It must remove or weaken the precise rule;
  the original behavior must still be allowed. An empty replacement is allowed
  only for a pure omission.
- The replacement must be shorter or equally specific. Do NOT add explanation,
  labels, or clarifying text; adding detail is the opposite of an ambiguity.
- Do NOT use hedging phrases such as "not necessarily", "may or may not",
  "unclear", or "depends on interpretation". Remove or weaken the rule directly.
- Sites must not overlap and must not target the same clause twice.
- Do NOT edit the module header, module name, port names, port widths,
  directions, parameter names/defaults, or the top module name.
- Do NOT add implementation details that are not in ORIGINAL_SPEC.
- Preserve formatting and every part of the spec not covered by an edit.
- Each site needs a concrete witness input/sequence under which the intended and
  alternative behaviors differ. Write it in `observable_difference`.
- The clarification question must ask about the SAME property that
  replacement_text removes or weakens. An edit about width must not produce a
  question about timing, and vice versa.
- Choose the category that matches the property actually weakened.
- The clarification question must be answerable from the original spec/reference;
  the answer must restore the original behavior.
- `mutation` must be one of: omission, weakening, alternative_allowed.

EXAMPLES
Example 1 (C2 timing, weakening):
  original_text: "observable exactly MUL_LATENCY rising edges after the acceptance edge"
  replacement_text: "observable after the configured MUL_LATENCY cycles"
  intended_behavior: "The result is visible exactly MUL_LATENCY rising edges after acceptance."
  alternative_behavior: "The result is visible one edge earlier because acceptance is counted as cycle 1."
  clarification_question: "With MUL_LATENCY=3, is the result visible exactly 3 rising edges after acceptance?"
  clarification_answer: "Yes. The acceptance edge is cycle 0; the result is visible on the third rising edge after it."
  observable_difference: "The first cycle in which out_valid is high differs by one."

Example 2 (C7 memory conflict, omission):
  original_text: "In mode \\"N\\", return hold_data, the result of the most recent prior sampled read."
  replacement_text: ""
  intended_behavior: "Mode N returns the most recently sampled read data."
  alternative_behavior: "Mode N returns the current memory word or undefined data."
  clarification_question: "In RDW_MODE=N, what data is returned on a same-address read/write?"
  clarification_answer: "hold_data, the most recent prior sampled read."
  observable_difference: "Read data differs after a prior read to another address."

ORIGINAL_SPEC:
<<<SPEC
{case['spec']}
SPEC>>>

REFERENCE_RTL:
<<<RTL
{case['reference'][:40000]}
RTL>>>

TESTBENCH:
<<<TB
{case['testbench'][:40000]}
TB>>>
{feedback_block}
Return strict JSON only, with this shape:
{{
  "sites": [
    {{
      "id": "S1",
      "category": "C2",
      "original_text": "...",
      "replacement_text": "...",
      "mutation": "weakening",
      "intended_behavior": "...",
      "alternative_behavior": "...",
      "clarification_question": "...",
      "clarification_answer": "...",
      "observable_difference": "...",
      "confidence": 0.0
    }}
  ]
}}"""
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


def auditor_prompt(case: dict[str, Any], sites: list[dict[str, Any]],
                   edited: str) -> list[dict[str, str]]:
    system = (
        "You are a strict reviewer for a controlled RTL ambiguity dataset. "
        "Reject sites that are not genuine, not observable, not answerable, or "
        "that change the interface. Return strict JSON only."
    )
    user = f"""Review the proposed ambiguity sites for this case.

ORIGINAL_SPEC:
<<<SPEC
{case['spec']}
SPEC>>>

EDITED_SPEC:
<<<EDITED
{edited}
EDITED>>>

PROPOSED_SITES:
<<<SITES
{json.dumps(sites, ensure_ascii=False, indent=2)}
SITES>>>

REFERENCE_RTL:
<<<RTL
{case['reference'][:40000]}
RTL>>>

TESTBENCH:
<<<TB
{case['testbench'][:40000]}
TB>>>

For every site check:
A. `original_text` occurs exactly once in ORIGINAL_SPEC; `replacement_text` is
   present in EDITED_SPEC (or is empty for a pure omission).
B. EDITED_SPEC still allows the original intended behavior. There is no
   contradiction with the original intent.
C. No residual clause in EDITED_SPEC uniquely determines the behavior; the
   alternative interpretation is genuinely possible.
D. The alternative is plausible for a competent RTL engineer and observably
   different on the reference RTL / testbench. The witness in
   `observable_difference` is concrete enough to distinguish them.
E. The clarification question is specific and answerable from ORIGINAL_SPEC or
   REFERENCE_RTL; the answer restores the original behavior.
F. The site does not change the module name, port names/widths/directions,
   parameter names/defaults, or the module header.
G. Sites are independent, non-overlapping, and do not target the same clause.
H. If ORIGINAL_SPEC and REFERENCE_RTL disagree at this clause, reject the site
   and state "reference/spec conflict". Do not build a site on behavior that
   only one of them supports.
I. The replacement must be shorter or equally specific. It must not add new
   explanation, labels, or clarification. If it adds detail, reject it.
J. The alternative must be one a competent RTL engineer would seriously
   consider. A purely theoretical misreading of an already explicit label or
   formula is not enough.
K. The clarification question targets the same property that replacement_text
   removes or weakens. If it asks about a different property (for example, the
   edit removes width information but the question asks about latency), reject.
L. The site's category matches the property actually weakened.

Global checks:
- Site count is exactly the requested count (8 for Tempo; 1-8 for the other
  datasets, with no padding).
- EDITED_SPEC contains no meta-language about ambiguity or missing information.
- No site injects a directly false rule; all mutations are omission/weakening/
  alternative_allowed.
- EDITED_SPEC is not more detailed than ORIGINAL_SPEC and does not copy
  implementation internals from the reference.

If a site is weak but repairable, say exactly what to change. If the whole set
is bad, explain why.

Return strict JSON only:
{{
  "pass": true,
  "site_results": [
    {{"id": "S1", "ok": true, "issues": []}}
  ],
  "global_issues": [],
  "fix_instructions": ""
}}"""
    return [{"role": "system", "content": system},
            {"role": "user", "content": user}]


# --------------------------------------------------------------------------
# validation
# --------------------------------------------------------------------------

def interface_tokens(spec: str, top: str) -> list[str]:
    tokens: list[str] = [top]
    header = re.search(r"module\s+" + re.escape(top) + r"\s*\(.*?\);",
                       spec, re.S)
    if header:
        tokens.append(header.group(0))
    section = False
    for line in spec.splitlines():
        stripped = line.strip()
        if re.match(r"^(Input ports|Output ports|Parameter)\s*:", stripped,
                    re.I):
            section = True
            continue
        if re.match(r"^(Implementation|Function|Behavior|Description)\s*:",
                    stripped, re.I):
            section = False
            continue
        if section and ":" in line:
            prefix = line.split(":", 1)[0].strip()
            if prefix and len(prefix) < 100:
                tokens.append(prefix)
    for match in re.finditer(r"^\s*([A-Za-z_]\w*)\s*=", spec, re.M):
        tokens.append(match.group(1))
    # keep order, drop duplicates
    seen = set()
    result = []
    for token in tokens:
        if token and token not in seen:
            seen.add(token)
            result.append(token)
    return result


def find_span(text: str, snippet: str) -> tuple[int, int] | None:
    """Find a unique occurrence, tolerating whitespace differences."""
    if not snippet:
        return None
    if text.count(snippet) == 1:
        start = text.index(snippet)
        return start, start + len(snippet)
    parts = snippet.split()
    if len(parts) < 2:
        return None
    pattern = r"\s+".join(re.escape(part) for part in parts)
    matches = list(re.finditer(pattern, text))
    if len(matches) == 1:
        return matches[0].span()
    return None


def validate_sites(original: str, sites: list[dict[str, Any]], top: str,
                   target: int | str) -> tuple[str, list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(sites, list) or not sites:
        return "", ["no sites returned"], warnings
    if target == "candidates":
        if not (8 <= len(sites) <= 12):
            errors.append(f"expected 8-12 candidate sites, got {len(sites)}")
    elif target == "ve_candidates":
        if not (1 <= len(sites) <= 6):
            errors.append(f"expected 1-6 candidate sites, got {len(sites)}")
    elif target != "adaptive" and len(sites) != target:
        errors.append(f"expected {target} sites, got {len(sites)}")
    if target == "adaptive" and not (1 <= len(sites) <= 8):
        errors.append(f"expected 1-8 sites, got {len(sites)}")

    spans = []
    cursor = original
    for index, site in enumerate(sites):
        sid = site.get("id") or f"S{index + 1}"
        for field in ("category", "original_text", "replacement_text",
                      "mutation", "intended_behavior", "alternative_behavior",
                      "clarification_question", "clarification_answer",
                      "observable_difference"):
            if field not in site:
                errors.append(f"{sid}: missing field {field}")
        if site.get("category") not in CATEGORIES:
            errors.append(f"{sid}: invalid category {site.get('category')!r}")
        if site.get("mutation") not in ("omission", "weakening",
                                        "alternative_allowed"):
            errors.append(f"{sid}: invalid mutation {site.get('mutation')!r}")
        original_text = site.get("original_text") or ""
        replacement = site.get("replacement_text")
        if replacement is None:
            errors.append(f"{sid}: replacement_text is null")
            continue
        if not original_text:
            errors.append(f"{sid}: empty original_text")
            continue
        if original_text == replacement:
            errors.append(f"{sid}: replacement is identical to original")
        span = find_span(original, original_text)
        if span is None:
            errors.append(
                f"{sid}: original_text occurs {original.count(original_text)} "
                f"times and has no unique whitespace-tolerant match")
            continue
        start, end = span
        original_text = original[start:end]
        site["original_text"] = original_text
        spans.append((start, end, sid))
        if original_text in replacement:
            errors.append(f"{sid}: replacement still contains the removed rule")
        if len(replacement) > max(len(original_text) + 40,
                                  int(len(original_text) * 1.25)):
            errors.append(
                f"{sid}: replacement adds too much text; the edit must remove "
                f"or weaken a rule, not add explanation/labels")
        if not replacement and site.get("mutation") != "omission":
            errors.append(f"{sid}: empty replacement requires mutation=omission")
        # apply
        cursor_span = find_span(cursor, original_text)
        if cursor_span is None:
            errors.append(f"{sid}: overlapping edit (not applicable in sequence)")
            continue
        cursor = (cursor[:cursor_span[0]] + replacement
                  + cursor[cursor_span[1]:])

    spans.sort()
    for left, right in zip(spans, spans[1:]):
        if right[0] < left[1]:
            errors.append(f"overlapping sites: {left[2]} and {right[2]}")

    if errors:
        return cursor, errors, warnings

    ratio = len(cursor) / max(len(original), 1)
    if not (0.5 <= ratio <= 1.5):
        errors.append(f"edited/original length ratio out of range: {ratio:.2f}")
    if top not in cursor:
        errors.append("top module name disappeared from edited spec")
    for token in interface_tokens(original, top):
        if token not in cursor:
            errors.append(f"interface token disappeared: {token!r}")
    lowered_original = original.lower()
    lowered_edited = cursor.lower()
    for phrase in FORBIDDEN_ADDED:
        if phrase in lowered_edited and phrase not in lowered_original:
            errors.append(f"meta-language added: {phrase!r}")
    if len(cursor.strip()) < 0.5 * len(original.strip()):
        errors.append("edited spec is too short")
    for site in sites:
        if float(site.get("confidence", 1.0)) < 0.5:
            warnings.append(f"{site.get('id')}: low confidence")
    return cursor, errors, warnings


# --------------------------------------------------------------------------
# per-case pipeline
# --------------------------------------------------------------------------

def save_json(path: Path, payload: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix(path.suffix + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n",
                   encoding="utf-8")
    tmp.replace(path)


def log_event(event: dict[str, Any]) -> None:
    if LOG_PATH is None:
        return
    with _lock:
        with LOG_PATH.open("a", encoding="utf-8") as fh:
            fh.write(json.dumps(event, ensure_ascii=False) + "\n")


def select_sites(sites: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """Keep the n highest-confidence sites, preserving the original order."""
    ranked = sorted(enumerate(sites),
                    key=lambda iv: (-float(iv[1].get("confidence", 0.5) or 0),
                                    iv[0]))
    chosen = {index for index, _ in ranked[:n]}
    return [site for index, site in enumerate(sites) if index in chosen]


def target_for(case: dict[str, Any]) -> int | str:
    if case["dataset"] == "tempo":
        return 8
    if case["dataset"] == "verilogeval":
        return "ve_candidates"
    return "adaptive"


def process_case(case: dict[str, Any], *, attempts: int, target: int | str,
                 force: bool, save_prompts: bool) -> dict[str, Any]:
    out_dir = OUT / case["dataset"] / case["short"]
    result_path = out_dir / "result.json"
    if result_path.is_file() and not force:
        previous = json.loads(result_path.read_text(encoding="utf-8"))
        if previous.get("status") == "passed":
            return {"case": case["short"], "dataset": case["dataset"],
                    "status": "cached", "sites": len(previous.get("sites", []))}

    out_dir.mkdir(parents=True, exist_ok=True)
    started = time.time()
    feedback = ""
    all_errors: list[str] = []
    total_usage = {"prompt_tokens": 0, "completion_tokens": 0,
                   "total_tokens": 0}
    final: dict[str, Any] | None = None

    for attempt in range(1, attempts + 1):
        try:
            gen_messages = generator_prompt(case, target, feedback or None)
            if save_prompts:
                (out_dir / f"attempt_{attempt}_generate_prompt.txt").write_text(
                    "\n\n".join(m["content"] for m in gen_messages),
                    encoding="utf-8")
            gen_response, gen_usage = chat(gen_messages, temperature=0.5)
            for key in total_usage:
                total_usage[key] += int(gen_usage.get(key, 0) or 0)
            payload = parse_json(gen_response["content"])
            sites = payload.get("sites") or []
            validation_target = "candidates" if target == 8 else target
            edited, errors, warnings = validate_sites(
                case["spec"], sites, case["top_module"], validation_target)
            save_json(out_dir / f"attempt_{attempt}_generate.json", {
                "attempt": attempt,
                "usage": gen_usage,
                "sites": sites,
                "validation_errors": errors,
                "validation_warnings": warnings,
                "raw_content": gen_response["content"],
            })
            log_event({"ts": datetime.now(timezone.utc).isoformat(),
                       "dataset": case["dataset"], "case": case["short"],
                       "attempt": attempt, "stage": "generate",
                       "status": "ok" if not errors else "invalid",
                       "errors": errors, "usage": gen_usage})
            if errors:
                all_errors = errors
                feedback = "Validation errors:\n" + "\n".join(f"- {e}" for e in errors)
                continue

            audit_messages = auditor_prompt(case, sites, edited)
            audit_response, audit_usage = chat(audit_messages, temperature=0.0)
            for key in total_usage:
                total_usage[key] += int(audit_usage.get(key, 0) or 0)
            audit = parse_json(audit_response["content"])
            save_json(out_dir / f"attempt_{attempt}_audit.json", {
                "attempt": attempt,
                "usage": audit_usage,
                "audit": audit,
                "raw_content": audit_response["content"],
            })
            log_event({"ts": datetime.now(timezone.utc).isoformat(),
                       "dataset": case["dataset"], "case": case["short"],
                       "attempt": attempt, "stage": "audit",
                       "status": "pass" if audit.get("pass") else "fail",
                       "usage": audit_usage})

            site_results = audit.get("site_results") or []
            bad_sites = [r for r in site_results if not r.get("ok", False)]
            if len(site_results) != len(sites):
                bad_sites.append({"id": "ALL",
                                  "issues": [f"audit returned {len(site_results)} "
                                             f"site results for {len(sites)} sites"]})
            # Tempo: the model proposes 8-12 candidates; keep the 8 best
            # audited sites and rebuild the edited spec from only those.
            if target == 8:
                ok_ids = {r.get("id") for r in site_results if r.get("ok")}
                ok_sites = [s for s in sites if s.get("id") in ok_ids]
                if len(ok_sites) >= 8:
                    selected = select_sites(ok_sites, 8)
                    sel_edited, sel_errors, sel_warnings = validate_sites(
                        case["spec"], selected, case["top_module"], 8)
                    if not sel_errors:
                        final = {
                            "status": "passed",
                            "dataset": case["dataset"],
                            "label": case["label"],
                            "case_id": case["case_id"],
                            "short": case["short"],
                            "top_module": case["top_module"],
                            "target_sites": 8,
                            "candidate_sites": len(sites),
                            "attempts": attempt,
                            "sites": selected,
                            "edited_spec": sel_edited,
                            "audit": audit,
                            "validation_warnings": warnings + sel_warnings,
                            "usage": total_usage,
                            "elapsed_s": round(time.time() - started, 2),
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                        }
                        break

            if target != 8 and audit.get("pass") is True and not bad_sites:
                final = {
                    "status": "passed",
                    "dataset": case["dataset"],
                    "label": case["label"],
                    "case_id": case["case_id"],
                    "short": case["short"],
                    "top_module": case["top_module"],
                    "target_sites": target,
                    "attempts": attempt,
                    "sites": sites,
                    "edited_spec": edited,
                    "audit": audit,
                    "validation_warnings": warnings,
                    "usage": total_usage,
                    "elapsed_s": round(time.time() - started, 2),
                    "generated_at": datetime.now(timezone.utc).isoformat(),
                }
                break

            # For adaptive cases, keep sites the auditor accepted and rebuild
            # the edited spec from just those sites.
            if target in ("adaptive", "ve_candidates") and len(site_results) == len(sites):
                ok_ids = {r.get("id") for r in site_results if r.get("ok")}
                kept = [s for s in sites if s.get("id") in ok_ids]
                dropped = [s for s in sites if s.get("id") not in ok_ids]
                if kept and dropped:
                    kept_edited, kept_errors, kept_warnings = validate_sites(
                        case["spec"], kept, case["top_module"],
                        ("ve_candidates" if target == "ve_candidates" else "adaptive"))
                    if not kept_errors:
                        final = {
                            "status": "passed",
                            "dataset": case["dataset"],
                            "label": case["label"],
                            "case_id": case["case_id"],
                            "short": case["short"],
                            "top_module": case["top_module"],
                            "target_sites": target,
                            "attempts": attempt,
                            "sites": kept,
                            "edited_spec": kept_edited,
                            "audit": audit,
                            "salvaged": True,
                            "dropped_sites": [
                                {"id": s.get("id"),
                                 "issues": next(
                                     (r.get("issues") for r in site_results
                                      if r.get("id") == s.get("id")), [])}
                                for s in dropped
                            ],
                            "validation_warnings": warnings + kept_warnings,
                            "usage": total_usage,
                            "elapsed_s": round(time.time() - started, 2),
                            "generated_at": datetime.now(timezone.utc).isoformat(),
                        }
                        break

            reasons = []
            if target == 8:
                passed_n = len([r for r in site_results if r.get("ok")])
                if passed_n < 8:
                    reasons.append(f"only {passed_n} audited sites passed; "
                                   f"need at least 8")
            if not audit.get("pass"):
                reasons.append("audit did not pass")
            for row in bad_sites:
                reasons.append(f"{row.get('id')}: " + "; ".join(
                    str(x) for x in (row.get("issues") or [])))
            if audit.get("global_issues"):
                reasons.append("global: " + "; ".join(
                    str(x) for x in audit["global_issues"]))
            if audit.get("fix_instructions"):
                reasons.append("fix: " + str(audit["fix_instructions"]))
            all_errors = reasons
            feedback = "\n".join(f"- {r}" for r in reasons)
        except Exception as exc:  # noqa: BLE001
            all_errors = [f"{type(exc).__name__}: {exc}"]
            feedback = "Previous attempt raised an error:\n- " + all_errors[0]
            log_event({"ts": datetime.now(timezone.utc).isoformat(),
                       "dataset": case["dataset"], "case": case["short"],
                       "attempt": attempt, "stage": "exception",
                       "status": "error", "errors": all_errors})

    if final is None:
        final = {
            "status": "failed",
            "dataset": case["dataset"],
            "case_id": case["case_id"],
            "short": case["short"],
            "top_module": case["top_module"],
            "target_sites": target,
            "attempts": attempts,
            "errors": all_errors,
            "usage": total_usage,
            "elapsed_s": round(time.time() - started, 2),
            "generated_at": datetime.now(timezone.utc).isoformat(),
        }
    save_json(result_path, final)
    (out_dir / "edited_spec.md").write_text(
        final.get("edited_spec", ""), encoding="utf-8")
    return {"case": case["short"], "dataset": case["dataset"],
            "status": final["status"],
            "sites": len(final.get("sites", []))}


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dataset", choices=["rtllm", "verilogeval", "tempo",
                                              "all"], default="all")
    parser.add_argument("--cases", default=None,
                        help="comma-separated short names")
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument("--workers", type=int, default=2)
    parser.add_argument("--attempts", type=int, default=3)
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--save-prompts", action="store_true")
    args = parser.parse_args()

    load_env()
    names = [x.strip() for x in args.cases.split(",") if x.strip()] \
        if args.cases else None
    datasets = list(DATASETS) if args.dataset == "all" else [args.dataset]
    cases: list[dict[str, Any]] = []
    for dataset in datasets:
        cases.extend(load_cases(dataset, names, args.limit))

    if not cases:
        raise SystemExit("no cases selected")

    if args.dry_run:
        case = cases[0]
        target = target_for(case)
        messages = generator_prompt(case, target)
        print("\n\n".join(m["content"] for m in messages))
        return

    LOGS.mkdir(parents=True, exist_ok=True)
    global LOG_PATH
    LOG_PATH = LOGS / f"run_{datetime.now().strftime('%Y%m%dT%H%M%S')}.jsonl"
    print(f"cases={len(cases)} workers={args.workers} log={LOG_PATH}")

    stats: dict[str, int] = {}
    with ThreadPoolExecutor(max_workers=args.workers) as pool:
        futures = {
            pool.submit(process_case, case, attempts=args.attempts,
                        target=target_for(case),
                        force=args.force, save_prompts=args.save_prompts): case
            for case in cases
        }
        for future in as_completed(futures):
            case = futures[future]
            try:
                result = future.result()
            except Exception as exc:  # noqa: BLE001
                result = {"dataset": case["dataset"], "case": case["short"],
                          "status": "error", "error": f"{type(exc).__name__}: {exc}"}
            key = f"{result['dataset']}/{result['case']}"
            stats[result["status"]] = stats.get(result["status"], 0) + 1
            print(f"{key:45s} {result['status']:8s} "
                  f"sites={result.get('sites', 0)}", flush=True)

    print(json.dumps(stats, ensure_ascii=False))


if __name__ == "__main__":
    main()