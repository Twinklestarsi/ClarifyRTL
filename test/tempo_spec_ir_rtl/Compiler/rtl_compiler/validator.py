import re
from collections import Counter, defaultdict

from .errors import IRValidationError
from .expressions import validate_action, validate_expression
from .parameters import evaluate_all


IDENTIFIER = re.compile(r"^[A-Za-z_][A-Za-z0-9_$]*$")
INTERNAL_SIGNAL_PREFIX = "__pn_"
STATE_TYPES = {
    "register", "counter", "pointer", "pipeline_register",
    "synchronizer_register", "memory", "ram",
}


def _duplicates(items):
    return sorted(name for name, count in Counter(items).items() if count > 1)


def _valid_width(width):
    if isinstance(width, int):
        return width > 0
    return bool(str(width).strip())


def _latency_cycles(value):
    if value is None:
        return 0
    if isinstance(value, int):
        return value
    match = re.fullmatch(r"\s*(\d+)\s*(?:cycles?)?\s*", str(value), re.I)
    return int(match.group(1)) if match else None


def validate_ir(model):
    errors = []
    warnings = model.warnings

    def identifier(name, label):
        if not name or not IDENTIFIER.match(name):
            errors.append(f"{label} has invalid Verilog identifier {name!r}")
        if name and str(name).startswith(INTERNAL_SIGNAL_PREFIX):
            errors.append(
                f"{label} {name!r} uses compiler-reserved prefix {INTERNAL_SIGNAL_PREFIX!r}")

    identifier(model.module_name, "module.name")
    for duplicate in _duplicates([x.name for x in model.parameters]):
        errors.append(f"duplicate parameter: {duplicate}")
    for item in model.parameters:
        identifier(item.name, "parameter")

    for duplicate in _duplicates([x.name for x in model.ports]):
        errors.append(f"duplicate port: {duplicate}")
    for port in model.ports:
        identifier(port.name, "port")
        if port.direction not in {"input", "output", "inout"}:
            errors.append(f"port {port.name}: direction must be input/output/inout")
        if not _valid_width(port.width):
            errors.append(f"port {port.name}: width must be positive/non-empty")

    ports = model.port_map()
    for duplicate in _duplicates([x.name for x in model.clock_domains]):
        errors.append(f"duplicate clock domain: {duplicate}")
    domains = model.domain_map()
    for domain in model.clock_domains:
        identifier(domain.name, "clock domain")
        if domain.clock not in ports or ports[domain.clock].direction != "input":
            errors.append(f"clock domain {domain.name}: clock {domain.clock!r} is not an input port")
        if domain.edge not in {"posedge", "negedge"}:
            errors.append(f"clock domain {domain.name}: edge must be posedge or negedge")
        if domain.reset and (domain.reset not in ports or ports[domain.reset].direction != "input"):
            errors.append(f"clock domain {domain.name}: reset {domain.reset!r} is not an input port")
        if domain.reset_type not in {"synchronous", "asynchronous", "sync", "async"}:
            errors.append(f"clock domain {domain.name}: invalid reset_type {domain.reset_type!r}")
        if domain.reset_polarity not in {"active_high", "active_low", "high", "low"}:
            errors.append(f"clock domain {domain.name}: invalid reset_polarity {domain.reset_polarity!r}")

    for duplicate in _duplicates([x.name for x in model.states]):
        errors.append(f"duplicate state: {duplicate}")
    states = model.state_map()
    for state in model.states:
        identifier(state.name, "state")
        if state.name in ports and ports[state.name].direction != "output":
            errors.append(f"state {state.name}: same-name port must be an output")
        if state.type not in STATE_TYPES:
            errors.append(f"state {state.name}: unsupported type {state.type!r}")
        if state.clock_domain not in domains:
            errors.append(f"state {state.name}: unknown clock domain {state.clock_domain!r}")
        if not _valid_width(state.width):
            errors.append(f"state {state.name}: width must be positive/non-empty")
        if state.type in {"memory", "ram"} and not state.depth:
            resource = next((item for item in model.resources
                             if str(item.get("name", "")) == state.name), None)
            parameters = resource.get("parameters", {}) if resource else {}
            resource_depth = (resource or {}).get(
                "depth", parameters.get("depth", parameters.get("DEPTH")))
            if not resource_depth:
                errors.append(f"memory state {state.name}: depth is required")
        if (state.type in {"memory", "ram"} and state.initial_value is not None
                and state.depth is None):
            errors.append(
                f"memory state {state.name}: scalar initial_value is unsupported; use explicit indexed initialization")
        domain = domains.get(state.clock_domain)
        if domain and domain.states and state.name not in domain.states:
            errors.append(
                f"state {state.name}: missing from clock domain {domain.name}.states")
    for domain in model.clock_domains:
        for state_name in domain.states:
            if state_name not in states:
                errors.append(f"clock domain {domain.name}: unknown owned state {state_name!r}")
            elif states[state_name].clock_domain != domain.name:
                errors.append(
                    f"clock domain {domain.name}: state {state_name} belongs to another domain")

    derived_names = []
    for signal in model.derived_signals:
        name = str(signal.get("name", "")).strip()
        identifier(name, "derived signal")
        derived_names.append(name)
        if signal.get("expression") is None:
            errors.append(f"derived signal {name}: expression is required")
        else:
            expression = str(signal.get("expression", "")).strip()
            assignment = re.match(
                r"^\s*([A-Za-z_][A-Za-z0-9_$]*)\s*(<=|=(?!=))\s*(.+)$",
                expression, re.S)
            # ``<=`` is also less-than-or-equal.  It is an assignment prefix
            # only when the left-hand side is the signal's own name.
            is_assignment = bool(assignment) and (
                assignment.group(2) == "=" or assignment.group(1) == name)
            if is_assignment and assignment.group(1) == name:
                warnings.append(
                    f"derived signal {name}: removed redundant '{name} =' from expression")
            elif is_assignment:
                errors.append(
                    f"derived signal {name}: expression must be RHS-only, not assignment to {assignment.group(1)}")
        if name in states:
            errors.append(f"derived signal {name}: name collides with a state")
        if name in ports and ports[name].direction != "output":
            errors.append(f"derived signal {name}: same-name port must be an output")
    for duplicate in _duplicates(derived_names):
        errors.append(f"duplicate derived signal: {duplicate}")

    transitions = model.transition_map()
    for duplicate in _duplicates([x.name for x in model.transitions]):
        errors.append(f"duplicate transition: {duplicate}")
    sequential_writers = defaultdict(list)
    combinational_writers = defaultdict(list)
    for transition in model.transitions:
        identifier(transition.name, "transition")
        if transition.transition_type not in {"sequential", "combinational"}:
            errors.append(f"transition {transition.name}: invalid transition_type")
        expected = "non_blocking" if transition.transition_type == "sequential" else "blocking"
        if transition.update_semantics != expected:
            errors.append(
                f"transition {transition.name}: {transition.transition_type} requires update_semantics={expected}")
        if transition.transition_type == "sequential" and transition.clock_domain not in domains:
            errors.append(f"transition {transition.name}: unknown clock domain {transition.clock_domain!r}")
        if transition.transition_type == "combinational" and transition.clock_domain:
            errors.append(f"transition {transition.name}: combinational transition must not have a clock domain")
        if transition.trigger.strip().lower() == "initial":
            errors.append(
                f"transition {transition.name}: trigger initial must be represented by state.initial_value")
        if not transition.guard:
            errors.append(f"transition {transition.name}: guard is required")
        if not transition.actions:
            errors.append(f"transition {transition.name}: action is required")
        latency = _latency_cycles(transition.latency)
        if latency is None:
            errors.append(f"transition {transition.name}: latency must be an integer cycle count")
        elif transition.transition_type == "combinational" and latency != 0:
            errors.append(f"transition {transition.name}: combinational latency must be 0")
        elif transition.transition_type == "sequential" and latency not in {0, 1}:
            errors.append(
                f"transition {transition.name}: latency {latency} must be expanded into explicit pipeline states")
        # Scalar states conflict by exact name; indexed memory writes conflict
        # only when the complete lvalue is repeated.  Different addresses such
        # as mem[0] and mem[1] are valid updates in one transition.
        action_targets = [a.target for a in transition.actions]
        if len(action_targets) != len(set(action_targets)):
            duplicate_targets = _duplicates(action_targets)
            errors.append(
                f"transition {transition.name}: target assigned more than once: {duplicate_targets}")
        for action in transition.actions + transition.else_actions:
            target = action.base_target
            if transition.transition_type == "sequential":
                if target not in states:
                    errors.append(
                        f"transition {transition.name}: sequential target {target!r} is not a declared state")
                elif states[target].clock_domain != transition.clock_domain:
                    errors.append(
                        f"transition {transition.name}: target {target} belongs to {states[target].clock_domain}, not {transition.clock_domain}")
                sequential_writers[target].append(transition.name)
            else:
                if target not in ports and target not in derived_names:
                    errors.append(
                        f"transition {transition.name}: combinational target {target!r} is not an output/derived signal")
                elif target in ports and ports[target].direction != "output":
                    errors.append(f"transition {transition.name}: cannot drive input port {target}")
                combinational_writers[target].append(transition.name)
        if transition.transition_type == "combinational" and transition.guard not in {"1", "1'b1", "true", "True"}:
            action_targets = {a.base_target for a in transition.actions}
            else_targets = {a.base_target for a in transition.else_actions}
            if action_targets != else_targets:
                errors.append(
                    f"transition {transition.name}: guarded combinational action requires matching else_action targets")

    derived_direct = {str(x.get("name", "")).strip() for x in model.derived_signals}
    for target, writers in combinational_writers.items():
        if len(set(writers)) > 1:
            errors.append(f"combinational target {target}: multiple transition drivers {sorted(set(writers))}")
        if target in derived_direct:
            errors.append(f"derived signal {target}: driven by both expression and transition")

    process_membership = {}
    for process in model.processes:
        identifier(process.name, "sequential process")
        if process.clock_domain not in domains:
            errors.append(f"process {process.name}: unknown clock domain {process.clock_domain!r}")
        for name in process.transitions:
            if name not in transitions:
                errors.append(f"process {process.name}: unknown transition {name!r}")
                continue
            if transitions[name].transition_type != "sequential":
                errors.append(f"process {process.name}: transition {name} is not sequential")
            if transitions[name].clock_domain != process.clock_domain:
                errors.append(f"process {process.name}: transition {name} uses another clock domain")
            if name in process_membership:
                errors.append(f"transition {name}: listed in multiple sequential processes")
            process_membership[name] = process.name

    priority_edges = set(model.priorities)
    for high, low in priority_edges:
        if high not in transitions or low not in transitions:
            errors.append(f"priority references unknown transition: {high} > {low}")
        elif transitions[high].clock_domain != transitions[low].clock_domain:
            errors.append(f"priority crosses clock domains: {high} > {low}")

    # Process order is an explicit high-to-low order.  Conflicting writers not
    # in a common process require a top-level priority relation.
    process_order = {}
    for process in model.processes:
        for index, name in enumerate(process.transitions):
            process_order[name] = (process.name, index)
    for target, writers in sequential_writers.items():
        unique = list(dict.fromkeys(writers))
        memberships = {
            process_membership.get(name, f"__auto_{transitions[name].clock_domain}")
            for name in unique
        }
        if len(memberships) > 1:
            errors.append(
                f"state {target}: writers span multiple RTL processes {sorted(memberships)}")
        with_else = [name for name in unique if any(
            action.base_target == target for action in transitions[name].else_actions)]
        if with_else and len(unique) > 1:
            errors.append(
                f"state {target}: transition-local else_action conflicts with other writers {unique}")
        for i, left in enumerate(unique):
            for right in unique[i + 1:]:
                in_order = (left in process_order and right in process_order and
                            process_order[left][0] == process_order[right][0])
                explicit = (left, right) in priority_edges or (right, left) in priority_edges
                if not in_order and not explicit:
                    errors.append(
                        f"state {target}: transitions {left} and {right} need common process order or explicit priority")

    if model.priorities:
        nodes = {name for pair in model.priorities for name in pair}
        incoming = {name: 0 for name in nodes}
        outgoing = defaultdict(list)
        for high, low in model.priorities:
            if high in nodes and low in nodes:
                outgoing[high].append(low)
                incoming[low] += 1
        ready = [name for name, count in incoming.items() if count == 0]
        visited = 0
        while ready:
            name = ready.pop()
            visited += 1
            for lower in outgoing[name]:
                incoming[lower] -= 1
                if incoming[lower] == 0:
                    ready.append(lower)
        if visited != len(nodes):
            errors.append("priority relations contain a cycle")

    for resource in model.resources:
        name = str(resource.get("name", "")).strip()
        kind = str(resource.get("type", "")).lower()
        identifier(name, "resource")
        parameters = resource.get("parameters", {}) or {}
        if kind in {"memory", "ram", "register_file", "register file"}:
            width = resource.get("width", parameters.get("width", parameters.get("WIDTH")))
            depth = resource.get("depth", parameters.get("depth", parameters.get("DEPTH")))
            if not width and name not in states:
                errors.append(f"memory resource {name}: width is required")
            if not depth and (name not in states or not states[name].depth):
                errors.append(f"memory resource {name}: depth is required")
            if not resource.get("ports"):
                errors.append(f"memory resource {name}: ports are required")
        if kind in {"submodule", "module", "instance"} and not resource.get(
                "implementation", resource.get("module")):
            errors.append(f"submodule resource {name}: implementation/module is required")

    known_signals = set(ports) | set(states) | set(derived_names)
    cdc_register_drivers = set()
    for index, crossing in enumerate(model.cdc):
        source = str(crossing.get("source", "")).strip()
        destination = crossing.get(
            "destination_clock_domain", crossing.get("destination"))
        if isinstance(destination, dict):
            destination = destination.get(
                "clock_domain", destination.get("domain", destination.get("name")))
        destination = str(destination or "").strip()
        synchronizer = crossing.get("synchronizer", {}) or {}
        stages = synchronizer.get("stages", crossing.get("stages", 2))
        registers = synchronizer.get("registers", crossing.get("registers", [])) or []
        if isinstance(registers, str):
            registers = [registers]
        if source not in known_signals:
            errors.append(f"cdc[{index}]: unknown source signal {source!r}")
        if destination not in domains:
            errors.append(f"cdc[{index}]: unknown destination clock domain {destination!r}")
        try:
            stage_count = int(stages)
            if stage_count < 1:
                raise ValueError
        except (TypeError, ValueError):
            errors.append(f"cdc[{index}]: stages must be a positive integer")
            stage_count = 0
        if registers and len(registers) != stage_count:
            errors.append(f"cdc[{index}]: register count must equal stages")
        for register in registers:
            register = str(register)
            identifier(register, f"cdc[{index}] register")
            if register in cdc_register_drivers:
                errors.append(f"cdc register {register}: driven by multiple crossings")
            cdc_register_drivers.add(register)
            if register in sequential_writers:
                errors.append(f"cdc register {register}: also driven by a transition")

    for state in model.states:
        if state.name not in sequential_writers and state.name not in cdc_register_drivers:
            warnings.append(f"state {state.name}: declared but has no update driver")

    # ---- expression and parameter checks ---------------------------------
    def _width(value):
        try:
            number = int(str(value).strip())
            return number if number > 0 else None
        except (TypeError, ValueError):
            return None

    declared = {}
    for port in model.ports:
        width = _width(port.width)
        if width:
            declared[port.name] = width
    for state in model.states:
        width = _width(state.width)
        if width:
            declared[state.name] = width
    memories = {state.name for state in model.states
                if str(state.type).lower() in {"memory", "ram"}}

    raw_ports = (model.raw.get("module") or {}).get("ports") or []
    for index, entry in enumerate(raw_ports):
        if isinstance(entry, dict) and (entry.get("msb") is not None or entry.get("lsb") is not None):
            warnings.append(
                f"module.ports[{index}]: msb/lsb are ignored; the port is emitted "
                "as [width-1:0]")

    known_names = ({port.name for port in model.ports}
                   | {state.name for state in model.states}
                   | {str(s.get("name")) for s in model.derived_signals if isinstance(s, dict)}
                   | {parameter.name for parameter in model.parameters})
    for signal in model.derived_signals:
        name = str(signal.get("name", ""))
        expression = str(signal.get("expression", ""))
        # backend._derived_expression() strips a redundant "name =" prefix, so
        # validate the same text it will emit.
        prefix = re.match(r"^\s*([A-Za-z_][A-Za-z0-9_$]*)\s*(?:<=|=(?!=))\s*(.+)$",
                          expression, re.S)
        if prefix and prefix.group(1) == name:
            expression = prefix.group(2)
        errors.extend(validate_expression(
            expression, widths=declared, memories=memories, declared=known_names,
            where=f"derived signal {name}"))

    transition_map = model.transition_map()
    for transition in model.transitions:
        errors.extend(validate_expression(
            transition.guard, widths=declared, memories=memories, declared=known_names,
            where=f"transition {transition.name} guard"))
        for kind, actions in (("action", transition.actions),
                              ("else_action", transition.else_actions)):
            for position, action in enumerate(actions):
                errors.extend(validate_action(
                    action.target, action.expression, widths=declared,
                    memories=memories, known=known_names,
                    where=f"transition {transition.name} {kind}[{position}]"))

    parameter_errors = evaluate_all({p.name: p.value for p in model.parameters})[1]
    # Parameters may be string/system constants; evaluation is advisory.
    warnings.extend(parameter_errors)

    if errors:
        raise IRValidationError(errors)
    return warnings
