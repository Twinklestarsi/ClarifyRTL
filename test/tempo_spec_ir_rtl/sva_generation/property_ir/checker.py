"""Deterministic checker for Temporal Property IR."""

import json
import re
from datetime import datetime, timezone
from pathlib import Path

import yaml

from Compiler.rtl_compiler.parser import parse_ir

from .generator import cdc_reference_names


_CATEGORIES = {
    "reset", "state_update", "fixed_delay", "bounded_delay",
    "pipeline_latency", "hold", "stable", "edge_event",
    "request_response", "handshake", "event_ordering", "fsm_transition",
    "cdc", "pulse_width", "mutual_exclusion", "priority",
    "state_invariant",
}
_RELATIONS = {
    "same_cycle", "next_cycle", "fixed_delay", "bounded_delay", "hold",
    "hold_until", "stable", "response", "pipeline", "ordered_sequence",
    "pulse_width", "cdc_sync",
}
_REFERENCE_TIMES = {"current", "trigger", "previous"}
_IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
_EXPRESSION_IDENTIFIER = re.compile(
    r"(?<!['$])\b[A-Za-z_][A-Za-z0-9_$]*\b")
_EXPRESSION_WORDS = {"true", "false", "signed", "unsigned"}
_TRIGGER_EVENTS = {"level", "rose", "fell", "changed", "stable"}


class PropertyIRCheckerError(RuntimeError):
    def __init__(self, report_path, errors):
        self.report_path = str(report_path)
        self.errors = errors
        super().__init__(
            f"Property IR check failed with {len(errors)} error(s); see {report_path}")


def _error(code, location, message):
    return {"code": code, "location": location, "message": message,
            "severity": "error"}


def _strip_balanced_outer_parentheses(value):
    text = str(value).strip()
    while text.startswith("(") and text.endswith(")"):
        depth = 0
        closes_at_end = False
        for index, char in enumerate(text):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0:
                    closes_at_end = index == len(text) - 1
                    break
        if not closes_at_end:
            break
        text = text[1:-1].strip()
    return text


def _top_level_equality(value):
    """Return (lhs, rhs, safe_root) for a top-level ==/=== expression."""
    text = _strip_balanced_outer_parentheses(value)
    depths = {"(": 0, "[": 0, "{": 0}
    closing = {")": "(", "]": "[", "}": "{"}
    equalities = []
    lower_precedence = False
    index = 0
    while index < len(text):
        char = text[index]
        if char in depths:
            depths[char] += 1
            index += 1
            continue
        if char in closing:
            opener = closing[char]
            depths[opener] = max(0, depths[opener] - 1)
            index += 1
            continue
        if any(depths.values()):
            index += 1
            continue
        if text.startswith("===", index):
            equalities.append((index, 3))
            index += 3
            continue
        if text.startswith("==", index):
            equalities.append((index, 2))
            index += 2
            continue
        if (text.startswith("&&", index) or text.startswith("||", index) or
                char in "?:"):
            lower_precedence = True
        index += 1
    if not equalities:
        return None
    position, width = equalities[0]
    lhs = _strip_balanced_outer_parentheses(text[:position])
    rhs = text[position + width:].strip()
    safe_root = len(equalities) == 1 and not lower_precedence and bool(rhs)
    return lhs, rhs, safe_root


def _normalize_effect_expected_values(data):
    """Canonicalize target + `target == expected` to target + expected."""
    normalizations = []
    properties = data.get("sva_properties", []) if isinstance(data, dict) else []
    if not isinstance(properties, list):
        return normalizations
    for index, prop in enumerate(properties):
        if not isinstance(prop, dict):
            continue
        effect = prop.get("effect")
        if not isinstance(effect, dict) or not effect.get("target"):
            continue
        expression = effect.get("expression")
        if expression is None:
            continue
        equality = _top_level_equality(expression)
        target = _strip_balanced_outer_parentheses(effect["target"])
        if equality is None or equality[0] != target or not equality[2]:
            continue
        before = str(expression)
        effect["expression"] = equality[1]
        normalizations.append({
            "normalization_type": "extract_effect_expected_value",
            "location": f"$.sva_properties[{index}].effect.expression",
            "property": prop.get("name"),
            "target": str(effect["target"]),
            "before": before,
            "after": equality[1],
            "message": (
                "Removed the duplicated target equality; effect.expression "
                "now contains only the expected RHS value"),
        })
    return normalizations


def _check_spec_traceability(prop, location, errors):
    if prop.get("generation_source") != "spec":
        errors.append(_error(
            "PROPERTY_SPEC_SOURCE_MISSING", location + ".generation_source",
            "Spec-derived property must declare generation_source: spec"))
    requirement = prop.get("source_requirement")
    if not isinstance(requirement, dict):
        errors.append(_error(
            "PROPERTY_SPEC_SOURCE_MISSING", location + ".source_requirement",
            "Spec-derived property requires source_requirement mapping"))
        return
    if not str(requirement.get("text", "")).strip():
        errors.append(_error(
            "PROPERTY_SPEC_SOURCE_MISSING",
            location + ".source_requirement.text",
            "Spec-derived property requires the supporting specification text"))
    for field in ("line_start", "line_end"):
        value = requirement.get(field)
        if value is not None and (not isinstance(value, int) or value < 1):
            errors.append(_error(
                "PROPERTY_SPEC_SOURCE_INVALID",
                f"{location}.source_requirement.{field}",
                f"{field} must be a positive integer or null"))


def _list(value):
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _known_expression_names(model):
    return ({parameter.name for parameter in model.parameters} |
            {port.name for port in model.ports} |
            set(model.state_map()) |
            {str(signal.get("name", "")) for signal in model.derived_signals})


def _expression_identifiers(value):
    if value is None:
        return set()
    return set(_EXPRESSION_IDENTIFIER.findall(str(value)))


def _behavior_reference_fields(prop, location):
    """Yield behavioral expression/signal fields that may reference states."""
    trigger = prop.get("trigger")
    if isinstance(trigger, dict):
        if trigger.get("signal"):
            yield location + ".trigger.signal", trigger["signal"]
        if trigger.get("expression"):
            yield location + ".trigger.expression", trigger["expression"]
    disable = prop.get("disable")
    if isinstance(disable, dict) and disable.get("expression"):
        yield location + ".disable.expression", disable["expression"]
    temporal = prop.get("temporal")
    if isinstance(temporal, dict):
        until = temporal.get("until")
        if isinstance(until, dict) and until.get("expression"):
            yield (location + ".temporal.until.expression",
                   until["expression"])
    invariant = prop.get("invariant")
    if isinstance(invariant, dict) and invariant.get("expression"):
        yield location + ".invariant.expression", invariant["expression"]
    effect = prop.get("effect")
    if isinstance(effect, dict):
        if effect.get("expression"):
            yield location + ".effect.expression", effect["expression"]
        valid = effect.get("valid")
        if isinstance(valid, dict) and valid.get("expression"):
            yield location + ".effect.valid.expression", valid["expression"]
        data = effect.get("data")
        if isinstance(data, dict):
            if data.get("source"):
                yield location + ".effect.data.source", data["source"]
    handshake = prop.get("handshake")
    if isinstance(handshake, dict):
        for field in ("request", "acknowledge"):
            if handshake.get(field):
                yield f"{location}.handshake.{field}", handshake[field]
    sequence = prop.get("sequence")
    if isinstance(sequence, list):
        for index, item in enumerate(sequence):
            event = item.get("event") if isinstance(item, dict) else item
            event_location = f"{location}.sequence[{index}].event"
            if isinstance(event, dict):
                if event.get("signal"):
                    yield event_location + ".signal", event["signal"]
                if event.get("expression"):
                    yield event_location + ".expression", event["expression"]
            elif event is not None:
                yield event_location, event
    participants = prop.get("participants")
    if isinstance(participants, list):
        for index, participant in enumerate(participants):
            yield f"{location}.participants[{index}]", participant


def _check_structural_bindings(prop, location, model, related_states, errors):
    """Reject accidental raw CDC, wrong state ownership, and reset binding."""
    if prop.get("category") == "cdc":
        return
    clock = prop.get("clock")
    domain_name = clock.get("domain") if isinstance(clock, dict) else None
    domain = model.domain_map().get(domain_name)
    if domain is None:
        return
    states = model.state_map()

    mismatched_scope = sorted({
        name for name in related_states
        if name in states and states[name].clock_domain != domain_name
    })
    if mismatched_scope:
        errors.append(_error(
            "PROPERTY_STATE_CLOCK_MISMATCH",
            location + ".scope.related_states",
            f"states are not owned by clock domain {domain_name!r}: "
            f"{mismatched_scope}"))

    effect = prop.get("effect")
    if isinstance(effect, dict) and effect.get("target"):
        target = str(effect["target"]).split("[", 1)[0].strip()
        state = states.get(target)
        if state is not None and state.clock_domain != domain_name:
            errors.append(_error(
                "PROPERTY_STATE_CLOCK_MISMATCH",
                location + ".effect.target",
                f"target {target!r} belongs to {state.clock_domain!r}, not "
                f"property clock domain {domain_name!r}"))
    if isinstance(effect, dict):
        data = effect.get("data")
        if isinstance(data, dict) and data.get("target"):
            target = str(data["target"]).split("[", 1)[0].strip()
            state = states.get(target)
            if state is not None and state.clock_domain != domain_name:
                errors.append(_error(
                    "PROPERTY_STATE_CLOCK_MISMATCH",
                    location + ".effect.data.target",
                    f"target {target!r} belongs to {state.clock_domain!r}, "
                    f"not property clock domain {domain_name!r}"))

    seen = set()
    for field_location, value in _behavior_reference_fields(prop, location):
        for name in sorted(_expression_identifiers(value) & set(states)):
            state = states[name]
            key = (field_location, name)
            if state.clock_domain == domain_name or key in seen:
                continue
            seen.add(key)
            errors.append(_error(
                "PROPERTY_UNSYNCHRONIZED_CDC_REFERENCE", field_location,
                f"state {name!r} belongs to {state.clock_domain!r} but this "
                f"property runs in {domain_name!r}; reference the final "
                "destination-domain synchronizer stage instead"))

    reset_signals = {
        item.reset for item in model.clock_domains if item.reset
    }
    expected_reset = domain.reset
    if prop.get("category") == "reset" and expected_reset:
        trigger = prop.get("trigger")
        trigger_names = set()
        if isinstance(trigger, dict):
            trigger_names |= _expression_identifiers(trigger.get("signal"))
            trigger_names |= _expression_identifiers(trigger.get("expression"))
        used_resets = trigger_names & reset_signals
        if expected_reset not in used_resets or used_resets - {expected_reset}:
            errors.append(_error(
                "PROPERTY_RESET_DOMAIN_MISMATCH", location + ".trigger",
                f"reset property for {domain_name!r} must be triggered by "
                f"its reset {expected_reset!r}, got {sorted(used_resets)}"))

    disable = prop.get("disable")
    if isinstance(disable, dict) and disable.get("expression") and expected_reset:
        used_resets = (_expression_identifiers(disable["expression"]) &
                       reset_signals)
        if expected_reset not in used_resets or used_resets - {expected_reset}:
            errors.append(_error(
                "PROPERTY_RESET_DOMAIN_MISMATCH",
                location + ".disable.expression",
                f"disable for {domain_name!r} must use only its reset "
                f"{expected_reset!r}, got {sorted(used_resets)}"))


def _check_expression(expression, location, model, errors, *, required=False):
    if expression is None or not str(expression).strip():
        if required:
            errors.append(_error(
                "PROPERTY_EXPRESSION_INVALID", location,
                "expression is required"))
        return
    unknown = sorted({
        name for name in _EXPRESSION_IDENTIFIER.findall(str(expression))
        if name not in _known_expression_names(model) and
        name.lower() not in _EXPRESSION_WORDS
    })
    if unknown:
        errors.append(_error(
            "PROPERTY_EXPRESSION_UNKNOWN_REFERENCE", location,
            f"expression references unknown identifiers {unknown}"))


def _check_trigger_and_expressions(prop, location, model, errors):
    trigger = prop.get("trigger")
    if trigger is not None:
        if not isinstance(trigger, dict):
            errors.append(_error("PROPERTY_TRIGGER_INVALID", location + ".trigger",
                                 "trigger must be a mapping"))
        else:
            event = trigger.get("event", "level")
            if event not in _TRIGGER_EVENTS:
                errors.append(_error(
                    "PROPERTY_TRIGGER_INVALID", location + ".trigger.event",
                    f"unsupported trigger event {event!r}"))
            signal = trigger.get("signal")
            if signal and signal not in _known_expression_names(model):
                errors.append(_error(
                    "PROPERTY_UNKNOWN_SIGNAL", location + ".trigger.signal",
                    f"unknown trigger signal {signal!r}"))
            _check_expression(trigger.get("expression"),
                              location + ".trigger.expression", model, errors)
            if not signal and not trigger.get("expression"):
                errors.append(_error(
                    "PROPERTY_TRIGGER_INVALID", location + ".trigger",
                    "trigger requires signal or expression"))
    disable = prop.get("disable")
    if disable is not None:
        if not isinstance(disable, dict):
            errors.append(_error("PROPERTY_EXPRESSION_INVALID", location + ".disable",
                                 "disable must be a mapping"))
        else:
            _check_expression(disable.get("expression"),
                              location + ".disable.expression", model, errors,
                              required=True)
    temporal = prop.get("temporal")
    if isinstance(temporal, dict):
        until = temporal.get("until")
        if isinstance(until, dict):
            _check_expression(until.get("expression"),
                              location + ".temporal.until.expression", model,
                              errors, required=True)
    invariant = prop.get("invariant")
    if invariant is not None:
        if not isinstance(invariant, dict):
            errors.append(_error("PROPERTY_EXPRESSION_INVALID",
                                 location + ".invariant",
                                 "invariant must be a mapping"))
        else:
            _check_expression(invariant.get("expression"),
                              location + ".invariant.expression", model, errors,
                              required=True)


def _check_known_signal(value, location, model, errors):
    if value not in _known_expression_names(model):
        errors.append(_error(
            "PROPERTY_UNKNOWN_SIGNAL", location,
            f"unknown signal {value!r}"))


def _check_composite_fields(prop, location, model, errors):
    category = prop.get("category")
    if category == "handshake":
        handshake = prop.get("handshake")
        if not isinstance(handshake, dict):
            errors.append(_error("PROPERTY_HANDSHAKE_INVALID",
                                 location + ".handshake",
                                 "handshake property requires a mapping"))
        else:
            for field in ("request", "acknowledge"):
                _check_known_signal(handshake.get(field),
                                    f"{location}.handshake.{field}", model,
                                    errors)
            _check_interval(handshake, "response_delay",
                            location + ".handshake", errors)
    elif category == "event_ordering":
        sequence = prop.get("sequence")
        if not isinstance(sequence, list) or not sequence:
            errors.append(_error("PROPERTY_SEQUENCE_INVALID",
                                 location + ".sequence",
                                 "event_ordering requires a non-empty sequence"))
        else:
            for index, item in enumerate(sequence):
                item_location = f"{location}.sequence[{index}]"
                event = item.get("event") if isinstance(item, dict) else item
                if isinstance(event, dict):
                    signal = event.get("signal")
                    expression = event.get("expression")
                    if signal:
                        _check_known_signal(signal, item_location + ".event.signal",
                                            model, errors)
                    else:
                        _check_expression(expression,
                                          item_location + ".event.expression",
                                          model, errors, required=True)
                else:
                    _check_expression(event, item_location + ".event", model,
                                      errors, required=True)
                if isinstance(item, dict):
                    _check_interval(item, "delay", item_location, errors)
    elif category == "pipeline_latency":
        effect = prop.get("effect", {}) or {}
        valid = effect.get("valid", {}) or {}
        data = effect.get("data", {}) or {}
        _check_expression(valid.get("expression"),
                          location + ".effect.valid.expression", model, errors)
        target = data.get("target")
        source = data.get("source")
        if target:
            _check_known_signal(str(target).split("[", 1)[0],
                                location + ".effect.data.target", model, errors)
        if source:
            _check_expression(source, location + ".effect.data.source", model,
                              errors, required=True)
        if not valid.get("expression") and not (target and source):
            errors.append(_error("PROPERTY_EFFECT_INVALID",
                                 location + ".effect",
                                 "pipeline output requires valid or data mapping"))
    elif category == "mutual_exclusion":
        participants = prop.get("participants")
        if not isinstance(participants, list) or len(participants) < 2:
            errors.append(_error("PROPERTY_PARTICIPANTS_INVALID",
                                 location + ".participants",
                                 "mutual_exclusion requires at least two participants"))
        else:
            for index, signal in enumerate(participants):
                _check_known_signal(signal,
                                    f"{location}.participants[{index}]", model,
                                    errors)
    elif category == "priority":
        priority = prop.get("priority")
        transitions = model.transition_map()
        if not isinstance(priority, dict):
            errors.append(_error("PROPERTY_PRIORITY_INVALID",
                                 location + ".priority",
                                 "priority property requires a mapping"))
        else:
            for field in ("higher", "lower"):
                name = priority.get(field)
                if name not in transitions:
                    errors.append(_error(
                        "PROPERTY_UNKNOWN_TRANSITION",
                        f"{location}.priority.{field}",
                        f"unknown transition {name!r}"))
    elif category == "pulse_width":
        duration = prop.get("duration")
        if not isinstance(duration, dict):
            errors.append(_error("PROPERTY_TEMPORAL_INVALID",
                                 location + ".duration",
                                 "pulse_width requires duration mapping"))
        elif duration.get("level", "high") not in {"high", "low"}:
            errors.append(_error("PROPERTY_TEMPORAL_INVALID",
                                 location + ".duration.level",
                                 "pulse level must be high or low"))


def _check_interval(container, field, location, errors):
    interval = container.get(field) if isinstance(container, dict) else None
    if interval is None:
        return
    if not isinstance(interval, dict):
        errors.append(_error("PROPERTY_TEMPORAL_INVALID", f"{location}.{field}",
                             f"{field} must be a mapping"))
        return
    minimum = interval.get("min")
    maximum = interval.get("max")
    for key, value in (("min", minimum), ("max", maximum)):
        if value is not None and (not isinstance(value, int) or isinstance(value, bool)):
            errors.append(_error(
                "PROPERTY_TEMPORAL_INVALID", f"{location}.{field}.{key}",
                f"{field}.{key} must be an integer or null"))
    if isinstance(minimum, int) and minimum < 0:
        errors.append(_error(
            "PROPERTY_TEMPORAL_INVALID", f"{location}.{field}.min",
            f"{field}.min must be >= 0"))
    if (isinstance(minimum, int) and isinstance(maximum, int) and
            maximum < minimum):
        errors.append(_error(
            "PROPERTY_TEMPORAL_INVALID", f"{location}.{field}.max",
            f"{field}.max must be >= {field}.min"))


def _scope_references(prop, location, model, cdc_names, errors):
    scope = prop.get("scope", {}) or {}
    if not isinstance(scope, dict):
        errors.append(_error("PROPERTY_SCOPE_INVALID", location + ".scope",
                             "scope must be a mapping"))
        return [], []
    known = {
        "related_transitions": set(model.transition_map()),
        "related_states": set(model.state_map()),
        "related_signals": ({port.name for port in model.ports} |
                            {str(signal.get("name", ""))
                             for signal in model.derived_signals}),
        "related_cdc": set(cdc_names),
    }
    code = {
        "related_transitions": "PROPERTY_UNKNOWN_TRANSITION",
        "related_states": "PROPERTY_UNKNOWN_STATE",
        "related_signals": "PROPERTY_UNKNOWN_SIGNAL",
        "related_cdc": "PROPERTY_UNKNOWN_CDC",
    }
    labels = {
        "related_transitions": "transition",
        "related_states": "state",
        "related_signals": "signal",
        "related_cdc": "CDC reference",
    }
    references = {}
    for field, valid in known.items():
        references[field] = _list(scope.get(field))
        for value in references[field]:
            if value not in valid:
                errors.append(_error(
                    code[field], f"{location}.scope.{field}",
                    f"unknown {labels[field]} {value!r}"))
    return references["related_transitions"], references["related_states"]


def _check_clock(prop, location, model, related_transitions, errors):
    if prop.get("category") == "cdc":
        return
    clock = prop.get("clock", {}) or {}
    domain = clock.get("domain") if isinstance(clock, dict) else None
    if domain not in model.domain_map():
        errors.append(_error("PROPERTY_UNKNOWN_CLOCK", location + ".clock.domain",
                             f"unknown clock domain {domain!r}"))
        return
    transitions = model.transition_map()
    mismatched = [name for name in related_transitions
                  if name in transitions and
                  transitions[name].clock_domain != domain]
    if mismatched:
        errors.append(_error(
            "PROPERTY_CLOCK_MISMATCH", location + ".clock.domain",
            f"related transitions do not belong to {domain!r}: {mismatched}"))


def _check_temporal(prop, location, errors):
    temporal = prop.get("temporal")
    if temporal is not None:
        if not isinstance(temporal, dict):
            errors.append(_error("PROPERTY_TEMPORAL_INVALID", location + ".temporal",
                                 "temporal must be a mapping"))
            return
        relation = temporal.get("relation")
        if relation not in _RELATIONS:
            errors.append(_error(
                "PROPERTY_TEMPORAL_INVALID", location + ".temporal.relation",
                f"unknown temporal relation {relation!r}"))
        _check_interval(temporal, "delay", location + ".temporal", errors)
        _check_interval(temporal, "duration", location + ".temporal", errors)
        delay = temporal.get("delay", {}) or {}
        if prop.get("category") == "fixed_delay" and isinstance(delay, dict):
            if delay.get("min") != delay.get("max"):
                errors.append(_error(
                    "PROPERTY_TEMPORAL_INVALID", location + ".temporal.delay",
                    "fixed_delay requires delay.min == delay.max"))
    duration = prop.get("duration")
    if isinstance(duration, dict):
        _check_interval({"duration": duration}, "duration", location, errors)
    elif duration is not None:
        errors.append(_error("PROPERTY_TEMPORAL_INVALID", location + ".duration",
                             "duration must be a mapping"))


def _check_effect(prop, location, model, related_transitions, errors):
    effect = prop.get("effect")
    if effect is None:
        return
    if not isinstance(effect, dict):
        errors.append(_error("PROPERTY_EFFECT_INVALID", location + ".effect",
                             "effect must be a mapping"))
        return
    target = effect.get("target")
    base_target = str(target).split("[", 1)[0] if target else None
    known_targets = (set(model.state_map()) |
                     {port.name for port in model.ports} |
                     {str(item.get("name", "")) for item in model.derived_signals})
    if target and base_target not in known_targets:
        errors.append(_error("PROPERTY_UNKNOWN_TARGET", location + ".effect.target",
                             f"unknown effect target {target!r}"))
    reference_time = effect.get("reference_time")
    if reference_time is not None and reference_time not in _REFERENCE_TIMES:
        errors.append(_error(
            "PROPERTY_EFFECT_INVALID", location + ".effect.reference_time",
            f"invalid reference_time {reference_time!r}"))
    target_reference_time = effect.get("target_reference_time")
    if (target_reference_time is not None and
            target_reference_time not in _REFERENCE_TIMES):
        errors.append(_error(
            "PROPERTY_EFFECT_INVALID",
            location + ".effect.target_reference_time",
            f"invalid target_reference_time {target_reference_time!r}"))
    effect_type = effect.get("type")
    equality = (_top_level_equality(effect.get("expression"))
                if target and effect.get("expression") is not None else None)
    if (equality is not None and
            equality[0] == _strip_balanced_outer_parentheses(target)):
        errors.append(_error(
            "PROPERTY_EFFECT_DUPLICATE_TARGET",
            location + ".effect.expression",
            "effect.expression repeats effect.target on the left side of an "
            "equality but cannot be safely normalized; store only the "
            "expected RHS value when effect.target is present"))
    target_state = model.state_map().get(base_target) if base_target else None
    whole_memory_target = bool(
        target_state is not None and target_state.depth not in (None, "") and
        str(target).strip() == base_target)
    if whole_memory_target and effect_type != "state_update":
        errors.append(_error(
            "PROPERTY_MEMORY_EFFECT_UNSUPPORTED",
            location + ".effect.type",
            "an unindexed whole-memory target is supported only for "
            "state_update effects; use an indexed target for other effects"))
    _check_expression(
        effect.get("expression"), location + ".effect.expression", model, errors,
        required=effect_type in {"condition", "state_update"})
    if effect_type in {"stable", "state_update"} and not target:
        errors.append(_error(
            "PROPERTY_EFFECT_INVALID", location + ".effect.target",
            f"{effect_type} effect requires target"))
    if target and related_transitions:
        transition_map = model.transition_map()
        writers = [name for name in related_transitions
                   if name in transition_map and any(
                       assignment.base_target == base_target
                       for assignment in (transition_map[name].actions +
                                          transition_map[name].else_actions))]
        if not writers:
            errors.append(_error(
                "PROPERTY_TRANSITION_MISMATCH", location + ".effect.target",
                f"none of {related_transitions} writes {target!r}"))


def _check_cdc(prop, location, model, errors):
    if prop.get("category") != "cdc":
        return
    cdc = prop.get("cdc")
    if not isinstance(cdc, dict):
        errors.append(_error("PROPERTY_CDC_INVALID", location + ".cdc",
                             "cdc property requires a cdc mapping"))
        return
    domains = set(model.domain_map())
    destination = cdc.get("destination_clock_domain")
    source = cdc.get("source_clock_domain")
    if destination not in domains:
        errors.append(_error(
            "PROPERTY_UNKNOWN_CLOCK", location + ".cdc.destination_clock_domain",
            f"unknown destination clock domain {destination!r}"))
    if source is not None and source not in domains:
        errors.append(_error(
            "PROPERTY_UNKNOWN_CLOCK", location + ".cdc.source_clock_domain",
            f"unknown source clock domain {source!r}"))
    known_signals = _known_expression_names(model)
    for field in ("source_signal", "destination_signal"):
        signal = cdc.get(field)
        if signal not in known_signals:
            errors.append(_error(
                "PROPERTY_UNKNOWN_SIGNAL", f"{location}.cdc.{field}",
                f"unknown CDC {field} {signal!r}"))
    delay = cdc.get("delay", {}) or {}
    if isinstance(delay, dict) and delay.get("unit") not in {
            None, "destination_cycle"}:
        errors.append(_error(
            "PROPERTY_CDC_INVALID", location + ".cdc.delay.unit",
            "CDC delay unit must be destination_cycle"))
    _check_interval(cdc, "delay", location + ".cdc", errors)


def check_property_ir(hw_ir_path, property_ir_path, report_path=None,
                      *, raise_on_error=True):
    """Validate Property IR references and temporal consistency against HW-IR."""
    source = Path(property_ir_path).resolve()
    report_output = Path(report_path or source.with_name(
        "property_ir_checker_report.json")).resolve()
    report_output.parent.mkdir(parents=True, exist_ok=True)
    errors = []
    property_count = 0
    spec_derived = False
    normalizations = []
    backup_file = None
    try:
        model = parse_ir(hw_ir_path)
        original = source.read_text(encoding="utf-8")
        data = yaml.safe_load(original)
        if not isinstance(data, dict):
            raise ValueError("Property IR root must be a mapping")
        properties = data.get("sva_properties")
        if not isinstance(properties, list):
            raise ValueError("sva_properties must be a list")
        normalizations = _normalize_effect_expected_values(data)
        if normalizations:
            normalized = yaml.safe_dump(
                data, allow_unicode=True, sort_keys=False,
                default_flow_style=False)
            if normalized != original:
                backup = source.with_name(
                    source.stem + ".property_checker_input" + source.suffix)
                backup.write_text(original, encoding="utf-8")
                source.write_text(normalized, encoding="utf-8")
                backup_file = str(backup)
        property_count = len(properties)
        spec_derived = data.get("generation_source") == "spec"
        names = set()
        cdc_names = cdc_reference_names(model)
        for index, prop in enumerate(properties):
            location = f"$.sva_properties[{index}]"
            if not isinstance(prop, dict):
                errors.append(_error("PROPERTY_SCHEMA_INVALID", location,
                                     "property must be a mapping"))
                continue
            name = prop.get("name")
            if not isinstance(name, str) or not _IDENTIFIER.fullmatch(name):
                errors.append(_error(
                    "PROPERTY_NAME_INVALID", location + ".name",
                    f"property name must be a legal identifier, got {name!r}"))
            elif name in names:
                errors.append(_error("PROPERTY_NAME_DUPLICATE", location + ".name",
                                     f"duplicate property name {name!r}"))
            else:
                names.add(name)
            if spec_derived:
                _check_spec_traceability(prop, location, errors)
            if prop.get("category") not in _CATEGORIES:
                errors.append(_error(
                    "PROPERTY_CATEGORY_INVALID", location + ".category",
                    f"unsupported category {prop.get('category')!r}"))
            related_transitions, related_states = _scope_references(
                prop, location, model, cdc_names, errors)
            _check_clock(prop, location, model, related_transitions, errors)
            _check_trigger_and_expressions(prop, location, model, errors)
            _check_composite_fields(prop, location, model, errors)
            _check_temporal(prop, location, errors)
            _check_effect(prop, location, model, related_transitions, errors)
            _check_structural_bindings(
                prop, location, model, related_states, errors)
            _check_cdc(prop, location, model, errors)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError) as exc:
        errors.append(_error("PROPERTY_INPUT_ERROR", "$", str(exc)))

    report = {
        "checker": "property_ir_checker",
        "scope": (
            "Property IR schema, references, clock/reset ownership, CDC "
            "bindings, temporal ranges, and effects"),
        "status": ("failed" if errors else
                   "normalized" if normalizations else "passed"),
        "valid": not errors,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "hw_ir": str(Path(hw_ir_path).resolve()),
        "property_ir": str(source),
        "property_count": property_count,
        "generation_source": "spec" if spec_derived else "hw_ir",
        "normalization_count": len(normalizations),
        "normalizations_applied": bool(backup_file),
        "normalizations": normalizations,
        "backup_property_ir": backup_file,
        "error_count": len(errors),
        "errors": errors,
    }
    report_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    if errors and raise_on_error:
        raise PropertyIRCheckerError(report_output, errors)
    return report
