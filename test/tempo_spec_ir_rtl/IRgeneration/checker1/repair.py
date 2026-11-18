import json
import re

import yaml

from .parser import parse_text


# These fields contain Verilog scalar text in the HW-PN IR contract.  Quoting
# their suspicious YAML values changes representation only, not hardware
# semantics.
VERILOG_SCALAR_FIELDS = {
    "guard", "trigger", "expression", "action", "else_action",
    "reset_value", "transformation",
}
FIELD_LINE = re.compile(
    r"^(?P<indent>[ \t-]*)(?P<key>[A-Za-z_][\w-]*)"
    r"(?P<separator>\s*:\s*)(?P<value>.*)$")
GROUP_ITEM = re.compile(
    r"^(?P<indent> +)-\s+(?P<value>\"[^\"]*\"|'[^']*'|[A-Za-z_$][\w$]*)\s*$")
GROUP_CONTINUATION = re.compile(
    r"^(?P<indent> +)(?P<value>\"[^\"]*\"|'[^']*'|[A-Za-z_$][\w$]*)\s*$")
ACTION_LIST_ITEM = re.compile(
    r"^(?P<indent>\s*)-\s+(?P<value>[A-Za-z_$][A-Za-z0-9_$]*(?:\[[^]]+\])?"
    r"\s*(?:<=|=(?!=))\s*.+)$")


def _split_comment(value):
    """Split a plain YAML scalar from a trailing comment."""
    marker = re.search(r"\s+#", value)
    if not marker:
        return value.rstrip(), ""
    return value[:marker.start()].rstrip(), value[marker.start():]


def _already_quoted(value):
    return (len(value) >= 2 and value[0] == value[-1] and
            value[0] in {'"', "'"})


def _suspicious_scalar(key, value):
    stripped = value.strip()
    if not stripped or _already_quoted(stripped):
        return False
    # A YAML double-quoted scalar may legally continue onto following lines.
    # The complete document parser handles it; quoting only its first physical
    # line would corrupt an otherwise valid expression.
    if stripped.startswith('"'):
        return False
    if stripped[0] in "!&*{":
        if key in {"action", "else_action"} and stripped.startswith("{") and ":" in stripped:
            return False
        return True
    # Verilog unsized literals such as '0 look like an unterminated YAML quote.
    # Do not treat every leading apostrophe as such: PyYAML legitimately emits
    # long single-quoted scalars across physical lines, with the closing quote
    # on a continuation line.
    if stripped.startswith("'") and not stripped.endswith("'"):
        return bool(re.fullmatch(r"'[01xXzZ]", stripped))
    if key in {"expression", "guard", "trigger", "transformation"}:
        if stripped.lower() in {
                "yes", "no", "on", "off", "null", "~", "true", "false"}:
            return True
        return ": " in stripped or stripped.endswith(":")
    return False


def quote_verilog_scalars(text):
    """Quote only YAML-ambiguous Verilog scalar fields.

    Returns ``(new_text, repairs)``.  Multiline Verilog concatenations under an
    expression field are collapsed to one quoted scalar.
    """
    lines = text.splitlines(keepends=True)
    output = []
    repairs = []
    index = 0
    while index < len(lines):
        raw_line = lines[index]
        line = raw_line.rstrip("\r\n")
        newline = raw_line[len(line):]
        action_item = ACTION_LIST_ITEM.match(line)
        if action_item:
            value, comment = _split_comment(action_item.group("value"))
            inner_rhs = re.match(
                r"^(?P<prefix>[A-Za-z_$][A-Za-z0-9_$]*(?:\[[^]]+\])?"
                r"\s*(?:<=|=(?!=))\s*)(?P<quote>[\"'])(?P<rhs>.*)(?P=quote)$",
                value.strip())
            if (inner_rhs and "?" in inner_rhs.group("rhs") and
                    ":" in inner_rhs.group("rhs")):
                value = inner_rhs.group("prefix") + inner_rhs.group("rhs")
            if not _already_quoted(value.strip()) and ": " in value:
                quoted = json.dumps(value.strip(), ensure_ascii=False)
                output.append(
                    f"{action_item.group('indent')}- {quoted}{comment}{newline or chr(10)}")
                repairs.append({
                    "repair_type": "quote_verilog_expression",
                    "location": f"line {index + 1}",
                    "line": index + 1,
                    "end_line": index + 1,
                    "field": "action",
                    "before": value.strip(),
                    "after": quoted,
                    "message": "Quoted YAML-ambiguous Verilog action list item",
                })
                index += 1
                continue
        match = FIELD_LINE.match(line)
        if not match or match.group("key") not in VERILOG_SCALAR_FIELDS:
            output.append(raw_line)
            index += 1
            continue
        key = match.group("key")
        value, comment = _split_comment(match.group("value"))
        consumed = 1
        combined = value.strip()
        if combined.startswith("{"):
            balance = combined.count("{") - combined.count("}")
            while balance > 0 and index + consumed < len(lines):
                continuation = lines[index + consumed].strip()
                combined += " " + continuation
                balance += continuation.count("{") - continuation.count("}")
                consumed += 1
        if not _suspicious_scalar(key, combined):
            output.append(raw_line)
            index += 1
            continue
        quoted = json.dumps(combined, ensure_ascii=False)
        replacement = (match.group("indent") + key + match.group("separator") +
                       quoted + comment + (newline or "\n"))
        output.append(replacement)
        repairs.append({
            "repair_type": "quote_verilog_expression",
            "location": f"line {index + 1}",
            "line": index + 1,
            "end_line": index + consumed,
            "field": key,
            "before": combined,
            "after": quoted,
            "message": "Quoted YAML-ambiguous Verilog scalar",
        })
        index += consumed
    result = "".join(output)
    if text.endswith(("\n", "\r")) and not result.endswith("\n"):
        result += "\n"
    return result, repairs


def repair_concurrency_groups(text):
    """Repair LLM-emitted continuation strings under concurrency.groups.

    The transformation is intentionally scoped to this exact schema location,
    where a group is defined as a list of transition names.
    """
    lines = text.splitlines(keepends=True)
    output = []
    repairs = []
    index = 0
    concurrency_indent = None
    groups_indent = None
    while index < len(lines):
        raw = lines[index]
        content = raw.rstrip("\r\n")
        stripped = content.strip()
        indent = len(content) - len(content.lstrip(" "))

        if re.match(r"^\s*concurrency\s*:\s*$", content):
            concurrency_indent = indent
            groups_indent = None
            output.append(raw)
            index += 1
            continue
        if concurrency_indent is not None and stripped and indent <= concurrency_indent:
            concurrency_indent = None
            groups_indent = None
        if (concurrency_indent is not None and
                re.match(r"^\s*groups\s*:\s*$", content) and
                indent > concurrency_indent):
            groups_indent = indent
            output.append(raw)
            index += 1
            continue
        if groups_indent is not None and stripped and indent <= groups_indent:
            groups_indent = None

        leader = GROUP_ITEM.match(content) if groups_indent is not None else None
        if not leader or len(leader.group("indent")) <= groups_indent:
            output.append(raw)
            index += 1
            continue

        values = [leader.group("value")]
        consumed = 1
        leader_indent = len(leader.group("indent"))
        while index + consumed < len(lines):
            following = lines[index + consumed].rstrip("\r\n")
            continuation = GROUP_CONTINUATION.match(following)
            if (not continuation or
                    len(continuation.group("indent")) <= leader_indent):
                break
            values.append(continuation.group("value"))
            consumed += 1
        if consumed == 1:
            output.append(raw)
            index += 1
            continue

        decoded = [yaml.safe_load(value) for value in values]
        encoded = ", ".join(json.dumps(str(value), ensure_ascii=False)
                            for value in decoded)
        newline = "\n" if raw.endswith("\n") else ""
        replacement = f"{leader.group('indent')}- [{encoded}]{newline}"
        output.append(replacement)
        repairs.append({
            "repair_type": "normalize_concurrency_group",
            "location": f"line {index + 1}",
            "line": index + 1,
            "end_line": index + consumed,
            "field": "concurrency.groups",
            "before": [str(value) for value in decoded],
            "after": f"[{encoded}]",
            "message": "Converted continuation strings into one transition-name list",
        })
        index += consumed
    return "".join(output), repairs


def repair_duplicate_keys(text):
    """Deterministically keep the last duplicate key and re-emit valid YAML."""
    try:
        data = yaml.safe_load(text)
    except yaml.YAMLError:
        return text, []
    if not isinstance(data, dict):
        return text, []
    dumped = yaml.safe_dump(
        data, allow_unicode=True, sort_keys=False, default_flow_style=False)
    return dumped, [{
        "repair_type": "deduplicate_mapping_keys",
        "location": "$",
        "message": "Removed duplicate YAML mapping keys deterministically; kept the last value",
    }]


def auto_repair_text(text):
    """Apply safe deterministic repairs and return text, repairs, parse issues."""
    repaired, repairs = repair_concurrency_groups(text)
    repaired, scalar_repairs = quote_verilog_scalars(repaired)
    repairs.extend(scalar_repairs)
    _, issues = parse_text(repaired)
    if issues and "duplicate key" in issues[0].message.lower():
        repaired, duplicate_repairs = repair_duplicate_keys(repaired)
        repairs.extend(duplicate_repairs)
        _, issues = parse_text(repaired)
    return repaired, repairs, issues
