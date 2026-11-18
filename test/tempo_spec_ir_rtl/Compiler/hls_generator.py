"""Deterministic lowering from checked HW-IR to Vitis-style HLS C++."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

from Compiler.rtl_compiler.lowering import build_processes, is_reset_transition, ordered_for_target
from Compiler.rtl_compiler.parser import parse_ir


class HLSGenerationError(RuntimeError):
    """Raised when HW-IR cannot be represented safely as HLS C++."""


_VERILOG_LITERAL = re.compile(
    r"(?<![A-Za-z0-9_$])(?:(\d+))?'([sS])?([bBoOdDhH])([0-9a-fA-F_xXzZ?]+)")
_IDENTIFIER = re.compile(r"\b[A-Za-z_][A-Za-z0-9_$]*\b")
_SLICE = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_$]*)\s*\[\s*([^:\]]+)\s*:\s*([^\]]+)\s*\]")
_BIT_SELECT = re.compile(
    r"\b([A-Za-z_][A-Za-z0-9_$]*)\s*\[\s*([^:\]]+)\s*\]")
_MEMORY_RESOURCE_TYPES = {
    "", "memory", "ram", "register_file", "register file",
}
_OPERATOR_RESOURCE_TYPES = {
    "multiplier", "mul", "adder", "subtractor", "substractor", "alu",
    "shifter", "comparator", "divider", "div", "mac", "mux", "operator",
    "combinational", "arithmetic", "logic", "incrementer", "decrementer",
}
_ANNOTATION_IMPLEMENTATIONS = {
    "", "behavioral", "inline", "inferred", "none", "operator",
}


def _records_by_name(items):
    return {str(item.get("name", "")): item for item in items
            if isinstance(item, dict) and item.get("name")}


def _iter_derived(model):
    for signal in model.derived_signals:
        if isinstance(signal, dict) and signal.get("name"):
            yield signal


def _width_map(model):
    widths = {}
    for port in model.ports:
        widths[port.name] = port.width
    for state in model.states:
        widths[state.name] = state.width
    for signal in _iter_derived(model):
        widths[str(signal.get("name"))] = signal.get("width", 1)
    return widths


def _numeric_width(width):
    if width is None:
        return None
    if isinstance(width, int):
        return width
    text = str(width).strip()
    match = re.fullmatch(r"(\d+)\s*(?:bits?)?", text, re.I)
    if match:
        return int(match.group(1))
    return None


def _infer_item_width(expr, widths):
    text = str(expr).strip()
    if not text:
        return "1"
    literal = _VERILOG_LITERAL.fullmatch(text)
    if literal and literal.group(1):
        return literal.group(1)
    if re.fullmatch(r"~\s*.+", text):
        return _infer_item_width(text.lstrip("~").strip(), widths)
    ident = re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", text)
    if ident and ident.group(0) in widths:
        numeric = _numeric_width(widths[ident.group(0)])
        return str(numeric if numeric is not None else widths[ident.group(0)])
    bit_sel = re.fullmatch(
        r"([A-Za-z_][A-Za-z0-9_$]*)\s*\[\s*([^:\]]+)\s*\]", text)
    if bit_sel:
        return "1"
    part = re.fullmatch(
        r"([A-Za-z_][A-Za-z0-9_$]*)\s*\[\s*([^:\]]+)\s*:\s*([^\]]+)\s*\]", text)
    if part:
        try:
            return str(int(part.group(2).strip()) - int(part.group(3).strip()) + 1)
        except ValueError:
            return f"(({part.group(2).strip()})-({part.group(3).strip()})+1)"
    return "1"


def _literal(match, *, sized):
    digits = match.group(4).replace("_", "")
    digits = re.sub(r"[xXzZ?]", "0", digits)
    if not digits:
        digits = "0"
    base = match.group(3).lower()
    prefix = {"b": "0b", "o": "0", "d": "", "h": "0x"}[base]
    value = prefix + digits
    width = match.group(1)
    if not sized or not width:
        return value
    kind = "ap_int" if match.group(2) else "ap_uint"
    return f"{kind}<{width}>({value})"


def _matching_pair(text, start, open_ch="{", close_ch="}"):
    depth = 0
    for index in range(start, len(text)):
        char = text[index]
        if char == open_ch:
            depth += 1
        elif char == close_ch:
            depth -= 1
            if depth == 0:
                return index
    raise HLSGenerationError(f"unbalanced '{open_ch}' in expression: {text}")


def _normalize_width_text(width):
    text = str(width if width is not None else 1).strip()
    bit_count = re.fullmatch(r"(\d+)\s*(?:bits?)?", text, re.I)
    if bit_count:
        return bit_count.group(1)
    packed_range = re.fullmatch(r"\[\s*([^:]+)\s*:\s*([^]]+)\s*\]", text)
    if packed_range:
        return (
            f"(({packed_range.group(1).strip()}) - "
            f"({packed_range.group(2).strip()}) + 1)")
    return text.replace("$clog2", "hls_clog2") or "1"


def _safe_width_expr(width):
    """Return a constexpr bitwidth expression that is always >= 1."""
    text = _normalize_width_text(width)
    if re.fullmatch(r"\d+", text):
        return text if int(text) > 0 else "1"
    # Fold trivial nested sums of literals: ((8)+(16)) -> 24
    folded = _fold_width_sum(text)
    if folded is not None:
        return folded if folded > 0 else "1"
    return f"hls_width({text})"


def _fold_width_sum(text):
    text = str(text).strip()
    if re.fullmatch(r"\d+", text):
        return int(text)
    match = re.fullmatch(r"\(\((.+)\)\+\((.+)\)\)", text)
    if not match:
        # also allow (a)+(b) without extra parens layer
        match = re.fullmatch(r"\((.+)\)\+\((.+)\)", text)
    if not match:
        return None
    left = _fold_width_sum(match.group(1).strip())
    right = _fold_width_sum(match.group(2).strip())
    if left is None or right is None:
        return None
    return left + right


def _add_width_exprs(left, right):
    left_fold = _fold_width_sum(left)
    right_fold = _fold_width_sum(right)
    if left_fold is not None and right_fold is not None:
        total = left_fold + right_fold
        return str(total if total > 0 else 1)
    return f"(({left})+({right}))"


def _cpp_width(width):
    return _safe_width_expr(width)


def _strip_outer_parens(text):
    text = str(text).strip()
    while text.startswith("(") and len(text) >= 2:
        try:
            close = _matching_pair(text, 0, "(", ")")
        except HLSGenerationError:
            break
        if close != len(text) - 1:
            break
        text = text[1:-1].strip()
    return text


def _top_level_index(text, char, *, start=0):
    depth_brace = depth_paren = depth_brack = 0
    for index in range(start, len(text)):
        current = text[index]
        if current == "{":
            depth_brace += 1
        elif current == "}":
            depth_brace -= 1
        elif current == "(":
            depth_paren += 1
        elif current == ")":
            depth_paren -= 1
        elif current == "[":
            depth_brack += 1
        elif current == "]":
            depth_brack -= 1
        elif (current == char and depth_brace == 0 and depth_paren == 0
              and depth_brack == 0):
            return index
    return None


def _split_ternary(text):
    text = _strip_outer_parens(text)
    question = _top_level_index(text, "?")
    if question is None:
        return None
    expect = 0
    depth_brace = depth_paren = depth_brack = 0
    for index in range(question + 1, len(text)):
        current = text[index]
        if current == "{":
            depth_brace += 1
        elif current == "}":
            depth_brace -= 1
        elif current == "(":
            depth_paren += 1
        elif current == ")":
            depth_paren -= 1
        elif current == "[":
            depth_brack += 1
        elif current == "]":
            depth_brack -= 1
        elif depth_brace == 0 and depth_paren == 0 and depth_brack == 0:
            if current == "?":
                expect += 1
            elif current == ":":
                if expect == 0:
                    return (
                        text[:question].strip(),
                        text[question + 1:index].strip(),
                        text[index + 1:].strip())
                expect -= 1
    return None


def _cast_to_width(text, width):
    if not width:
        return text
    split = _split_ternary(text)
    if split:
        cond, true_expr, false_expr = split
        return (
            f"hls_mux<{width}>({cond}, "
            f"{_cast_to_width(true_expr, width)}, "
            f"{_cast_to_width(false_expr, width)})")
    return f"hls_trunc<{width}>({text})"


def _wrap_bit_selects(text, array_names):
    def replace(match):
        name = match.group(1)
        index = match.group(2).strip()
        if name in array_names:
            return match.group(0)
        prefix = text[max(0, match.start() - 11):match.start()]
        if prefix.endswith("ap_uint<1>("):
            return match.group(0)
        return f"ap_uint<1>({name}[{index}])"

    return _BIT_SELECT.sub(replace, text)


def _split_top_level(text, separator=","):
    parts = []
    start = 0
    depth_brace = depth_paren = depth_brack = 0
    for index, char in enumerate(text):
        if char == "{":
            depth_brace += 1
        elif char == "}":
            depth_brace -= 1
        elif char == "(":
            depth_paren += 1
        elif char == ")":
            depth_paren -= 1
        elif char == "[":
            depth_brack += 1
        elif char == "]":
            depth_brack -= 1
        elif (char == separator and depth_brace == 0 and depth_paren == 0
              and depth_brack == 0):
            parts.append(text[start:index])
            start = index + 1
    parts.append(text[start:])
    return parts


def _parse_replication(item):
    text = item.strip()
    depth_paren = depth_brack = 0
    for index, char in enumerate(text):
        if char == "(":
            depth_paren += 1
        elif char == ")":
            depth_paren -= 1
        elif char == "[":
            depth_brack += 1
        elif char == "]":
            depth_brack -= 1
        elif char == "{" and depth_paren == 0 and depth_brack == 0:
            close = _matching_pair(text, index)
            if close != len(text) - 1:
                return None
            multiplicity = text[:index].strip()
            inner = text[index + 1:close]
            if multiplicity:
                return multiplicity, inner
            return None
    return None


def _unary_reduction_operand_start(text, index):
    if index == 0:
        return True
    previous = index - 1
    while previous >= 0 and text[previous].isspace():
        previous -= 1
    if previous < 0:
        return True
    return text[previous] in "=?:(,;{+~!<>"


def _parse_operand(text, start):
    index = start
    while index < len(text) and text[index].isspace():
        index += 1
    if index >= len(text):
        raise HLSGenerationError(f"missing operand in {text}")
    if text[index] in "{([":
        pairs = {"{": "}", "(": ")", "[": "]"}
        close = _matching_pair(text, index, text[index], pairs[text[index]])
        end = close + 1
        while end < len(text) and text[end] in "[(":
            pair = {"[": "]", "(": ")"}[text[end]]
            end = _matching_pair(text, end, text[end], pair) + 1
        return start, end
    end = index
    if text[end] in "~!+-":
        end += 1
        while end < len(text) and text[end].isspace():
            end += 1
    while end < len(text) and (text[end].isalnum() or text[end] in "_$"):
        end += 1
    while end < len(text) and text[end] in "[(":
        pair = {"[": "]", "(": ")"}[text[end]]
        end = _matching_pair(text, end, text[end], pair) + 1
    return start, end


def _lower_reductions(text):
    operators = (
        ("~|", "!({operand}).or_reduce()"),
        ("~&", "!({operand}).and_reduce()"),
        ("~^", "!({operand}).xor_reduce()"),
        ("|", "({operand}).or_reduce()"),
        ("&", "({operand}).and_reduce()"),
        ("^", "({operand}).xor_reduce()"),
    )
    index = 0
    output = []
    while index < len(text):
        matched = False
        for token, template in operators:
            if not text.startswith(token, index):
                continue
            if token == "|" and (
                    text.startswith("||", index) or
                    (index and text[index - 1] == "|")):
                continue
            if token == "&" and (
                    text.startswith("&&", index) or
                    (index and text[index - 1] == "&")):
                continue
            if not _unary_reduction_operand_start(text, index):
                continue
            operand_start = index + len(token)
            _, operand_end = _parse_operand(text, operand_start)
            operand = text[operand_start:operand_end].strip()
            output.append(template.format(operand=operand))
            index = operand_end
            matched = True
            break
        if not matched:
            output.append(text[index])
            index += 1
    return "".join(output)


def _replace_dollar_call(text, function, wrapper):
    token = f"${function}"
    token_l = token.lower()
    lowered = text.lower()
    index = 0
    output = []
    while True:
        found = lowered.find(token_l, index)
        if found < 0:
            output.append(text[index:])
            break
        output.append(text[index:found])
        cursor = found + len(token)
        while cursor < len(text) and text[cursor].isspace():
            cursor += 1
        if cursor >= len(text) or text[cursor] != "(":
            output.append(text[found:found + len(token)])
            index = found + len(token)
            continue
        close = _matching_pair(text, cursor, "(", ")")
        inner = text[cursor + 1:close]
        output.append(f"{wrapper}({inner})")
        index = close + 1
    return "".join(output)


def _lower_concat_body(body, replacements, widths, as_parameter, array_names):
    items = [item.strip() for item in _split_top_level(body) if item.strip()]
    if not items:
        return "hls_trunc<1>(0)"
    parts = []
    part_widths = []
    for item in items:
        replication = _parse_replication(item)
        if replication:
            multiplicity, inner = replication
            count = _translate_expression(
                multiplicity, replacements, as_parameter=True, widths=widths,
                array_names=array_names)
            inner_cpp = _translate_expression(
                inner, replacements, as_parameter=as_parameter, widths=widths,
                array_names=array_names)
            width = _infer_item_width(inner, widths or {})
            safe_width = _safe_width_expr(width)
            safe_count = _safe_width_expr(count)
            parts.append(
                f"hls_replicate<{safe_count}, {safe_width}>("
                f"hls_trunc<{safe_width}>({inner_cpp}))")
            part_widths.append(f"(({safe_count})*({safe_width}))")
            continue
        part = _translate_expression(
            item, replacements, as_parameter=as_parameter, widths=widths,
            array_names=array_names)
        width = _infer_item_width(item, widths or {})
        safe_width = _safe_width_expr(width)
        parts.append(f"hls_trunc<{safe_width}>({part})")
        part_widths.append(safe_width)
    if len(parts) == 1:
        return parts[0]
    # Vitis: ap_concat_ref has no .concat(). Materialize to ap_uint after each
    # pairwise concat so the next call is on a real ap_uint value.
    result = parts[0]
    acc_width = part_widths[0]
    for part, width in zip(parts[1:], part_widths[1:]):
        acc_width = _add_width_exprs(acc_width, width)
        result = (
            f"hls_trunc<{_safe_width_expr(acc_width)}>"
            f"(({result}).concat({part}))")
    return result


def _lower_braces(text, replacements, widths, as_parameter, array_names):
    index = 0
    output = []
    while index < len(text):
        if text[index] == "{":
            close = _matching_pair(text, index)
            output.append(_lower_concat_body(
                text[index + 1:close], replacements, widths, as_parameter,
                array_names))
            index = close + 1
        else:
            output.append(text[index])
            index += 1
    return "".join(output)


def _translate_expression(value, replacements, *, as_parameter=False,
                          widths=None, array_names=None, context_width=None):
    text = str(value).strip()
    if not text:
        return "0"
    array_names = array_names or set()
    if not as_parameter:
        text = _lower_braces(
            text, replacements, widths, as_parameter, array_names)
        text = _replace_dollar_call(text, "signed", "hls_as_signed")
        text = _replace_dollar_call(text, "unsigned", "hls_as_unsigned")
        text = _lower_reductions(text)
    text = re.sub(r"(?<![A-Za-z0-9_$])'0\b", "0", text)
    text = re.sub(r"(?<![A-Za-z0-9_$])'1\b", "-1", text)
    text = _VERILOG_LITERAL.sub(
        lambda match: _literal(match, sized=not as_parameter), text)
    text = text.replace("!==", "!=").replace("===", "==")
    text = text.replace(">>>", ">>").replace("<<<", "<<")
    text = text.replace("$clog2", "hls_clog2")
    text = _IDENTIFIER.sub(
        lambda match: replacements.get(match.group(0), match.group(0)), text)
    # ap_uint/ap_int provide range(high, low) for Verilog-style part selects.
    text = _SLICE.sub(
        lambda match: (f"{match.group(1)}.range({match.group(2).strip()}, "
                       f"{match.group(3).strip()})"), text)
    if not as_parameter:
        text = _wrap_bit_selects(text, array_names)
    if context_width:
        text = _cast_to_width(text, context_width)
    return text


def _hls_type(width, datatype="unsigned"):
    width_text = _safe_width_expr(width)
    signed = "signed" in str(datatype).lower() and "unsigned" not in str(
        datatype).lower()
    kind = "ap_int" if signed else "ap_uint"
    return f"{kind}<{width_text}>"


def _reset_condition(domain):
    if not domain.reset:
        return None
    return (f"!{domain.reset}" if domain.reset_polarity in {
            "active_low", "low"} else domain.reset)


def _memory_depth(model, state):
    if state.depth not in (None, ""):
        return state.depth
    for resource in model.resources:
        if str(resource.get("name", "")) != state.name:
            continue
        parameters = resource.get("parameters", {}) or {}
        return resource.get(
            "depth", parameters.get("depth", parameters.get("DEPTH")))
    raise HLSGenerationError(f"memory state {state.name!r} has no depth")


def _resource_kind(resource):
    return str(resource.get("type", "")).lower()


def _resource_is_memory(resource):
    return _resource_kind(resource) in _MEMORY_RESOURCE_TYPES


def _resource_is_annotation(resource):
    kind = _resource_kind(resource)
    if kind in _OPERATOR_RESOURCE_TYPES:
        return True
    implementation = str(resource.get("implementation", "")).lower()
    ports = resource.get("ports", []) or []
    if implementation in _ANNOTATION_IMPLEMENTATIONS and not ports:
        return True
    if kind in {"submodule", "module", "instance"} and not ports:
        return True
    return False


def _check_compatibility(model):
    errors = []
    if any(port.direction == "inout" for port in model.ports):
        errors.append("inout ports are unsupported in HLS output mode")
    unsupported_resources = [
        str(resource.get("name", "<unnamed>"))
        for resource in model.resources
        if not _resource_is_memory(resource)
        and not _resource_is_annotation(resource)
    ]
    if unsupported_resources:
        errors.append(f"unsupported instantiated resources: {unsupported_resources}")
    if errors:
        raise HLSGenerationError("; ".join(errors))


def _unique_clocks(model):
    clocks = []
    seen = set()
    for domain in model.clock_domains:
        if domain.clock and domain.clock not in seen:
            seen.add(domain.clock)
            clocks.append(domain.clock)
    return clocks


def _implicit_clock(model):
    return len(_unique_clocks(model)) <= 1


def _omit_clock_names(model):
    if _implicit_clock(model):
        return set(_unique_clocks(model))
    return set()


def _port_signature(model, omitted_clocks):
    fields = []
    for port in model.ports:
        if port.name in omitted_clocks:
            continue
        port_type = _hls_type(port.width, "signed" if port.signed else "unsigned")
        if port.direction == "input":
            fields.append(f"{port_type} {port.name}")
        else:
            fields.append(f"{port_type} &{port.name}")
    return fields


def _state_names(model):
    return {state.name: f"__state_{state.name}" for state in model.states}


def _derived_names(model):
    return {str(signal.get("name")): f"__derived_{signal.get('name')}"
            for signal in _iter_derived(model)}


def _array_names(model, replacements):
    return {replacements[state.name] for state in model.states
            if state.type in {"memory", "ram"} and state.name in replacements}


def _render_parameters(model, widths):
    width_tokens = set()
    for value in list(widths.values()) + [
            getattr(port, "width", None) for port in model.ports]:
        width_tokens.update(re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(value)))
    for signal in _iter_derived(model):
        width_tokens.update(
            re.findall(r"[A-Za-z_][A-Za-z0-9_]*", str(signal.get("width", ""))))
    # Expressions like N-1 / a[N-2:0] need N >= 2 to stay legal.
    needs_ge_two = set()
    corpus = []
    for signal in _iter_derived(model):
        corpus.append(str(signal.get("width", "")))
        corpus.append(str(signal.get("expression", "")))
    for port in model.ports:
        corpus.append(str(port.width))
    joined = "\n".join(corpus)
    for name in width_tokens:
        if re.search(rf"\b{re.escape(name)}\s*-\s*[12]\b", joined):
            needs_ge_two.add(name)

    lines = []
    for parameter in model.parameters:
        value = _translate_expression(
            parameter.value, {}, as_parameter=True, widths=widths)
        if (parameter.name in width_tokens
                and re.fullmatch(r"-?\d+", str(value).strip())):
            numeric = int(value)
            minimum = 2 if parameter.name in needs_ge_two else 1
            if numeric < minimum:
                value = str(minimum)
        lines.append(
            f"static constexpr int {parameter.name} = {value};")
    if lines:
        lines.append("")
    return lines


def _ir_timing_section(model):
    timing = model.raw.get("timing") if isinstance(model.raw, dict) else None
    return timing if isinstance(timing, dict) else {}


def _memory_read_latency(model, state):
    """Best-effort IR memory read latency; 0 means same-cycle / combo-style read."""
    timing = _ir_timing_section(model)
    for key in ("ram_access", "memory_read", "memory"):
        entry = timing.get(key)
        if isinstance(entry, dict) and entry.get("latency") is not None:
            try:
                return max(0, int(entry["latency"]))
            except (TypeError, ValueError):
                pass
    for resource in model.resources:
        if str(resource.get("name", "")) != state.name:
            continue
        parameters = resource.get("parameters", {}) or {}
        for key in ("read_latency", "latency", "READ_LATENCY"):
            if key in parameters:
                try:
                    return max(0, int(parameters[key]))
                except (TypeError, ValueError):
                    pass
            if isinstance(resource.get(key), (int, str)):
                try:
                    return max(0, int(resource[key]))
                except (TypeError, ValueError):
                    pass
    # Default: match RTL same-cycle array read semantics, not BRAM+1.
    return 0


def _has_async_reset(model):
    return any(
        domain.reset and domain.reset_type in {"asynchronous", "async"}
        for domain in model.clock_domains)


def _render_ir_timing_comments(model, implicit_clock):
    lines = [
        "    // IR timing contract encoded into HLS pragmas below:",
        "    //   - one top-function invocation == one active clock cycle",
        "    //   - multi-cycle IR pipelines are explicit static registers,",
        "    //     not extra HLS schedule stages",
    ]
    for domain in model.clock_domains:
        reset = domain.reset or "none"
        clock = ("implicit ap_clk" if implicit_clock and domain.clock in
                 _omit_clock_names(model) else domain.clock)
        lines.append(
            f"    // clock_domain {domain.name}: {domain.edge} {clock}, "
            f"reset={reset} ({domain.reset_type}/{domain.reset_polarity})")
    seq_latencies = sorted({
        int(t.latency) for t in model.transitions
        if t.transition_type == "sequential"
        and str(t.latency).lstrip("-").isdigit()
    })
    if seq_latencies:
        lines.append(
            f"    // sequential transition.latency values present: {seq_latencies}")
    timing = _ir_timing_section(model)
    if timing:
        compact = ", ".join(
            f"{key}={value}" for key, value in list(timing.items())[:6])
        lines.append(f"    // IR timing metadata: {compact}")
    return lines


def _render_schedule_pragmas(model):
    """Force Vitis to keep the one-cycle-per-invocation schedule."""
    lines = [
        "    // Lock schedule to IR cycle semantics (no tool-inserted flops).",
        "    #pragma HLS pipeline II=1",
        "    #pragma HLS latency min=0 max=0",
    ]
    # Heavy operators: prefer fabric / latency-0 so they stay combinational
    # inside the IR cycle; real multi-cycle work is already in IR states.
    resource_kinds = {
        _resource_kind(resource) for resource in model.resources
    }
    if resource_kinds & {"multiplier", "mul", "mult"}:
        lines.append(
            "    // IR multiplier resources are cycle-local; keep mul latency 0.")
        lines.append(
            "    #pragma HLS bind_op op=mul impl=fabric latency=0")
    if resource_kinds & {"divider", "div", "divider_unsigned", "divider_signed"}:
        lines.append(
            "    // IR divider resources are cycle-local; keep div latency 0.")
        lines.append(
            "    #pragma HLS bind_op op=udiv impl=fabric latency=0")
        lines.append(
            "    #pragma HLS bind_op op=sdiv impl=fabric latency=0")
    return lines


def _render_state_pragmas(model, replacements):
    """Per-state reset / dependence pragmas derived from IR clock domains."""
    lines = []
    domains = model.domain_map()
    for state in model.states:
        name = replacements[state.name]
        domain = domains.get(state.clock_domain)
        if state.type in {"memory", "ram"}:
            # Cross-invocation array updates must not inflate II.
            lines.append(
                f"    #pragma HLS dependence variable={name} inter false")
            continue
        if domain and domain.reset:
            # Map IR reset onto the static register. Async IR resets still
            # become per-cycle conditions in C; this keeps tool reset policy
            # aligned with the static variable rather than stripping it.
            lines.append(f"    #pragma HLS reset variable={name}")
        lines.append(
            f"    #pragma HLS dependence variable={name} inter false")
    return lines


def _memory_bind_pragma(model, state, name):
    latency = _memory_read_latency(model, state)
    if latency <= 0:
        # Same-cycle read model used by direct RTL array/mem access.
        return (
            f"    #pragma HLS bind_storage variable={name} "
            f"type=ram_1p impl=lutram latency=0")
    return (
        f"    #pragma HLS bind_storage variable={name} "
        f"type=ram_2p impl=bram latency={latency}")


def _render_state_declarations(model, replacements, widths, array_names):
    lines = []
    for state in model.states:
        state_type = _hls_type(state.width, state.datatype)
        name = replacements[state.name]
        if state.type in {"memory", "ram"}:
            depth = _translate_expression(
                _memory_depth(model, state), replacements, widths=widths,
                array_names=array_names)
            lines.append(f"    static {state_type} {name}[{depth}] = {{}};")
            lines.append(_memory_bind_pragma(model, state, name))
        else:
            initial = (state.initial_value if state.initial_value is not None
                       else state.reset_value)
            initial = _translate_expression(
                initial, replacements, widths=widths, array_names=array_names,
                context_width=_cpp_width(state.width))
            lines.append(f"    static {state_type} {name} = {initial};")
    return lines



def _render_derived_locals(model, replacements, derived_replacements, widths,
                           array_names):
    lines = []
    active_replacements = dict(replacements)
    for signal in _iter_derived(model):
        name = str(signal.get("name", ""))
        width = signal.get("width", 1)
        expression = _translate_expression(
            signal.get("expression", 0), active_replacements, widths=widths,
            array_names=array_names, context_width=_cpp_width(width))
        local = derived_replacements[name]
        lines.append(f"    {_hls_type(width)} {local} = {expression};")
        active_replacements[name] = local
        # Derived locals capture the pre-update register view so sequential
        # guards see current Q, not next.
    return lines, active_replacements


def _actions_for_target(transition, target, *, otherwise=False):
    actions = transition.else_actions if otherwise else transition.actions
    return [action for action in actions if action.base_target == target]


def _next_target(action_target, state_name):
    return f"__next_{state_name}" + action_target[len(state_name):]


def _render_scalar_updates(model, processes, replacements, widths, array_names):
    states = model.state_map()
    transitions = model.transition_map()
    targets = []
    for process in processes:
        for transition_name in process.transitions:
            transition = transitions[transition_name]
            if is_reset_transition(model, transition):
                continue
            for action in transition.actions + transition.else_actions:
                if (action.base_target in states and
                        states[action.base_target].type not in {"memory", "ram"} and
                        action.base_target not in targets):
                    targets.append(action.base_target)
    lines = []
    for target in targets:
        state = states[target]
        lines.append(
            f"        {_hls_type(state.width, state.datatype)} "
            f"__next_{target} = {replacements[target]};")
    if lines:
        lines.append("")
    for process in processes:
        process_targets = []
        for name in process.transitions:
            transition = transitions[name]
            for action in transition.actions:
                target = action.base_target
                if (target in states and states[target].type not in {
                        "memory", "ram"} and target not in process_targets):
                    process_targets.append(target)
        for target in process_targets:
            dest_width = _cpp_width(states[target].width)
            names = ordered_for_target(model, process, target)
            for index, name in enumerate(names):
                transition = transitions[name]
                keyword = "if" if index == 0 else "else if"
                guard = _translate_expression(
                    transition.guard, replacements, widths=widths,
                    array_names=array_names)
                lines.append(f"        {keyword} ({guard}) {{")
                for action in _actions_for_target(transition, target):
                    rhs = _translate_expression(
                        action.expression, replacements, widths=widths,
                        array_names=array_names, context_width=dest_width)
                    lines.append(
                        f"            {_next_target(action.target, target)} = {rhs};")
                lines.append("        }")
                else_actions = _actions_for_target(
                    transition, target, otherwise=True)
                if else_actions:
                    lines.append("        else {")
                    for action in else_actions:
                        rhs = _translate_expression(
                            action.expression, replacements, widths=widths,
                            array_names=array_names, context_width=dest_width)
                        lines.append(
                            f"            {_next_target(action.target, target)} = {rhs};")
                    lines.append("        }")
            if names:
                lines.append("")
    for target in targets:
        lines.append(f"        {replacements[target]} = __next_{target};")
    return lines


def _render_memory_updates(model, processes, replacements, widths, array_names):
    states = model.state_map()
    transitions = model.transition_map()
    lines = []
    for process in processes:
        targets = []
        for name in process.transitions:
            for action in transitions[name].actions:
                target = action.base_target
                if (target in states and states[target].type in {"memory", "ram"}
                        and target not in targets):
                    targets.append(target)
        for target in targets:
            dest_width = _cpp_width(states[target].width)
            for index, name in enumerate(ordered_for_target(
                    model, process, target)):
                transition = transitions[name]
                keyword = "if" if index == 0 else "else if"
                guard = _translate_expression(
                    transition.guard, replacements, widths=widths,
                    array_names=array_names)
                lines.append(f"        {keyword} ({guard}) {{")
                for action in _actions_for_target(transition, target):
                    lhs = _translate_expression(
                        action.target, replacements, widths=widths,
                        array_names=array_names)
                    rhs = _translate_expression(
                        action.expression, replacements, widths=widths,
                        array_names=array_names, context_width=dest_width)
                    lines.append(f"            {lhs} = {rhs};")
                lines.append("        }")
            if targets:
                lines.append("")
    return lines


def _domain_state_names(model, domain):
    names = [name for name in domain.states if name]
    if names:
        return names
    return [state.name for state in model.states
            if state.clock_domain == domain.name]


def _domain_processes(processes, domain):
    return [process for process in processes
            if process.clock_domain == domain.name]


def _render_one_domain(model, domain, processes, replacements, widths,
                       implicit_clock, array_names):
    domain_processes = _domain_processes(processes, domain)
    owned = set(_domain_state_names(model, domain))
    scalar_states = [
        state for state in model.states
        if state.name in owned and state.type not in {"memory", "ram"}
    ]
    updates = _render_scalar_updates(
        model, domain_processes, replacements, widths, array_names)
    updates.extend(_render_memory_updates(
        model, domain_processes, replacements, widths, array_names))
    reset = _reset_condition(domain)
    tick = None
    if not implicit_clock and domain.clock:
        suffix = "fall" if domain.edge == "negedge" else "rise"
        tick = f"__{suffix}_{domain.clock}"

    lines = []
    if reset and tick:
        lines.append(f"    if ({reset}) {{")
        for state in scalar_states:
            value = _translate_expression(
                state.reset_value, replacements, widths=widths,
                array_names=array_names, context_width=_cpp_width(state.width))
            lines.append(f"        {replacements[state.name]} = {value};")
        lines.append(f"    }} else if ({tick}) {{")
        lines.extend(updates)
        lines.append("    }")
    elif reset:
        lines.append(f"    if ({reset}) {{")
        for state in scalar_states:
            value = _translate_expression(
                state.reset_value, replacements, widths=widths,
                array_names=array_names, context_width=_cpp_width(state.width))
            lines.append(f"        {replacements[state.name]} = {value};")
        lines.append("    } else {")
        lines.extend(updates)
        lines.append("    }")
    elif tick:
        lines.append(f"    if ({tick}) {{")
        lines.extend(updates)
        lines.append("    }")
    else:
        lines.extend(line[4:] if line.startswith("    ") else line
                     for line in updates)
    if lines:
        lines.append("")
    return lines


def _render_sequential_logic(model, processes, replacements, widths,
                             implicit_clock, array_names):
    lines = []
    clocks = _unique_clocks(model)
    if not implicit_clock:
        for clock in clocks:
            lines.append(f"    static ap_uint<1> __prev_{clock} = 0;")
        for clock in clocks:
            lines.append(
                f"    const bool __rise_{clock} = ({clock}) && (!__prev_{clock});")
            lines.append(
                f"    const bool __fall_{clock} = (!{clock}) && __prev_{clock};")
        for clock in clocks:
            lines.append(f"    __prev_{clock} = {clock};")
        if clocks:
            lines.append("")

    if not model.clock_domains:
        updates = _render_scalar_updates(
            model, processes, replacements, widths, array_names)
        updates.extend(_render_memory_updates(
            model, processes, replacements, widths, array_names))
        lines.append("")
        lines.extend(line[4:] if line.startswith("    ") else line
                     for line in updates)
        return lines

    ranked = sorted(
        model.clock_domains,
        key=lambda domain: 0 if domain.edge == "posedge" else 1)
    lines.append("")
    for domain in ranked:
        lines.extend(_render_one_domain(
            model, domain, processes, replacements, widths, implicit_clock,
            array_names))
    return lines


def _render_combinational_outputs(model, replacements, widths, array_names):
    lines = []
    ports = model.port_map()
    output_names = {port.name for port in model.ports
                    if port.direction == "output"}
    for transition in model.transitions:
        if transition.transition_type != "combinational":
            continue
        else_by_target = {
            action.base_target: action for action in transition.else_actions}
        for action in transition.actions:
            if action.base_target not in output_names:
                continue
            dest_width = _cpp_width(ports[action.base_target].width)
            value = _translate_expression(
                action.expression, replacements, widths=widths,
                array_names=array_names, context_width=dest_width)
            if transition.guard not in {"1", "1'b1", "true", "True"}:
                guard = _translate_expression(
                    transition.guard, replacements, widths=widths,
                    array_names=array_names)
                otherwise = else_by_target.get(action.base_target)
                if otherwise is None:
                    raise HLSGenerationError(
                        f"combinational output {action.base_target!r} has no else action")
                fallback = _translate_expression(
                    otherwise.expression, replacements, widths=widths,
                    array_names=array_names, context_width=dest_width)
                value = f"hls_mux<{dest_width}>({guard}, {value}, {fallback})"
            lines.append(f"    {action.target} = {value};")
    return lines


def _render_outputs(model, replacements, widths, array_names):
    """Drive output ports from the current register/memory/derived view (Q).

    Must run before sequential updates. Assigning after ``state = next`` makes
    Vitis treat the port as combinational next-state logic instead of the flop.

    ``replacements`` must include derived locals (``__derived_*``) so port
    assignments never refer to undeclared bare IR names.
    """
    lines = []
    ports = model.port_map()
    for state in model.states:
        port = ports.get(state.name)
        if port and port.direction == "output":
            lines.append(f"    {state.name} = {replacements[state.name]};")
    for signal in _iter_derived(model):
        name = str(signal.get("name", ""))
        if name in ports and ports[name].direction == "output":
            # Use the already-lowered local; do not re-translate the IR
            # expression with a state-only map (that drops __derived_* names).
            local = replacements.get(name)
            if local:
                lines.append(f"    {name} = {local};")
            else:
                dest_width = _cpp_width(signal.get("width", ports[name].width))
                expression = _translate_expression(
                    signal.get("expression", 0), replacements, widths=widths,
                    array_names=array_names, context_width=dest_width)
                lines.append(f"    {name} = {expression};")
    memory_states = {state.name for state in model.states
                     if state.type in {"memory", "ram"}}
    for resource in model.resources:
        name = str(resource.get("name", ""))
        if name not in memory_states:
            continue
        resource_ports = resource.get("ports", {}) or {}
        read = resource_ports.get("read") if isinstance(
            resource_ports, dict) else None
        if not isinstance(read, dict):
            continue
        address = read.get("address")
        data = read.get("data")
        if data in ports and ports[data].direction == "output" and address:
            # Skip if this output was already driven as a derived signal.
            if any(str(signal.get("name", "")) == data
                   for signal in _iter_derived(model)):
                continue
            address_expr = _translate_expression(
                address, replacements, widths=widths, array_names=array_names)
            dest_width = _cpp_width(ports[data].width)
            lines.append(
                f"    {data} = hls_trunc<{dest_width}>("
                f"{replacements[name]}[{address_expr}]);")
    return lines


_HLS_HELPERS = """#include <ap_int.h>

namespace {
constexpr int hls_clog2(int value) {
    int result = 0;
    for (int current = value - 1; current > 0; current >>= 1) ++result;
    return result;
}

constexpr int hls_width(int value) {
    return value > 0 ? value : 1;
}

template<int W, typename T>
inline ap_uint<W> hls_trunc(T value) {
    return ap_uint<W>(value);
}

template<int W, typename C, typename A, typename B>
inline ap_uint<W> hls_mux(C cond, A a, B b) {
    return cond ? ap_uint<W>(a) : ap_uint<W>(b);
}

template<int N, int W>
inline ap_uint<(N > 0 ? N * W : W)> hls_replicate(ap_uint<W> value) {
    ap_uint<(N > 0 ? N * W : W)> result = 0;
    for (int i = 0; i < (N > 0 ? N : 0); ++i) {
        result.range((i + 1) * W - 1, i * W) = value;
    }
    return result;
}

template<int W>
inline ap_int<W> hls_as_signed(ap_uint<W> value) {
    return ap_int<W>(value);
}

template<int W>
inline ap_int<W> hls_as_signed(ap_int<W> value) {
    return value;
}

template<int W>
inline ap_uint<W> hls_as_unsigned(ap_uint<W> value) {
    return value;
}

template<int W>
inline ap_uint<W> hls_as_unsigned(ap_int<W> value) {
    return ap_uint<W>(value);
}
}
"""


def generate_hls_c(ir_path, output_path, report_path=None):
    """Generate a stateful, one-invocation-per-cycle HLS C++ top function.

    Registered outputs are driven from current static state (Q) *before*
    ``state = next``, so Vitis keeps the port on the flop rather than
    combinational next-state logic.
    """
    source = Path(ir_path).resolve()
    model = parse_ir(source)
    _check_compatibility(model)
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)

    omitted_clocks = _omit_clock_names(model)
    implicit_clock = _implicit_clock(model)
    widths = _width_map(model)
    state_replacements = _state_names(model)
    derived_replacements = _derived_names(model)
    replacements = dict(state_replacements)
    array_names = _array_names(model, replacements)
    processes = build_processes(model)
    signature = _port_signature(model, omitted_clocks)
    lines = _HLS_HELPERS.splitlines()
    lines.append("")
    lines.extend(_render_parameters(model, widths))
    lines.append(f"void {model.module_name}(")
    for index, field in enumerate(signature):
        comma = "," if index + 1 < len(signature) else ""
        lines.append(f"    {field}{comma}")
    lines.extend([
        ") {",
        "    #pragma HLS interface ap_ctrl_none port=return",
    ])
    for port in model.ports:
        if port.name in omitted_clocks:
            continue
        lines.append(f"    #pragma HLS interface ap_none port={port.name}")
    lines.extend(_render_ir_timing_comments(model, implicit_clock))
    lines.extend(_render_schedule_pragmas(model))
    lines.append("    // One invocation models one active clock cycle.")
    lines.append(
        "    // Emit registered outputs from Q before state<=next updates.")
    if not implicit_clock:
        lines.append(
            "    // Distinct clocks are sampled with stored previous values.")
    elif any(domain.edge == "negedge" for domain in model.clock_domains):
        lines.append(
            "    // Dual-edge domains on one clock: posedge then negedge.")
    lines.extend(_render_state_declarations(
        model, replacements, widths, array_names))
    lines.extend(_render_state_pragmas(model, replacements))
    derived_lines, expression_replacements = _render_derived_locals(
        model, replacements, derived_replacements, widths, array_names)
    if derived_lines:
        lines.append("")
        lines.extend(derived_lines)

    # Cycle-accurate contract: ports sample current Q / combo-of-Q, then the
    # static registers advance for the next invocation (next clock cycle).
    # Pass expression_replacements so derived names stay as __derived_*.
    output_lines = _render_outputs(
        model, expression_replacements, widths, array_names)
    combo_lines = _render_combinational_outputs(
        model, expression_replacements, widths, array_names)
    if output_lines or combo_lines:
        lines.append("")
        lines.extend(output_lines)
        lines.extend(combo_lines)
    lines.extend(_render_sequential_logic(
        model, processes, expression_replacements, widths, implicit_clock,
        array_names))
    lines.extend(["}", ""])
    output.write_text("\n".join(lines), encoding="utf-8")

    warnings = []
    unique_clocks = _unique_clocks(model)
    if len(unique_clocks) > 1:
        warnings.append(
            "multiple clocks are mapped with per-clock edge detection; "
            "the caller must supply toggling clock ports")
    if any(domain.edge == "negedge" for domain in model.clock_domains
           ) and implicit_clock:
        warnings.append(
            "negedge logic on the implicit clock is applied after posedge "
            "logic in the same top-function invocation")
    for domain in model.clock_domains:
        if domain.reset and domain.reset_type in {"asynchronous", "async"}:
            warnings.append(
                "asynchronous reset is represented as a reset condition "
                "evaluated once per HLS top-function invocation")
            break
    if model.cdc:
        warnings.append(
            "CDC records are treated as documentation; synchronizer state "
            "already present in HW-IR is emitted as ordinary registers")
    annotated = [
        str(resource.get("name", "<unnamed>"))
        for resource in model.resources
        if _resource_is_annotation(resource)
    ]
    if annotated:
        warnings.append(
            "operator/submodule resources were treated as annotations: "
            f"{annotated}")

    report = {
        "status": "ok", "generator": "deterministic_hls_c",
        "created_at": datetime.now(timezone.utc).isoformat(),
        "input_ir": str(source), "output_hls_c": str(output),
        "module": model.module_name,
        "execution_model": (
            "edge_detected_clock_ports" if not implicit_clock else
            "one_top_function_invocation_per_active_clock_cycle"),
        "output_timing": "registered_ports_sample_q_before_state_update",
        "hls_pragmas": {
            "pipeline_ii": 1,
            "latency": {"min": 0, "max": 0},
            "interface": "ap_ctrl_none + ap_none",
            "state_reset_pragmas": any(
                domain.reset for domain in model.clock_domains),
            "memory_bind": "lutram latency=0 by default; bram if IR read latency>0",
        },
        "dialect": "Vitis-style C++ with ap_uint/ap_int",
        "clock_ports_omitted": sorted(omitted_clocks),
        "clock_ports_sampled": ([] if implicit_clock else unique_clocks),
        "counts": {
            "ports": len(signature), "states": len(model.states),
            "processes": len(processes), "transitions": len(model.transitions),
            "memories": sum(state.type in {"memory", "ram"}
                            for state in model.states),
        },
        "warnings": warnings,
    }
    if report_path:
        report_output = Path(report_path).resolve()
        report_output.parent.mkdir(parents=True, exist_ok=True)
        report_output.write_text(
            json.dumps(report, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8")
    return output, report
