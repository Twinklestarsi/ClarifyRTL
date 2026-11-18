"""LLM-assisted HW-IR repair using complete RTL/SVA simulation context."""

import copy
import json
import os
import shutil
from pathlib import Path

import yaml

from IRgeneration.checker1 import Checker1Error, check_ir as check_ir_format
from IRgeneration.checker2 import Checker2Error, check_ir as check_ir_semantics
from IRgeneration.ir_generate.gen_petrinet_ir import OpenAI, strip_fences
from IRgeneration import llm_stream


REPAIR_SYSTEM_PROMPT = """You are a hardware IR repair engineer.

Repair the current Hardware-aware Petri Net IR with a minimal structural patch
so that the generated implementation artifact implements the original
specification and passes the applicable validation. Use the current Property IR,
SVA, assertion failures, and simulation logs as diagnostic evidence. They may
contain a faulty diagnostic, so when evidence conflicts, prioritize the
original specification and externally observable testbench behavior. Change
the HW-IR, not generated RTL, SVA, testbench, or reports. Preserve the module
interface and every correct, unrelated field. Do not delete behavior merely to
make an assertion vacuous.

checker1 and checker2 have already attempted all deterministic Python repairs
that are considered safe. Preserve those applied repairs. Resolve every
remaining checker error from its complete report; do not merely hide, delete,
or rename an offending record unless that removal is required by the original
specification.

Output ONLY one YAML mapping containing a JSON-Patch-style ``patch`` list. Each
operation must be minimal and use an RFC 6901 path into the CURRENT HW-IR:

patch:
  - op: replace
    path: /states/0/reset_value
    value: "'0"
  - op: add
    path: /derived_signals/-
    value: {name: example, width: 1, expression: "...", dependencies: []}

Supported operations are add, replace, and remove. Use ``-`` only to append to
a list. Do not replace the document root or an entire top-level states,
transitions, clock_domains, module, or derived_signals collection. Patch the
most specific fields/items instead. Paths and list indices must refer to the
CURRENT HW-IR included in the context, not to a rewritten schema. Do not output
the complete IR, markdown fences, explanations, SVA, or RTL.

Only if the CURRENT HW-IR is syntactically invalid YAML and therefore cannot
accept a structural path patch, output a minimal line patch instead:

text_edits:
  - start_line: 3
    end_line: 3
    replacement: "  correctly_indented_field: true"

Line numbers are 1-based and inclusive. Never replace the complete document;
repair only the malformed lines. Use exactly one of patch or text_edits."""


class HWIRPatchError(ValueError):
    """The model response is not a safe structural HW-IR patch."""


_PATCH_OPERATIONS = {"add", "replace", "remove"}
_PROTECTED_TOP_LEVEL_COLLECTIONS = {
    "module", "clock_domains", "states", "derived_signals", "transitions",
    "sequential_processes",
}


def _pointer_tokens(path):
    if not isinstance(path, str) or not path.startswith("/") or path == "/":
        raise HWIRPatchError(
            f"patch path must be a non-root RFC 6901 pointer, got {path!r}")
    return [token.replace("~1", "/").replace("~0", "~")
            for token in path[1:].split("/")]


def _parse_patch_document(content):
    text = strip_fences(content or "").strip()
    if not text:
        raise HWIRPatchError("LLM returned an empty patch response")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HWIRPatchError(f"patch response is not valid YAML: {exc}") from exc
    if not isinstance(document, dict) or "patch" not in document:
        raise HWIRPatchError(
            "response must be a mapping containing only a patch operation list; "
            "a complete replacement HW-IR is not accepted")
    unexpected = sorted(set(document) - {"patch"})
    if unexpected:
        raise HWIRPatchError(
            f"patch response contains unsupported root fields {unexpected}")
    operations = document["patch"]
    if not isinstance(operations, list) or not operations:
        raise HWIRPatchError("patch must be a non-empty list")
    if len(operations) > 128:
        raise HWIRPatchError("patch contains more than 128 operations")
    normalized = []
    for index, operation in enumerate(operations):
        if not isinstance(operation, dict):
            raise HWIRPatchError(f"patch[{index}] must be a mapping")
        op = operation.get("op")
        if op not in _PATCH_OPERATIONS:
            raise HWIRPatchError(
                f"patch[{index}].op must be add, replace, or remove")
        path = operation.get("path")
        tokens = _pointer_tokens(path)
        if (len(tokens) == 1 and tokens[0] in
                _PROTECTED_TOP_LEVEL_COLLECTIONS):
            raise HWIRPatchError(
                f"patch[{index}] may not {op} the complete top-level "
                f"collection {tokens[0]!r}; patch specific fields/items")
        if op in {"add", "replace"} and "value" not in operation:
            raise HWIRPatchError(f"patch[{index}] {op} requires value")
        unexpected_fields = sorted(
            set(operation) - {"op", "path", "value"})
        if unexpected_fields:
            raise HWIRPatchError(
                f"patch[{index}] has unsupported fields {unexpected_fields}")
        normalized.append({key: copy.deepcopy(operation[key])
                           for key in ("op", "path", "value")
                           if key in operation})
    return normalized, text


def _parse_text_edit_document(content, line_count):
    text = strip_fences(content or "").strip()
    if not text:
        raise HWIRPatchError("LLM returned an empty text patch response")
    try:
        document = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise HWIRPatchError(
            f"text patch response is not valid YAML: {exc}") from exc
    if not isinstance(document, dict) or set(document) != {"text_edits"}:
        raise HWIRPatchError(
            "syntactically invalid HW-IR requires a text_edits patch; a "
            "complete replacement HW-IR is not accepted")
    edits = document["text_edits"]
    if not isinstance(edits, list) or not edits or len(edits) > 64:
        raise HWIRPatchError("text_edits must contain 1 to 64 edits")
    normalized = []
    covered = set()
    for index, edit in enumerate(edits):
        if not isinstance(edit, dict) or set(edit) != {
                "start_line", "end_line", "replacement"}:
            raise HWIRPatchError(
                f"text_edits[{index}] requires only start_line, end_line, "
                "and replacement")
        start = edit["start_line"]
        end = edit["end_line"]
        if (not isinstance(start, int) or isinstance(start, bool) or
                not isinstance(end, int) or isinstance(end, bool) or
                start < 1 or end < start or end > line_count):
            raise HWIRPatchError(
                f"text_edits[{index}] has invalid line range {start!r}:{end!r}")
        interval = set(range(start, end + 1))
        if covered & interval:
            raise HWIRPatchError("text_edits line ranges may not overlap")
        covered |= interval
        normalized.append({
            "start_line": start, "end_line": end,
            "replacement": str(edit["replacement"]),
        })
    if len(covered) >= line_count:
        raise HWIRPatchError(
            "text_edits may not replace the complete HW-IR document")
    return normalized, text


def apply_hw_ir_text_edits(source_text, edits):
    """Apply minimal 1-based inclusive line edits to malformed YAML text."""
    lines = source_text.splitlines(keepends=True)
    for edit in sorted(edits, key=lambda item: item["start_line"], reverse=True):
        replacement = edit["replacement"]
        replacement_lines = replacement.splitlines(keepends=True)
        if replacement and replacement_lines and not replacement_lines[-1].endswith(
                ("\n", "\r")):
            replacement_lines[-1] += "\n"
        lines[edit["start_line"] - 1:edit["end_line"]] = replacement_lines
    return "".join(lines)


def _list_index(token, length, *, allow_end=False):
    if token == "-":
        if allow_end:
            return length
        raise HWIRPatchError("'-' is allowed only for list append")
    try:
        index = int(token)
    except (TypeError, ValueError) as exc:
        raise HWIRPatchError(f"list path component is not an index: {token!r}") from exc
    maximum = length if allow_end else length - 1
    if index < 0 or index > maximum:
        raise HWIRPatchError(
            f"list index {index} is outside allowed range 0..{maximum}")
    return index


def apply_hw_ir_patch(hw_ir, operations):
    """Apply validated JSON-Patch-style operations to a deep copy of HW-IR."""
    if not isinstance(hw_ir, dict):
        raise HWIRPatchError("current HW-IR root must be a mapping")
    candidate = copy.deepcopy(hw_ir)
    for operation_index, operation in enumerate(operations):
        op = operation["op"]
        tokens = _pointer_tokens(operation["path"])
        parent = candidate
        for token in tokens[:-1]:
            if isinstance(parent, dict):
                if token not in parent:
                    raise HWIRPatchError(
                        f"patch[{operation_index}] path component {token!r} "
                        "does not exist")
                parent = parent[token]
            elif isinstance(parent, list):
                parent = parent[_list_index(token, len(parent))]
            else:
                raise HWIRPatchError(
                    f"patch[{operation_index}] traverses a scalar at {token!r}")
        leaf = tokens[-1]
        if isinstance(parent, dict):
            if op == "add":
                parent[leaf] = copy.deepcopy(operation["value"])
            elif op == "replace":
                if leaf not in parent:
                    raise HWIRPatchError(
                        f"patch[{operation_index}] replace target does not "
                        f"exist: {operation['path']}")
                parent[leaf] = copy.deepcopy(operation["value"])
            else:
                if leaf not in parent:
                    raise HWIRPatchError(
                        f"patch[{operation_index}] remove target does not "
                        f"exist: {operation['path']}")
                del parent[leaf]
        elif isinstance(parent, list):
            if op == "add":
                index = _list_index(leaf, len(parent), allow_end=True)
                parent.insert(index, copy.deepcopy(operation["value"]))
            else:
                index = _list_index(leaf, len(parent))
                if op == "replace":
                    parent[index] = copy.deepcopy(operation["value"])
                else:
                    del parent[index]
        else:
            raise HWIRPatchError(
                f"patch[{operation_index}] target parent is a scalar")
    return candidate


def _read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None


def _checker_issues(report, checker):
    if not isinstance(report, dict):
        return [{"checker": checker, "message": "checker report unavailable"}]
    errors = report.get("unresolved_errors", report.get("errors", []))
    return [{"checker": checker, **item} if isinstance(item, dict)
            else {"checker": checker, "message": str(item)}
            for item in errors]


def _validate_candidate(candidate_path, output, attempt):
    checker1_path = output / f"candidate_checker1_attempt_{attempt}.json"
    checker2_path = output / f"candidate_checker2_attempt_{attempt}.json"
    validation_path = output / f"candidate_validation_attempt_{attempt}.json"
    issues = []
    checker1_report = None
    checker2_report = None
    try:
        checker1_report = check_ir_format(candidate_path, checker1_path)
    except (Checker1Error, OSError, UnicodeError, ValueError) as exc:
        checker1_report = _read_json(checker1_path)
        issues = _checker_issues(checker1_report, "checker1") or [{
            "checker": "checker1", "message": str(exc)}]
    if not issues:
        try:
            checker2_report = check_ir_semantics(candidate_path, checker2_path)
        except (Checker2Error, OSError, UnicodeError, ValueError,
                RuntimeError) as exc:
            checker2_report = _read_json(checker2_path)
            issues = _checker_issues(checker2_report, "checker2") or [{
                "checker": "checker2", "message": str(exc)}]
    validation = {
        "status": "passed" if not issues else "failed",
        "candidate": str(candidate_path),
        "checker1_report": str(checker1_path),
        "checker1_status": (checker1_report or {}).get("status"),
        "checker2_report": (str(checker2_path)
                            if checker2_report is not None else None),
        "checker2_status": (checker2_report or {}).get("status"),
        "issues": issues,
    }
    validation_path.write_text(
        json.dumps(validation, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return validation, validation_path


def _section(title, content):
    return (f"\n===== BEGIN {title} =====\n{content}"
            f"\n===== END {title} =====\n")


def _file_section(path, label=None):
    source = Path(path).resolve()
    if not source.is_file():
        return _section(label or str(source), f"<missing file: {source}>")
    return _section(
        f"{label or source.name} ({source})",
        source.read_text(encoding="utf-8", errors="replace"))


def build_repair_context(*, iteration, failure, spec_path, hw_ir_path,
                         property_ir_path, rtl_path, instrumented_rtl_path,
                         sva_path, testbenches, extra_sources, artifacts,
                         history, sva_enabled=True, output_mode="rtl", generation_contract=None):
    """Build an untruncated, reproducible repair context document."""
    verification_description = (
        "or HLS C++ generation" if output_mode == "hls" else
        "or integrated RTL + SVA simulation" if sva_enabled else
        "or RTL-only simulation (--no_sva is active)")
    parts = [
        _section("CONFIRMED GENERATION CONTRACT (must preserve)", json.dumps(generation_contract, ensure_ascii=False, indent=2)) if generation_contract else "",
        "# HW-IR Repair Feedback Context\n",
        "The following is the complete context for one failed HW-IR pipeline "
        "attempt. The failure may come from checker1, checker2, a downstream "
        f"deterministic stage, {verification_description}. Repair only the "
        "HW-IR.\n",
        _section("ITERATION", str(iteration)),
        _section("FAILURE SUMMARY JSON", json.dumps(
            failure, ensure_ascii=False, indent=2)),
        _section("REPAIR HISTORY JSON", json.dumps(
            history, ensure_ascii=False, indent=2)),
    ]
    if spec_path:
        parts.append(_file_section(spec_path, "ORIGINAL SPECIFICATION"))
    else:
        parts.append(_section(
            "ORIGINAL SPECIFICATION",
            "<not provided; infer intended behavior from HW-IR and testbench>"))
    parts.append(_file_section(hw_ir_path, "CURRENT HW-IR TO REPAIR"))
    if sva_enabled:
        parts.append(_file_section(
            property_ir_path, "CURRENT TEMPORAL PROPERTY IR"))
    if output_mode != "hls":
        parts.append(_file_section(rtl_path, "CURRENT GENERATED RTL"))
    if sva_enabled and output_mode != "hls":
        parts.extend([
            _file_section(instrumented_rtl_path,
                          "CURRENT GENERATED RTL WITH SVA"),
            _file_section(sva_path, "CURRENT GENERATED SVA"),
        ])
    for index, path in enumerate(testbenches, 1):
        parts.append(_file_section(path, f"TESTBENCH {index}"))
    if not testbenches:
        parts.append(_section("TESTBENCHES", "<none>"))
    for index, path in enumerate(extra_sources, 1):
        parts.append(_file_section(path, f"EXTRA RTL SOURCE {index}"))
    if not extra_sources:
        parts.append(_section("EXTRA RTL SOURCES", "<none>"))
    for label, path in artifacts:
        parts.append(_file_section(path, label))
    repair_requirement = (
        "Return one minimal YAML patch list against CURRENT HW-IR, following "
        "the system prompt schema. The patch will be applied transactionally "
        "to a staging copy and must pass YAML parsing, checker1, and checker2 "
        "before it can replace the working IR.")
    if output_mode == "hls":
        repair_requirement += (
            " The next effective HW-IR attempt will generate HLS C++ only; "
            "RTL compilation, Verilator, Property IR, and SVA remain disabled.")
    elif sva_enabled:
        repair_requirement += (
            " The next effective HW-IR attempt will compile RTL and run the "
            "same testbench without SVA."
            " Property IR extraction/checking, SVA generation, and RTL+SVA "
            "diagnostic simulation are activated only if that RTL-only "
            "simulation still fails.")
    else:
        repair_requirement += (
            " The next effective HW-IR attempt will compile RTL and run the "
            "same testbench without SVA."
            " Because --no_sva is active, Property IR/SVA generation and "
            "SVA-derived HW-IR feedback remain disabled on every repair "
            "iteration.")
    parts.append(_section("REPAIR REQUIREMENT", repair_requirement))
    return "".join(parts)


def _response_meta(response):
    usage = getattr(response, "usage", None)
    choices = getattr(response, "choices", []) or []
    choice = choices[0] if choices else None
    return {
        "id": getattr(response, "id", None),
        "model": getattr(response, "model", None),
        "finish_reason": getattr(choice, "finish_reason", None),
        "usage": usage.model_dump() if usage is not None else None,
    }


def repair_hw_ir(*, context_text, output_dir, hw_ir_path=None,
                 api_key=None, base_url=None, model=None, temperature=0.2,
                 max_tokens=8192, max_patch_attempts=3):
    """Generate, apply, and validate a transactional HW-IR patch."""
    api_key = api_key or os.environ.get("OPENAI_API_KEY")
    base_url = base_url or os.environ.get("OPENAI_BASE_URL")
    model = model or os.environ.get("OPENAI_MODEL")
    if not api_key:
        raise ValueError(
            "API key is required to repair HW-IR from simulation feedback")
    if not base_url:
        raise ValueError(
            "Base URL is required to repair HW-IR from simulation feedback")
    if not model:
        raise ValueError(
            "Model name is required to repair HW-IR from simulation feedback")
    if OpenAI is None:
        raise RuntimeError(
            "The openai package is required to repair HW-IR from simulation feedback")

    output = Path(output_dir).resolve()
    output.mkdir(parents=True, exist_ok=True)
    base_path = Path(
        hw_ir_path or output / "input_hw_ir.yaml").resolve()
    if not base_path.is_file():
        raise ValueError(f"HW-IR to repair was not found: {base_path}")
    try:
        base_text = base_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Failed to read HW-IR to repair: {exc}") from exc
    try:
        base_document = yaml.safe_load(base_text)
    except yaml.YAMLError:
        base_document = None
    if base_document is not None and not isinstance(base_document, dict):
        base_document = None
    if max_patch_attempts < 1:
        raise ValueError("max_patch_attempts must be at least 1")

    context_path = output / "feedback_context.md"
    messages_path = output / "repair_messages.json"
    raw_path = output / "raw_repair_response.txt"
    response_path = output / "repair_response.json"
    patch_path = output / "repair_patch.yaml"
    candidate_path = output / "candidate_hw_ir.yaml"
    for stale in (raw_path, response_path, patch_path, candidate_path):
        if stale.is_file():
            stale.unlink()
    context_path.write_text(context_text, encoding="utf-8")
    messages = [
        {"role": "system", "content": REPAIR_SYSTEM_PROMPT},
        {"role": "user", "content": context_text},
    ]
    client = OpenAI(api_key=api_key, base_url=base_url)
    attempts = []
    final_response = None
    for attempt in range(1, max_patch_attempts + 1):
        print(f"[HW-IR patch candidate {attempt}/{max_patch_attempts}] "
              "Generating a minimal patch; the working IR is unchanged")
        messages_path.write_text(
            json.dumps(messages, ensure_ascii=False, indent=2),
            encoding="utf-8")
        (output / f"repair_messages_attempt_{attempt}.json").write_text(
            json.dumps(messages, ensure_ascii=False, indent=2),
            encoding="utf-8")
        response = llm_stream.create(
            client, model=model, messages=messages,
            temperature=temperature, max_tokens=max_tokens)
        final_response = response
        choices = getattr(response, "choices", []) or []
        content = (getattr(choices[0].message, "content", "")
                   if choices else "") or ""
        raw_attempt = output / f"raw_repair_response_attempt_{attempt}.txt"
        raw_attempt.write_text(content, encoding="utf-8")
        raw_path.write_text(content, encoding="utf-8")
        attempt_record = {
            "patch_attempt": attempt,
            "response": _response_meta(response),
            "raw_response": str(raw_attempt),
            "status": "rejected",
            "issues": [],
        }
        try:
            if base_document is not None:
                repair_mode = "structural_patch"
                operations, patch_text = _parse_patch_document(content)
                candidate = apply_hw_ir_patch(base_document, operations)
                candidate_text = yaml.safe_dump(
                    candidate, allow_unicode=True, sort_keys=False,
                    default_flow_style=False)
                persisted_patch = {"patch": operations}
            else:
                repair_mode = "text_patch"
                operations, patch_text = _parse_text_edit_document(
                    content, len(base_text.splitlines()))
                candidate_text = apply_hw_ir_text_edits(base_text, operations)
                persisted_patch = {"text_edits": operations}
            patch_attempt = output / f"repair_patch_attempt_{attempt}.yaml"
            patch_attempt.write_text(patch_text + "\n", encoding="utf-8")
            attempt_record["patch"] = str(patch_attempt)
            attempt_record["repair_mode"] = repair_mode
            staging = output / f"candidate_hw_ir_attempt_{attempt}.yaml"
            staging.write_text(candidate_text, encoding="utf-8")
            validation, validation_path = _validate_candidate(
                staging, output, attempt)
            attempt_record["candidate"] = str(staging)
            attempt_record["validation"] = str(validation_path)
            attempt_record["issues"] = validation["issues"]
        except (HWIRPatchError, OSError, UnicodeError, yaml.YAMLError,
                ValueError) as exc:
            attempt_record["issues"] = [{
                "checker": "patch", "message": str(exc)}]
            validation = None

        if validation is not None and validation["status"] == "passed":
            attempt_record["status"] = "validated"
            attempts.append(attempt_record)
            shutil.copyfile(str(staging), str(candidate_path))
            patch_path.write_text(yaml.safe_dump(
                persisted_patch, allow_unicode=True, sort_keys=False,
                default_flow_style=False), encoding="utf-8")
            meta = _response_meta(response)
            meta.update({
                "status": "validated",
                "context": str(context_path),
                "messages": str(messages_path),
                "raw_response": str(raw_path),
                "repair_patch": str(patch_path),
                "repair_mode": repair_mode,
                "candidate_hw_ir": str(candidate_path),
                "candidate_validation": attempt_record["validation"],
                "patch_attempts_used": attempt,
                "max_patch_attempts": max_patch_attempts,
                "attempts": attempts,
                "working_ir_untouched_until_validation": True,
            })
            response_path.write_text(
                json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8")
            print(f"[HW-IR patch candidate {attempt}/{max_patch_attempts}] "
                  "checker1/checker2 pre-commit validation passed")
            return candidate_path, meta

        attempts.append(attempt_record)
        issue_summary = attempt_record["issues"][0].get(
            "message", "candidate validation failed")
        print(f"[HW-IR patch candidate {attempt}/{max_patch_attempts}] "
              f"Rejected; the working IR remains unchanged: {issue_summary}")
        if attempt < max_patch_attempts:
            messages.extend([
                {"role": "assistant", "content": content or "<empty response>"},
                {"role": "user", "content": (
                    "The proposed patch was rejected before it could modify "
                    "the working HW-IR. Return one complete corrected patch "
                    "document relative to the ORIGINAL CURRENT HW-IR, not "
                    "relative to the rejected candidate. Preserve the current "
                    "schema and fix every issue below. Do not return a complete "
                    "HW-IR.\n\nPatch/candidate validation issues:\n" +
                    json.dumps(attempt_record["issues"],
                               ensure_ascii=False, indent=2))},
            ])

    meta = _response_meta(final_response) if final_response is not None else {}
    meta.update({
        "status": "rejected",
        "context": str(context_path),
        "messages": str(messages_path),
        "raw_response": str(raw_path),
        "repair_patch": None,
        "candidate_hw_ir": None,
        "patch_attempts_used": max_patch_attempts,
        "max_patch_attempts": max_patch_attempts,
        "attempts": attempts,
        "working_ir_untouched_until_validation": True,
    })
    response_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    raise ValueError(
        "HW-IR patch validation failed repeatedly; the working HW-IR was not "
        "overwritten; see "
        f"{response_path}")
