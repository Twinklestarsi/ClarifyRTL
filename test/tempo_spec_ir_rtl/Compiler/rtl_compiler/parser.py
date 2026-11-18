import json
import re
from pathlib import Path

import yaml

from .errors import IRParseError
from .model import (Assignment, ClockDomain, IRModel, Parameter, Port,
                    SequentialProcess, State, Transition)
from .elaborate import elaborate, ElaborationError
from .schema import check_keys


def _items(value, label):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        result = []
        for name, body in value.items():
            if isinstance(body, dict):
                result.append({"name": name, **body})
            else:
                result.append({"name": name, "value": body})
        return result
    raise IRParseError(f"{label} must be a list or mapping")


def _string(value, default=""):
    if value is None:
        return default
    if isinstance(value, bool):
        return "1'b1" if value else "1'b0"
    return str(value).strip()


def _parameters(value):
    result = []
    if isinstance(value, dict):
        for name, body in value.items():
            if isinstance(body, dict):
                body = body.get("value", body.get("default", 0))
            result.append(Parameter(str(name), _string(body)))
        return result
    for item in _items(value, "module.parameters"):
        if isinstance(item, str):
            result.append(Parameter(_string(item), "0"))
            continue
        if not isinstance(item, dict):
            raise IRParseError("module.parameters list items must be names or mappings")
        result.append(Parameter(_string(item.get("name")), _string(
            item.get("value", item.get("default", 0)))))
    return result


def _ports(value):
    result = []
    for item in _items(value, "module.ports"):
        direction = _string(item.get("direction", item.get("dir"))).lower()
        datatype = _string(item.get("datatype", item.get("type", "wire"))).lower()
        explicit_signed = item.get("signed", False)
        if isinstance(explicit_signed, str):
            explicit_signed = explicit_signed.strip().lower() in {
                "1", "true", "yes", "signed"}
        signed = bool(explicit_signed) or (
            "signed" in datatype and "unsigned" not in datatype)
        result.append(Port(
            name=_string(item.get("name")), direction=direction,
            width=item.get("width", 1), datatype=datatype, signed=signed))
    return result


def _clock_domains(value):
    result = []
    for item in _items(value, "clock_domains"):
        reset = item.get("reset")
        if isinstance(reset, dict):
            reset_name = reset.get("signal", reset.get("name"))
            reset_type = reset.get("type", item.get("reset_type", "synchronous"))
            polarity = reset.get("polarity", item.get("reset_polarity"))
        else:
            reset_name = reset
            reset_type = item.get("reset_type", "synchronous")
            polarity = item.get("reset_polarity")
        reset_name = _string(reset_name) or None
        if not polarity:
            polarity = "active_low" if reset_name and reset_name.lower().endswith("_n") else "active_high"
        result.append(ClockDomain(
            name=_string(item.get("name")),
            clock=_string(item.get("clock", item.get("clk"))),
            edge=_string(item.get("edge", "posedge")).lower(),
            reset=reset_name,
            reset_type=_string(reset_type, "synchronous").lower(),
            reset_polarity=_string(polarity).lower(),
            states=[_string(x) for x in item.get("states", [])]))
    return result


def _states(value):
    result = []
    for item in _items(value, "states"):
        result.append(State(
            name=_string(item.get("name")),
            type=_string(item.get("type", "register")).lower().replace(" ", "_"),
            width=item.get("width", 1),
            clock_domain=_string(item.get("clock_domain", item.get("domain"))),
            reset_value=_string(item.get("reset_value", 0)),
            datatype=_string(item.get("datatype", "unsigned")).lower(),
            depth=item.get("depth"),
            initial_value=(_string(item.get("initial_value"))
                           if item.get("initial_value") is not None else None),
            initial_enable=(_string(item.get("initial_enable"))
                            if item.get("initial_enable") is not None else None),
            write_mask=(_string(item.get("write_mask"))
                        if item.get("write_mask") is not None else None)))
    return result


_ASSIGNMENT = re.compile(
    r"^\s*([A-Za-z_$][\w$]*(?:\s*\[.+\])?)\s*(?:<=|=)\s*(.+?)\s*;?\s*$")


def _actions(value, label):
    if value is None:
        return []
    if isinstance(value, str):
        chunks = [x for x in re.split(r";|\n", value) if x.strip()]
    elif isinstance(value, list):
        chunks = value
    elif isinstance(value, dict):
        if "target" in value or "lhs" in value or "state" in value:
            chunks = [value]
        else:
            chunks = [{"target": key, "expression": val}
                      for key, val in value.items()]
    else:
        raise IRParseError(f"{label} must be an assignment, list, or mapping")

    result = []
    for chunk in chunks:
        if isinstance(chunk, dict):
            target = chunk.get("target", chunk.get("lhs", chunk.get("state")))
            expression = chunk.get("expression", chunk.get("rhs", chunk.get("value")))
            if target is None or expression is None:
                raise IRParseError(f"{label} contains an incomplete assignment: {chunk}")
            result.append(Assignment(_string(target), _string(expression)))
            continue
        match = _ASSIGNMENT.match(_string(chunk))
        if not match:
            raise IRParseError(f"{label} is not in 'target = expression' form: {chunk}")
        result.append(Assignment(match.group(1).replace(" ", ""), match.group(2)))
    return result


def _transitions(value, warnings):
    result = []
    for item in _items(value, "transitions"):
        domain = _string(item.get("clock_domain", item.get("clock"))) or None
        kind = _string(item.get("transition_type", item.get("type"))).lower()
        if not kind:
            kind = "sequential" if domain else "combinational"
            warnings.append(f"transition {item.get('name')}: inferred transition_type={kind}")
        semantics = item.get("update_semantics", "")
        if isinstance(semantics, dict):
            semantics = semantics.get("type", "")
        semantics = _string(semantics).lower()
        if not semantics:
            semantics = "non_blocking" if kind == "sequential" else "blocking"
            warnings.append(f"transition {item.get('name')}: inferred update_semantics={semantics}")
        guard = item.get("guard", "1'b1")
        result.append(Transition(
            name=_string(item.get("name")), transition_type=kind,
            clock_domain=domain, guard=_string(guard, "1'b1"),
            actions=_actions(item.get("action", item.get("actions")),
                             f"transition {item.get('name')}.action"),
            else_actions=_actions(item.get("else_action", item.get("else_actions")),
                                  f"transition {item.get('name')}.else_action"),
            latency=item.get("latency", 0), update_semantics=semantics,
            trigger=_string(item.get("trigger"))))
    return result


def _processes(value):
    result = []
    for item in _items(value, "sequential_processes"):
        transitions = item.get("transitions", [])
        if isinstance(transitions, str):
            transitions = [transitions]
        result.append(SequentialProcess(
            name=_string(item.get("name")),
            clock_domain=_string(item.get("clock_domain", item.get("domain"))),
            transitions=[_string(x) for x in transitions]))
    return result


def _priorities(value):
    result = []
    if not value:
        return result
    entries = value if isinstance(value, list) else [value]
    for entry in entries:
        if isinstance(entry, dict) and "higher" in entry and "lower" in entry:
            high = entry["higher"]
            low = entry["lower"]
            high = high if isinstance(high, list) else [high]
            low = low if isinstance(low, list) else [low]
            result.extend((_string(h), _string(l)) for h in high for l in low)
        elif isinstance(entry, (list, tuple)):
            result.extend((_string(entry[i]), _string(entry[i + 1]))
                          for i in range(len(entry) - 1))
        else:
            raise IRParseError("priority entries require higher/lower fields")
    return result


def _generic_list(value, label):
    if value is None:
        return []
    if isinstance(value, list):
        return value
    if isinstance(value, dict):
        # A single CDC/resource normally has semantic keys rather than names.
        if any(key in value for key in ("source", "type", "implementation")):
            return [value]
        return _items(value, label)
    raise IRParseError(f"{label} must be a list or mapping")


def parse_ir(path):
    ir_path = Path(path)
    if not ir_path.is_file():
        raise IRParseError(f"IR file does not exist: {ir_path}")
    try:
        text = ir_path.read_text(encoding="utf-8")
        data = json.loads(text) if ir_path.suffix.lower() == ".json" else yaml.safe_load(text)
    except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise IRParseError(f"cannot parse {ir_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise IRParseError("IR document root must be a mapping")
    key_errors, key_warnings = check_keys(data)
    if key_errors:
        raise IRParseError("IR schema check failed:\n- " + "\n- ".join(key_errors))
    try:
        data = elaborate(data)
    except ElaborationError as exc:
        raise IRParseError(f"IR elaboration failed: {exc}") from exc
    module = data.get("module")
    if not isinstance(module, dict):
        raise IRParseError("module must be a mapping")
    warnings = list(key_warnings)
    states_value = data.get("states", data.get("places"))
    model = IRModel(
        module_name=_string(module.get("name")),
        parameters=_parameters(module.get("parameters")),
        ports=_ports(module.get("ports")),
        clock_domains=_clock_domains(data.get("clock_domains")),
        states=_states(states_value),
        derived_signals=_generic_list(data.get("derived_signals"), "derived_signals"),
        transitions=_transitions(data.get("transitions"), warnings),
        processes=_processes(data.get("sequential_processes")),
        priorities=_priorities(data.get("priority", data.get("priorities"))),
        resources=_generic_list(data.get("resources"), "resources"),
        cdc=_generic_list(data.get("cdc"), "cdc"),
        raw=data, warnings=warnings)
    return model
