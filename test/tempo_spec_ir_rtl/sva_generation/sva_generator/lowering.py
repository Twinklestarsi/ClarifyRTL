"""Lower checked Temporal Property IR into an SVA backend AST."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from Compiler.rtl_compiler.parser import parse_ir

from .emitter import COMPATIBILITY_PROFILES, emit_sva
from .model import (ClockEvent, DirectBody, GenerateLoop, ImplicationBody,
                    PropertyAST, RawBody, SVAProgram, SequenceAST)


class SVALoweringError(RuntimeError):
    pass


_PROPERTY_ONLY_SYNTAX = re.compile(
    r"\|[-=]>|##|\[\*|\[->|\[=|\b(?:until|until_with|throughout|within|"
    r"intersect|first_match|s_until|s_until_with|implies|iff)\b")


def _identifier(value):
    result = re.sub(r"[^A-Za-z0-9_$]+", "_", str(value)).strip("_")
    if not result or not re.match(r"[A-Za-z_]", result):
        result = "property_" + result
    return result


def _unique_name(base, used):
    base = _identifier(base)
    candidate = base
    suffix = 2
    while candidate in used:
        candidate = f"{base}_{suffix}"
        suffix += 1
    used.add(candidate)
    return candidate


def _measurement(prop):
    value = prop.get("measurement", {}) or {}
    return {
        "assert": bool(value.get("generate_assert", True)),
        "cover": bool(value.get("generate_cover", False)),
        "delay_covers": bool(value.get("generate_delay_covers", False)),
    }


def _clock_for_domain(model, domain_name):
    domain = model.domain_map().get(str(domain_name or ""))
    if not domain:
        raise SVALoweringError(f"unknown clock domain {domain_name!r}")
    return ClockEvent(domain.edge, domain.clock)


def _property_clock(model, prop):
    if prop.get("category") == "cdc":
        cdc = prop.get("cdc", {}) or {}
        return _clock_for_domain(model, cdc.get("destination_clock_domain"))
    clock = prop.get("clock", {}) or {}
    return _clock_for_domain(model, clock.get("domain"))


def _disable(prop):
    value = prop.get("disable")
    return str(value.get("expression")) if isinstance(value, dict) and value.get(
        "expression") is not None else None


def _trigger(prop):
    trigger = prop.get("trigger", {}) or {}
    expression = trigger.get("expression")
    signal = trigger.get("signal")
    event = trigger.get("event", "level")
    if event == "level":
        value = expression if expression is not None else signal
        return str(value) if value is not None else "1'b1"
    if not signal:
        raise SVALoweringError(
            f"property {prop.get('name')}: event {event!r} requires trigger.signal")
    functions = {
        "rose": "$rose", "fell": "$fell", "changed": "$changed",
        "stable": "$stable",
    }
    if event not in functions:
        raise SVALoweringError(
            f"property {prop.get('name')}: unsupported trigger event {event!r}")
    event_expression = f"{functions[event]}({signal})"
    return (f"({expression}) && {event_expression}"
            if expression is not None else event_expression)


def _interval(value, default):
    value = value if isinstance(value, dict) else {}
    minimum = value.get("min", default[0])
    maximum = value.get("max", default[1])
    minimum = default[0] if minimum is None else int(minimum)
    maximum = minimum if maximum is None else int(maximum)
    return minimum, maximum


def _delay(prop):
    category = prop.get("category")
    if category == "cdc":
        return _interval((prop.get("cdc", {}) or {}).get("delay"), (1, 1))
    temporal = prop.get("temporal", {}) or {}
    relation = temporal.get("relation")
    defaults = {
        "same_cycle": (0, 0), "next_cycle": (1, 1), "stable": (1, 1),
    }
    default = defaults.get(relation, (0, 0))
    return _interval(temporal.get("delay"), default)


def _delay_prefix(minimum, maximum):
    if minimum == 0 and maximum == 0:
        return ""
    if minimum == maximum:
        return f"##{minimum} "
    return f"##[{minimum}:{maximum}] "


def _implication(antecedent, consequent, minimum, maximum):
    return ImplicationBody(str(antecedent), str(consequent), minimum, maximum)


def _past(expression, cycles):
    expression = str(expression).strip()
    if cycles <= 0:
        return expression
    if re.fullmatch(
            r"'(?:0|1|[xXzZ])|(?:\d+)?'[sS]?[bBoOdDhH]"
            r"[0-9a-fA-F_xXzZ?]+|\d+",
            expression):
        return expression
    if cycles == 1:
        return f"$past({expression})"
    return f"$past({expression}, {cycles})"


def _target_at_history(target, cycles):
    """Sample dynamic lvalue indices at the antecedent edge."""
    text = str(target)
    if cycles <= 0 or "[" not in text:
        return text
    return re.sub(
        r"\[([^\[\]]+)\]",
        lambda match: f"[{_past(match.group(1), cycles)}]", text)


def _effect(prop, history_cycles=None):
    effect = prop.get("effect", {}) or {}
    effect_type = effect.get("type", "condition")
    target = effect.get("target")
    expression = effect.get("expression")
    reference = effect.get("reference_time", "current")
    target_reference = effect.get("target_reference_time", "current")
    if effect_type == "stable":
        if not target:
            raise SVALoweringError(
                f"property {prop.get('name')}: stable effect requires target")
        return f"$stable({target})"
    if effect_type == "state_transition":
        destination = effect.get("to")
        if not target or destination is None:
            raise SVALoweringError(
                f"property {prop.get('name')}: incomplete state transition effect")
        return f"{target} == {destination}"
    if effect_type == "pipeline_output":
        raise SVALoweringError("pipeline_output must be lowered by pipeline handler")
    if expression is None:
        if target:
            return str(target)
        raise SVALoweringError(
            f"property {prop.get('name')}: effect expression is missing")
    rhs = str(expression)
    if reference in {"trigger", "previous"}:
        rhs = _past(rhs, 1 if history_cycles is None else history_cycles)
    if target and target_reference in {"trigger", "previous"}:
        target = _target_at_history(
            target, 1 if history_cycles is None else history_cycles)
    return f"{target} == {rhs}" if target else rhs


def _effect_diagnostics(prop, history_cycles):
    """Return structured actual/expected procedural expressions."""
    effect = prop.get("effect", {}) or {}
    effect_type = effect.get("type", "condition")
    target = effect.get("target")
    expression = effect.get("expression")
    reference = effect.get("reference_time", "current")
    target_reference = effect.get("target_reference_time", "current")
    cycles = max(0, int(history_cycles or 0))
    if effect_type == "stable" and target:
        return str(target), _past(target, cycles or 1)
    if effect_type == "state_transition" and target is not None:
        destination = effect.get("to")
        if destination is not None:
            return str(target), str(destination)
    if effect_type == "pipeline_output" or expression is None:
        return None, None
    rhs = str(expression)
    if reference in {"trigger", "previous"}:
        rhs = _past(rhs, cycles or 1)
    if target:
        lhs = str(target)
        if target_reference in {"trigger", "previous"}:
            lhs = _target_at_history(lhs, cycles or 1)
        return lhs, rhs
    return rhs, "1'b1"


def _duration_consequent(prop, consequent):
    temporal = prop.get("temporal", {}) or {}
    duration = temporal.get("duration")
    until = temporal.get("until")
    if isinstance(until, dict) and until.get("expression"):
        return f"({consequent}) until_with ({until['expression']})"
    if not isinstance(duration, dict):
        return consequent
    minimum, maximum = _interval(duration, (1, 1))
    repeat = str(minimum) if minimum == maximum else f"{minimum}:{maximum}"
    return f"({consequent})[*{repeat}]"


def _bounded_history_body(prop, antecedent, minimum, maximum):
    branches = []
    for cycles in range(minimum, maximum + 1):
        branches.append(f"##{cycles} ({_effect(prop, cycles)})")
    return RawBody(f"({antecedent}) |-> (" + " or ".join(branches) + ")")


def _property_ast(program, used_names, prop, body, *, suffix="", role="primary",
                  emit_assert=None, emit_cover=None, clock=None, disable=None,
                  diagnostic_trigger=None, diagnostic_actual=None,
                  diagnostic_expected=None, generate_loop=None):
    measure = _measurement(prop)
    base = prop.get("name", "unnamed_property")
    name = _unique_name(base + suffix, used_names)
    requirement = prop.get("source_requirement", {}) or {}
    requirement_id = (str(requirement.get("id", ""))
                      if isinstance(requirement, dict) else "")
    spec_source = ""
    if isinstance(requirement, dict) and requirement.get("file"):
        spec_source = str(requirement["file"])
        line_start = requirement.get("line_start")
        line_end = requirement.get("line_end")
        if line_start:
            spec_source += f":{line_start}"
            if line_end and line_end != line_start:
                spec_source += f"-{line_end}"
    diagnostic_values = {
        "trigger": diagnostic_trigger,
        "actual": diagnostic_actual,
        "expected": diagnostic_expected,
    }
    for label, expression in tuple(diagnostic_values.items()):
        if expression is not None and _PROPERTY_ONLY_SYNTAX.search(str(expression)):
            program.warnings.append(
                f"property {base}: omitted diagnostic_{label} because it "
                "contains property/sequence-only syntax")
            diagnostic_values[label] = None
    ast = PropertyAST(
        name=name,
        clock=clock or _property_clock(program._model, prop),
        disable=_disable(prop) if disable is None else disable,
        property_expression=body,
        source_property=str(base),
        role=role,
        category=str(prop.get("category", "unknown")),
        emit_assert=measure["assert"] if emit_assert is None else emit_assert,
        emit_cover=measure["cover"] if emit_cover is None else emit_cover,
        diagnostic_trigger=(str(diagnostic_values["trigger"])
                            if diagnostic_values["trigger"] is not None else None),
        diagnostic_actual=(str(diagnostic_values["actual"])
                           if diagnostic_values["actual"] is not None else None),
        diagnostic_expected=(str(diagnostic_values["expected"])
                             if diagnostic_values["expected"] is not None else None),
        requirement_id=requirement_id,
        spec_source=spec_source,
        generate_loop=generate_loop,
    )
    program.properties.append(ast)
    return ast


def _add_delay_covers(program, used_names, prop, antecedent, consequent_builder,
                      minimum, maximum, clock=None, generate_loop=None):
    if not _measurement(prop)["delay_covers"]:
        return
    for cycles in range(minimum, maximum + 1):
        consequent = consequent_builder(cycles)
        body = _implication(antecedent, consequent, cycles, cycles)
        _property_ast(
            program, used_names, prop, body, suffix=f"__delay_{cycles}_cover",
            role=f"delay_cover_{cycles}", emit_assert=False, emit_cover=True,
            clock=clock, generate_loop=generate_loop)


def _lower_generic(program, used_names, prop, *, generate_loop=None):
    antecedent = _trigger(prop)
    minimum, maximum = _delay(prop)
    effect = prop.get("effect", {}) or {}
    history = effect.get("reference_time") in {"trigger", "previous"}
    if history and minimum != maximum:
        body = _bounded_history_body(prop, antecedent, minimum, maximum)
    else:
        consequent = _duration_consequent(
            prop, _effect(prop, minimum if history else None))
        body = _implication(antecedent, consequent, minimum, maximum)
    diagnostic_trigger = None
    diagnostic_actual = None
    diagnostic_expected = None
    if minimum == maximum and "[*" not in consequent and "until_with" not in consequent:
        diagnostic_trigger = _past(antecedent, minimum)
        diagnostic_actual, diagnostic_expected = _effect_diagnostics(
            prop, minimum)
    _property_ast(
        program, used_names, prop, body,
        diagnostic_trigger=diagnostic_trigger,
        diagnostic_actual=diagnostic_actual,
        diagnostic_expected=diagnostic_expected,
        generate_loop=generate_loop)
    _add_delay_covers(
        program, used_names, prop, antecedent,
        lambda cycles: _duration_consequent(
            prop, _effect(prop, cycles if history else None)),
        minimum, maximum, generate_loop=generate_loop)


def _whole_memory_state(program, prop):
    """Return the target state for an unindexed whole-memory update."""
    effect = prop.get("effect", {}) or {}
    if effect.get("type", "condition") != "state_update":
        return None
    target = str(effect.get("target", "")).strip()
    if not re.fullmatch(r"[A-Za-z_$][\w$]*", target):
        return None
    state = program._model.state_map().get(target)
    if state is None or state.depth in (None, ""):
        return None
    return state


def _lower_whole_memory_update(program, used_names, prop, state):
    """Lower one aggregate memory update into an elaborated element check."""
    base = _identifier(prop.get("name", "memory_update"))
    loop_label = _unique_name(f"gen_{base}_elements", used_names)
    index = f"__sva_i_{base}"
    loop = GenerateLoop(index=index, stop=str(state.depth), label=loop_label)

    element_prop = dict(prop)
    effect = dict(prop.get("effect", {}) or {})
    target = str(effect["target"])
    effect["target"] = f"{target}[{index}]"

    # A whole-memory copy is also element-wise. Scalar fill expressions such
    # as '0 remain unchanged and are context-sized by the element comparison.
    expression = str(effect.get("expression", "")).strip()
    source_state = program._model.state_map().get(expression)
    if source_state is not None and source_state.depth not in (None, ""):
        effect["expression"] = f"{expression}[{index}]"
    element_prop["effect"] = effect
    _lower_generic(program, used_names, element_prop, generate_loop=loop)


def _lower_invariant(program, used_names, prop):
    invariant = prop.get("invariant", {}) or {}
    expression = invariant.get("expression")
    if expression is None:
        raise SVALoweringError(
            f"property {prop.get('name')}: invariant.expression is missing")
    _property_ast(
        program, used_names, prop, DirectBody(str(expression)),
        diagnostic_actual=str(expression), diagnostic_expected="1'b1")


def _lower_pipeline(program, used_names, prop):
    antecedent = _trigger(prop)
    minimum, maximum = _delay(prop)
    effect = prop.get("effect", {}) or {}
    valid = effect.get("valid", {}) or {}
    data = effect.get("data", {}) or {}

    def consequent(cycles):
        pieces = []
        if valid.get("expression") is not None:
            pieces.append(str(valid["expression"]))
        if data.get("target") and data.get("source"):
            source = (_past(data["source"], cycles)
                      if data.get("reference_time", "trigger") == "trigger"
                      else str(data["source"]))
            pieces.append(f"{data['target']} == {source}")
        if not pieces:
            raise SVALoweringError(
                f"property {prop.get('name')}: pipeline output is incomplete")
        return " && ".join(f"({piece})" for piece in pieces)

    if minimum == maximum:
        body = _implication(antecedent, consequent(minimum), minimum, maximum)
    else:
        branches = [f"##{cycles} ({consequent(cycles)})"
                    for cycles in range(minimum, maximum + 1)]
        body = RawBody(
            f"({antecedent}) |-> (" + " or ".join(branches) + ")")
    _property_ast(program, used_names, prop, body)
    _add_delay_covers(
        program, used_names, prop, antecedent, consequent, minimum, maximum)


def _lower_handshake(program, used_names, prop):
    handshake = prop.get("handshake", {}) or {}
    request = handshake.get("request")
    acknowledge = handshake.get("acknowledge")
    if not request or not acknowledge:
        raise SVALoweringError(
            f"property {prop.get('name')}: handshake request/acknowledge missing")
    minimum, maximum = _interval(
        handshake.get("response_delay"), (1, 1))
    response = _implication(request, acknowledge, minimum, maximum)
    _property_ast(program, used_names, prop, response, suffix="__response",
                  role="handshake_response")
    _add_delay_covers(
        program, used_names, prop, request, lambda _: str(acknowledge),
        minimum, maximum)
    if handshake.get("request_hold_until_ack"):
        body = _implication(f"{request} && !({acknowledge})", request, 1, 1)
        _property_ast(
            program, used_names, prop, body, suffix="__request_hold",
            role="handshake_request_hold", emit_cover=False)
    if handshake.get("acknowledge_requires_request"):
        body = _implication(acknowledge, request, 0, 0)
        _property_ast(
            program, used_names, prop, body, suffix="__ack_requires_request",
            role="handshake_ack_requires_request", emit_cover=False)


def _event_expression(item):
    event = item.get("event") if isinstance(item, dict) else item
    if isinstance(event, dict):
        signal = event.get("signal")
        kind = event.get("type", event.get("event", "level"))
        if kind == "rose":
            return f"$rose({signal})"
        if kind == "fell":
            return f"$fell({signal})"
        if kind == "changed":
            return f"$changed({signal})"
        return str(event.get("expression", signal))
    return str(event)


def _lower_ordering(program, used_names, prop):
    events = prop.get("sequence", []) or []
    if not events:
        raise SVALoweringError(
            f"property {prop.get('name')}: ordered sequence is empty")
    pieces = [_event_expression(events[0])]
    for item in events[1:]:
        delay = item.get("delay") if isinstance(item, dict) else None
        minimum, maximum = _interval(delay, (0, 0))
        pieces.append(_delay_prefix(minimum, maximum).strip())
        pieces.append(_event_expression(item))
    sequence_name = _unique_name("seq_" + str(prop.get("name")), used_names)
    program.sequences.append(SequenceAST(
        sequence_name, " ".join(piece for piece in pieces if piece),
        str(prop.get("name"))))
    _property_ast(program, used_names, prop, DirectBody(sequence_name))


def _lower_pulse_width(program, used_names, prop):
    trigger = prop.get("trigger", {}) or {}
    signal = trigger.get("signal")
    if not signal:
        raise SVALoweringError(
            f"property {prop.get('name')}: pulse width requires trigger.signal")
    duration = prop.get("duration", {}) or {}
    minimum, maximum = _interval(duration, (1, 1))
    high = duration.get("level", "high") == "high"
    start = f"$rose({signal})" if high else f"$fell({signal})"
    asserted = str(signal) if high else f"!({signal})"
    released = f"!({signal})" if high else str(signal)
    minimum_body = _implication(start, f"({asserted})[*{minimum}]", 0, 0)
    _property_ast(program, used_names, prop, minimum_body,
                  suffix="__minimum_width", role="pulse_minimum")
    maximum_body = _implication(start, released, maximum, maximum)
    _property_ast(program, used_names, prop, maximum_body,
                  suffix="__maximum_width", role="pulse_maximum")


def _lower_mutex(program, used_names, prop):
    participants = prop.get("participants", []) or []
    if len(participants) < 2:
        raise SVALoweringError(
            f"property {prop.get('name')}: mutual exclusion needs participants")
    expression = "$onehot0({" + ", ".join(map(str, participants)) + "})"
    _property_ast(program, used_names, prop, DirectBody(expression))


def _transition_consequent(transition, cycles):
    if not transition.actions:
        raise SVALoweringError(
            f"transition {transition.name!r} has no observable action")
    return " && ".join(
        f"({action.target} == {_past(action.expression, cycles)})"
        for action in transition.actions)


def _lower_priority(program, used_names, prop):
    relation = prop.get("priority", {}) or {}
    transition_map = program._model.transition_map()
    higher = transition_map.get(relation.get("higher"))
    lower = transition_map.get(relation.get("lower"))
    if not higher or not lower:
        raise SVALoweringError(
            f"property {prop.get('name')}: priority transition is missing")
    high_guard = re.sub(r"[\s()]", "", higher.guard)
    low_guard = re.sub(r"[\s()]", "", lower.guard)
    complementary = (high_guard == "!" + low_guard or
                     low_guard == "!" + high_guard)
    if complementary:
        antecedent = higher.guard
        program.warnings.append(
            f"property {prop.get('name')}: priority guards are complementary; "
            "lowered the selected higher-transition effect under its own guard")
    else:
        antecedent = f"({higher.guard}) && ({lower.guard})"
    body = _implication(
        antecedent, _transition_consequent(higher, 1), 1, 1)
    _property_ast(program, used_names, prop, body)


def _lower_cdc(program, used_names, prop):
    cdc = prop.get("cdc", {}) or {}
    source = cdc.get("source_signal")
    destination = cdc.get("destination_signal")
    if not source or not destination:
        raise SVALoweringError(
            f"property {prop.get('name')}: CDC source/destination missing")
    minimum, maximum = _delay(prop)
    clock = _property_clock(program._model, prop)
    antecedent = f"$changed({source})"
    if minimum == maximum:
        consequent = f"{destination} == {_past(source, minimum)}"
        body = _implication(antecedent, consequent, minimum, maximum)
    else:
        branches = [
            f"##{cycles} ({destination} == {_past(source, cycles)})"
            for cycles in range(minimum, maximum + 1)]
        body = RawBody(
            f"({antecedent}) |-> (" + " or ".join(branches) + ")")
    _property_ast(program, used_names, prop, body, suffix="__visibility",
                  role="cdc_visibility", clock=clock)
    _add_delay_covers(
        program, used_names, prop, antecedent,
        lambda cycles: f"{destination} == {_past(source, cycles)}",
        minimum, maximum, clock=clock)
    data_property = cdc.get("data_property", {}) or {}
    if data_property.get("type") == "gray":
        source_domain = cdc.get("source_clock_domain")
        gray_clock = (_clock_for_domain(program._model, source_domain)
                      if source_domain else clock)
        gray = f"$onehot0({source} ^ $past({source}))"
        _property_ast(
            program, used_names, prop, DirectBody(gray), suffix="__gray_change",
            role="cdc_gray_change", clock=gray_clock, emit_cover=False)


def _lower_one(program, used_names, prop):
    memory_state = _whole_memory_state(program, prop)
    if memory_state is not None:
        _lower_whole_memory_update(program, used_names, prop, memory_state)
        return
    category = prop.get("category")
    if category == "state_invariant":
        _lower_invariant(program, used_names, prop)
    elif category == "pipeline_latency":
        _lower_pipeline(program, used_names, prop)
    elif category == "handshake":
        _lower_handshake(program, used_names, prop)
    elif category == "event_ordering":
        _lower_ordering(program, used_names, prop)
    elif category == "pulse_width":
        _lower_pulse_width(program, used_names, prop)
    elif category == "mutual_exclusion":
        _lower_mutex(program, used_names, prop)
    elif category == "priority":
        _lower_priority(program, used_names, prop)
    elif category == "cdc":
        _lower_cdc(program, used_names, prop)
    else:
        _lower_generic(program, used_names, prop)


def lower_property_ir(hw_ir_path, property_ir_path):
    """Return an SVA AST program for an already checked Property IR file."""
    model = parse_ir(hw_ir_path)
    try:
        data = yaml.safe_load(Path(property_ir_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        raise SVALoweringError(f"cannot read Property IR: {exc}") from exc
    properties = data.get("sva_properties") if isinstance(data, dict) else None
    if not isinstance(properties, list):
        raise SVALoweringError("Property IR requires an sva_properties list")
    program = SVAProgram(model.module_name)
    # Lowering helpers need HW transition/domain lookup but this context is not
    # part of the backend AST and is removed before returning.
    program._model = model
    used_names = set()
    for index, prop in enumerate(properties):
        if not isinstance(prop, dict):
            raise SVALoweringError(f"sva_properties[{index}] must be a mapping")
        try:
            _lower_one(program, used_names, prop)
        except (KeyError, TypeError, ValueError) as exc:
            raise SVALoweringError(
                f"property {prop.get('name', index)!r} cannot be lowered: {exc}") from exc
    del program._model
    return program


def generate_sva(hw_ir_path, property_ir_path, output_path, report_path=None,
                 *, compatibility_profile="default"):
    """Lower Property IR, emit SVA, and write a generation report."""
    program = lower_property_ir(hw_ir_path, property_ir_path)
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    output.write_text(
        emit_sva(program, compatibility_profile=compatibility_profile),
        encoding="utf-8")
    report_output = Path(report_path or output.with_name(
        "sva_generation_report.json")).resolve()
    assertions = sum(item.emit_assert for item in program.properties)
    covers = sum(item.emit_cover for item in program.properties)
    compatibility_level = COMPATIBILITY_PROFILES.index(
        compatibility_profile)
    minimal_actions = compatibility_level >= 2
    diagnostic_values = (0 if minimal_actions else sum(
        len(item.diagnostics) for item in program.properties
        if item.emit_assert))
    report = {
        "generator": "sva_generator",
        "status": "passed",
        "generated_at": datetime.now(timezone.utc).isoformat(),
        "hw_ir": str(Path(hw_ir_path).resolve()),
        "property_ir": str(Path(property_ir_path).resolve()),
        "output": str(output),
        "module": program.module_name,
        "compatibility_profile": compatibility_profile,
        "source_property_count": len(yaml.safe_load(
            Path(property_ir_path).read_text(encoding="utf-8"))["sva_properties"]),
        "property_generation_source": yaml.safe_load(
            Path(property_ir_path).read_text(encoding="utf-8")).get(
                "generation_source", "hw_ir"),
        "sequence_count": len(program.sequences),
        "property_declaration_count": (
            0 if compatibility_level >= 3 else len(program.properties)),
        "generate_loop_count": sum(
            item.generate_loop is not None for item in program.properties),
        "assertion_count": assertions,
        "assertion_action_block_count": assertions,
        "assertion_diagnostic_value_count": diagnostic_values,
        "cover_count": covers,
        "warnings": program.warnings,
    }
    report_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    return output, report_output, report
