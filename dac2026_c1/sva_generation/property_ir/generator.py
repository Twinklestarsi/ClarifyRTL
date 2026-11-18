"""Deterministically extract Temporal Property IR from normalized HW-IR."""

import re
from pathlib import Path

import yaml

from Compiler.rtl_compiler.lowering import is_reset_transition
from Compiler.rtl_compiler.parser import parse_ir


_IDENTIFIER = re.compile(r"(?<!['$])\b[A-Za-z_][A-Za-z0-9_$]*\b")
_TRUE = {"1", "1'b1", "true"}


def _identifier(value):
    text = re.sub(r"[^A-Za-z0-9_$]+", "_", str(value)).strip("_")
    if not text or not re.match(r"[A-Za-z_]", text):
        text = "property_" + text
    return text


def _unique_name(base, used):
    base = _identifier(base)
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _balanced_outer_parentheses(expression):
    text = expression.strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        encloses_all = True
        for index, char in enumerate(text):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(text) - 1:
                    encloses_all = False
                    break
        if depth or not encloses_all:
            break
        text = text[1:-1].strip()
    return text


def _ternary_parts(expression):
    """Return a top-level Verilog ternary as condition/true/false, if present."""
    text = _balanced_outer_parentheses(expression)
    depths = {"(": 0, "[": 0, "{": 0}
    closing = {")": "(", "]": "[", "}": "{"}
    question = None
    nested = 0
    for index, char in enumerate(text):
        if char in depths:
            depths[char] += 1
            continue
        if char in closing:
            key = closing[char]
            depths[key] = max(0, depths[key] - 1)
            continue
        if any(depths.values()):
            continue
        if char == "?":
            if question is None:
                question = index
            else:
                nested += 1
        elif char == ":" and question is not None:
            if nested:
                nested -= 1
            else:
                return (text[:question].strip(),
                        text[question + 1:index].strip(),
                        text[index + 1:].strip())
    return None


def _expression_paths(expression, conditions=()):
    parts = _ternary_parts(expression)
    if not parts:
        return [(list(conditions), _balanced_outer_parentheses(expression))]
    condition, true_expression, false_expression = parts
    return (_expression_paths(true_expression, conditions + ((condition, True),)) +
            _expression_paths(false_expression, conditions + ((condition, False),)))


def _negate(expression):
    text = _balanced_outer_parentheses(expression)
    if re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", text):
        return "!" + text
    if text.startswith("!") and re.fullmatch(
            r"![A-Za-z_][A-Za-z0-9_$]*", text):
        return text[1:]
    return f"!({text})"


def _and_expression(expressions):
    parts = []
    for expression in expressions:
        text = str(expression).strip()
        if not text or text.lower() in _TRUE:
            continue
        if re.fullmatch(r"!?[A-Za-z_][A-Za-z0-9_$]*", text):
            parts.append(text)
        else:
            parts.append(f"({text})")
    return " && ".join(parts) or "1'b1"


def _top_level_and_parts(expression):
    text = _balanced_outer_parentheses(str(expression))
    depths = {"(": 0, "[": 0, "{": 0}
    closing = {")": "(", "]": "[", "}": "{"}
    parts = []
    start = 0
    index = 0
    while index < len(text):
        char = text[index]
        if char in depths:
            depths[char] += 1
        elif char in closing:
            key = closing[char]
            depths[key] = max(0, depths[key] - 1)
        elif (char == "&" and index + 1 < len(text) and
              text[index + 1] == "&" and not any(depths.values())):
            parts.append(text[start:index].strip())
            start = index + 2
            index += 1
        index += 1
    parts.append(text[start:].strip())
    return parts


def _reset_assertion(domain):
    if not domain.reset:
        return None
    if domain.reset_polarity in {"active_low", "low"}:
        return "!" + domain.reset
    return domain.reset


def _reset_condition_role(expression, domain):
    if not domain or not domain.reset:
        return None
    text = re.sub(
        r"\s+", "", _balanced_outer_parentheses(str(expression))).lower()
    low = {
        f"!{domain.reset.lower()}", f"~{domain.reset.lower()}",
        f"{domain.reset.lower()}==0", f"{domain.reset.lower()}==1'b0",
        f"{domain.reset.lower()}===1'b0", f"0=={domain.reset.lower()}",
        f"1'b0=={domain.reset.lower()}", f"1'b0==={domain.reset.lower()}",
    }
    high = {
        domain.reset.lower(), f"{domain.reset.lower()}==1",
        f"{domain.reset.lower()}==1'b1", f"{domain.reset.lower()}===1'b1",
        f"1=={domain.reset.lower()}", f"1'b1=={domain.reset.lower()}",
        f"1'b1==={domain.reset.lower()}",
    }
    asserted = low if domain.reset_polarity in {"active_low", "low"} else high
    deasserted = high if domain.reset_polarity in {"active_low", "low"} else low
    if text in asserted:
        return "asserted"
    if text in deasserted:
        return "deasserted"
    return None


def _is_reset_transition(model, transition):
    if is_reset_transition(model, transition):
        return True
    if transition.transition_type != "sequential":
        return False
    domain = model.domain_map().get(transition.clock_domain)
    return _reset_condition_role(transition.guard, domain) == "asserted"


def _normal_operation_guard(expression, domain):
    parts = _top_level_and_parts(expression)
    remaining = [part for part in parts
                 if _reset_condition_role(part, domain) != "deasserted"]
    return _and_expression(remaining)


def _scope(*, transitions=(), states=(), signals=(), cdc=()):
    return {
        "related_transitions": list(dict.fromkeys(transitions)),
        "related_states": list(dict.fromkeys(states)),
        "related_signals": list(dict.fromkeys(signals)),
        "related_cdc": list(dict.fromkeys(cdc)),
    }


def _measurement():
    return {
        "generate_assert": True,
        "generate_cover": False,
        "generate_delay_covers": False,
    }


def _referenced_signals(model, *expressions):
    signal_names = ({port.name for port in model.ports} |
                    {str(item.get("name", "")) for item in model.derived_signals})
    state_names = set(model.state_map())
    referenced = set()
    for expression in expressions:
        referenced.update(_IDENTIFIER.findall(str(expression)))
    return sorted((referenced & signal_names) - state_names)


def _transition_writes(transition, state_name):
    return any(action.base_target == state_name
               for action in transition.actions + transition.else_actions)


def _reset_properties(model, used_names):
    properties = []
    reset_transitions = [transition for transition in model.transitions
                         if _is_reset_transition(model, transition)]
    for state in model.states:
        domain = model.domain_map().get(state.clock_domain)
        if not domain or not domain.reset or state.type in {"memory", "ram"}:
            continue
        related = [transition.name for transition in reset_transitions
                   if transition.clock_domain == domain.name and
                   _transition_writes(transition, state.name)]
        assertion = _reset_assertion(domain)
        properties.append({
            "name": _unique_name(f"{state.name}_reset", used_names),
            "category": "reset",
            "scope": _scope(transitions=related, states=[state.name],
                            signals=[domain.reset]),
            "clock": {"domain": domain.name},
            "trigger": {"expression": assertion, "signal": None,
                        "event": "level"},
            "temporal": {
                "relation": "next_cycle",
                "delay": {"min": 1, "max": 1, "unit": "cycle",
                          "clock_domain": domain.name},
            },
            "effect": {"type": "condition", "target": state.name,
                       "expression": state.reset_value,
                       "reference_time": "current"},
            "measurement": _measurement(),
        })
    return properties


def _effect_for_path(action, expression):
    target = action.target
    base_target = action.base_target
    compact = re.sub(r"\s+", "", expression)
    if compact in {re.sub(r"\s+", "", target), base_target}:
        return "stable", {
            "type": "stable", "target": target,
            "expression": None, "reference_time": "previous"}
    # Every sequential RHS is sampled on the triggering edge.  This matters
    # for input data as well as self-referential expressions.  Indexed lvalues
    # also need their address sampled at the trigger edge (for example a RAM
    # write must check mem[$past(addr)] == $past(data), not next-cycle addr).
    effect = {
        "type": "state_update", "target": target,
        "expression": expression, "reference_time": "trigger"}
    if "[" in target:
        effect["target_reference_time"] = "trigger"
    return "state_update", effect


def _action_properties(model, used_names):
    properties = []
    for transition in model.transitions:
        if transition.transition_type != "sequential" or _is_reset_transition(
                model, transition):
            continue
        domain = model.domain_map()[transition.clock_domain]
        disable = _reset_assertion(domain)
        normal_guard = (_normal_operation_guard(transition.guard, domain)
                        if disable else transition.guard)
        actions = [(action, normal_guard) for action in transition.actions]
        actions.extend((action, _negate(normal_guard))
                       for action in transition.else_actions)
        for action in actions:
            assignment, base_guard = action
            paths = _expression_paths(assignment.expression)
            for path_index, (conditions, expression) in enumerate(paths, 1):
                path_conditions = [condition if selected else _negate(condition)
                                   for condition, selected in conditions]
                trigger_expression = _and_expression(
                    [base_guard] + path_conditions)
                category, effect = _effect_for_path(assignment, expression)
                suffix = f"_path_{path_index}" if len(paths) > 1 else ""
                prop = {
                    "name": _unique_name(
                        f"{transition.name}_{assignment.base_target}_{category}{suffix}",
                        used_names),
                    "category": category,
                    "scope": _scope(
                        transitions=[transition.name],
                        states=[assignment.base_target],
                        signals=_referenced_signals(
                            model, trigger_expression, expression)),
                    "clock": {"domain": transition.clock_domain},
                    "trigger": {"expression": trigger_expression,
                                "signal": None, "event": "level"},
                    "temporal": {
                        "relation": "stable" if category == "stable" else "next_cycle",
                        "delay": {"min": 1, "max": 1, "unit": "cycle",
                                  "clock_domain": transition.clock_domain},
                    },
                    "effect": effect,
                    "measurement": _measurement(),
                }
                if disable:
                    prop["disable"] = {"expression": disable}
                properties.append(prop)
    return properties


def _priority_properties(model, used_names):
    result = []
    transitions = model.transition_map()
    for higher, lower in model.priorities:
        high = transitions.get(higher)
        low = transitions.get(lower)
        if not high or not low or not high.clock_domain:
            continue
        # A clock-domain reset branch already has per-state reset properties
        # and is implemented outside normal transition selection.  Treating
        # reset metadata as an ordinary priority relation creates a false
        # assertion during every deasserted-reset cycle.
        if _is_reset_transition(model, high) or _is_reset_transition(model, low):
            continue
        result.append({
            "name": _unique_name(f"{higher}_over_{lower}_priority", used_names),
            "category": "priority",
            "scope": _scope(transitions=[higher, lower]),
            "clock": {"domain": high.clock_domain},
            "priority": {"higher": higher, "lower": lower},
            "trigger": {"expression": high.guard, "signal": None,
                        "event": "level"},
            "effect": {"selected_transition": higher},
            "measurement": _measurement(),
        })
    return result


def _cdc_name(item, index):
    if item.get("name"):
        return _identifier(item["name"])
    source = item.get("source", item.get("source_signal", f"source_{index}"))
    destination = item.get(
        "destination", item.get("destination_signal", f"destination_{index}"))
    if isinstance(source, dict):
        source = source.get("signal", source.get("name", f"source_{index}"))
    if isinstance(destination, dict):
        destination = destination.get(
            "signal", destination.get("name", destination.get("domain",
                                                               f"destination_{index}")))
    return _identifier(f"{source}_to_{destination}")


def cdc_reference_names(model):
    return [_cdc_name(item, index) for index, item in enumerate(model.cdc, 1)]


def _cdc_properties(model, used_names):
    result = []
    state_map = model.state_map()
    domains = model.domain_map()
    signal_names = ({port.name for port in model.ports} |
                    {str(signal.get("name", ""))
                     for signal in model.derived_signals})
    for index, item in enumerate(model.cdc, 1):
        reference = _cdc_name(item, index)
        source = item.get("source", item.get("source_signal"))
        source_domain = item.get("source_clock_domain", item.get("source_domain"))
        if isinstance(source, dict):
            source_domain = source.get("clock_domain", source.get("domain",
                                                                  source_domain))
            source = source.get("signal", source.get("name"))
        destination = item.get("destination", item.get("destination_signal"))
        destination_domain = item.get(
            "destination_clock_domain", item.get("destination_domain"))
        destination_signal = item.get("destination_signal")
        if isinstance(destination, dict):
            destination_domain = destination.get(
                "clock_domain", destination.get("domain", destination_domain))
            destination_signal = destination.get(
                "signal", destination.get("name", destination_signal))
        elif destination in domains:
            destination_domain = destination
        sync = item.get("synchronizer", {}) or {}
        registers = sync.get("registers", []) or []
        if isinstance(registers, str):
            registers = [registers]
        if not destination_signal and registers:
            destination_signal = registers[-1]
        if not destination_domain and destination_signal in state_map:
            destination_domain = state_map[destination_signal].clock_domain
        if not source_domain and source in state_map:
            source_domain = state_map[source].clock_domain
        stages = sync.get("stages", len(registers) or 1)
        try:
            stages = max(1, int(stages))
        except (TypeError, ValueError):
            stages = max(1, len(registers))
        latency = sync.get("latency", stages)
        try:
            minimum = max(1, int(latency))
        except (TypeError, ValueError):
            minimum = stages
        cdc_body = {
            "source_clock_domain": source_domain,
            "destination_clock_domain": destination_domain,
            "source_signal": source,
            "destination_signal": destination_signal,
            "synchronization": {"type": "multi_flop", "stages": stages},
            "delay": {"min": minimum, "max": minimum + 1,
                      "unit": "destination_cycle"},
        }
        encoding = item.get("encoding", item.get("data_property"))
        if encoding:
            cdc_body["data_property"] = (encoding if isinstance(encoding, dict)
                                         else {"type": encoding})
        result.append({
            "name": _unique_name(f"{reference}_cdc", used_names),
            "category": "cdc",
            "scope": _scope(states=registers,
                            signals=[value for value in (source, destination_signal)
                                     if value in signal_names], cdc=[reference]),
            "cdc": cdc_body,
            "measurement": _measurement(),
        })
    return result


def _infer_domain(domain_map, state_map, dependencies):
    """Infer the most common clock domain from a set of dependency states."""
    domain_counts = {}
    for dep in dependencies:
        state = state_map.get(dep)
        if state and state.clock_domain:
            domain_counts[state.clock_domain] = domain_counts.get(
                state.clock_domain, 0) + 1
    if domain_counts:
        most_common = max(domain_counts, key=domain_counts.get)
        return domain_map.get(most_common)
    # Fall back to the first available domain
    for domain in domain_map.values():
        return domain
    return None


def _derived_signal_properties(model, used_names):
    properties = []
    domain_map = model.domain_map()
    state_map = model.state_map()
    # Pre-check: need at least one clock domain
    if not domain_map:
        return properties
    for signal in model.derived_signals:
        name = signal.get("name", "")
        expression = signal.get("expression", "")
        dependencies = signal.get("dependencies", [])
        if not name or not expression:
            continue
        # Infer clock domain from dependencies
        domain = _infer_domain(domain_map, state_map, dependencies)
        if not domain:
            continue
        disable = _reset_assertion(domain)
        prop = {
            "name": _unique_name(f"{name}_definition", used_names),
            "category": "state_invariant",
            "scope": _scope(
                signals=[name] + [d for d in dependencies
                                   if d not in state_map],
                states=[d for d in dependencies if d in state_map],
            ),
            "clock": {"domain": domain.name},
            "invariant": {"expression": f"({name}) == ({expression})"},
            "measurement": _measurement(),
        }
        if disable:
            prop["disable"] = {"expression": disable}
        properties.append(prop)
    return properties


def build_property_ir(model):
    """Build a serializable Property IR document from a parsed HW-IR model."""
    used_names = set()
    properties = []
    properties.extend(_reset_properties(model, used_names))
    properties.extend(_action_properties(model, used_names))
    properties.extend(_priority_properties(model, used_names))
    properties.extend(_cdc_properties(model, used_names))
    properties.extend(_derived_signal_properties(model, used_names))
    return {"sva_properties": properties}


def generate_property_ir(hw_ir_path, output_path):
    """Generate ``temporal_property_ir.yaml`` from an existing normalized HW-IR."""
    model = parse_ir(hw_ir_path)
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(yaml.safe_dump(
        build_property_ir(model), allow_unicode=True, sort_keys=False,
        default_flow_style=False), encoding="utf-8")
    return output
