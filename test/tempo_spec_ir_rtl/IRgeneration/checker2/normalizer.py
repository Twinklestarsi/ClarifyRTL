import copy
import re


ASSIGNMENT = re.compile(
    r"^\s*([A-Za-z_][A-Za-z0-9_$]*)\s*(?:<=|=(?!=))\s*(.+)$", re.S)
STATE_TYPE_ALIASES = {
    "stack_pointer": "pointer",
    "fifo_pointer": "pointer",
    "read_pointer": "pointer",
    "write_pointer": "pointer",
    "output_register": "register",
    "pipeline register": "pipeline_register",
    "pipeline_reg": "pipeline_register",
    "synchronizer register": "synchronizer_register",
    "sync_register": "synchronizer_register",
}
EDGE_ALIASES = {
    "rising": "posedge", "rising_edge": "posedge", "positive": "posedge",
    "falling": "negedge", "falling_edge": "negedge", "negative": "negedge",
}
RESET_TYPE_ALIASES = {
    "sync": "synchronous", "synchronous_reset": "synchronous",
    "async": "asynchronous", "asynchronous_reset": "asynchronous",
}
RESET_POLARITY_ALIASES = {
    "high": "active_high", "active-high": "active_high",
    "low": "active_low", "active-low": "active_low",
}
TRANSITION_TYPE_ALIASES = {
    "clocked": "sequential", "sequential_transition": "sequential",
    "comb": "combinational", "combinational_transition": "combinational",
}
SEMANTICS_ALIASES = {
    "nonblocking": "non_blocking", "non-blocking": "non_blocking",
    "blocking_assignment": "blocking", "blocking-assignment": "blocking",
}


def records(value, section):
    """Yield mutable records and stable paths from list/name-keyed mapping."""
    if isinstance(value, list):
        for index, item in enumerate(value):
            if isinstance(item, dict):
                yield item, f"$.{section}[{index}]"
    elif isinstance(value, dict):
        for name, item in value.items():
            if isinstance(item, dict):
                if not item.get("name"):
                    item["name"] = str(name)
                yield item, f"$.{section}.{name}"


def _record(repairs, repair_type, location, field, before, after, message):
    repairs.append({
        "repair_type": repair_type,
        "location": location,
        "field": field,
        "before": before,
        "after": after,
        "message": message,
    })


def _normalize_field(item, field, aliases, location, repairs):
    value = item.get(field)
    if value is None:
        return
    normalized = aliases.get(str(value).strip().lower())
    if normalized and normalized != value:
        item[field] = normalized
        _record(repairs, f"normalize_{field}", location, field, value,
                normalized, f"Normalized {field} alias")


def _ports(data):
    module = data.get("module", {})
    return {str(item.get("name")): item
            for item, _ in records(module.get("ports", {}), "module.ports")}


def _states(data):
    value = data.get("states", data.get("places", {}))
    return {str(item.get("name")): item
            for item, _ in records(value, "states")}


def _derived_records(data):
    return list(records(data.get("derived_signals", {}), "derived_signals"))


def _add_derived(data, signal):
    derived = data.get("derived_signals")
    if derived is None:
        data["derived_signals"] = [signal]
    elif isinstance(derived, list):
        derived.append(signal)
    elif isinstance(derived, dict):
        name = signal.pop("name")
        derived[name] = signal


def _transformations(value, location="$.data_model"):
    if isinstance(value, dict):
        if "transformation" in value:
            transformation = value["transformation"]
            values = transformation if isinstance(transformation, list) else [transformation]
            for index, item in enumerate(values):
                if isinstance(item, str):
                    yield item, f"{location}.transformation[{index}]"
        for key, child in value.items():
            if key != "transformation":
                yield from _transformations(child, f"{location}.{key}")
    elif isinstance(value, list):
        for index, child in enumerate(value):
            yield from _transformations(child, f"{location}[{index}]")


def _dependencies(expression, known):
    identifiers = re.findall(r"\b[A-Za-z_][A-Za-z0-9_$]*\b", expression)
    return list(dict.fromkeys(name for name in identifiers if name in known))


def _prune_unused_derived(data, ports, repairs):
    derived = data.get("derived_signals")
    if not isinstance(derived, list):
        return
    by_name = {str(item.get("name")): item for item in derived
               if isinstance(item, dict) and item.get("name")}
    names = set(by_name)
    live = {name for name in names
            if name in ports and
            str(ports[name].get("direction", "")).lower() == "output"}
    searchable = []
    for transition, _ in records(data.get("transitions", {}), "transitions"):
        searchable.append(str(transition.get("guard", "")))
        searchable.append(str(transition.get("action", transition.get("actions", ""))))
        searchable.append(str(transition.get("else_action", transition.get("else_actions", ""))))
    searchable.append(str(data.get("resources", "")))
    for text in searchable:
        live.update(_dependencies(text, names))
    pending = list(live)
    while pending:
        name = pending.pop()
        item = by_name.get(name, {})
        dependencies = item.get("dependencies", []) or []
        if isinstance(dependencies, str):
            dependencies = [dependencies]
        dependencies = set(map(str, dependencies)) | set(
            _dependencies(str(item.get("expression", "")), names))
        for dependency in dependencies & names:
            if dependency not in live:
                live.add(dependency)
                pending.append(dependency)
    removed = [item for item in derived
               if isinstance(item, dict) and str(item.get("name")) not in live]
    if removed:
        data["derived_signals"] = [item for item in derived if item not in removed]
        _record(repairs, "remove_unused_derived_signals", "$.derived_signals",
                "derived_signals", [item.get("name") for item in removed], None,
                "Removed derived signals outside the output/state-update dependency closure")


def _materialize_memory_states(data, states, repairs):
    resources = data.get("resources", []) or []
    if not isinstance(resources, list):
        return
    transitions = list(records(data.get("transitions", {}), "transitions"))
    state_value = data.get("states")
    if not isinstance(state_value, list):
        return
    for index, resource in enumerate(resources):
        if not isinstance(resource, dict):
            continue
        name = str(resource.get("name", ""))
        kind = str(resource.get("type", "")).lower()
        if not name or kind not in {"memory", "ram"}:
            continue
        parameters = resource.get("parameters", {}) or {}
        if isinstance(parameters, list):
            mapped = {}
            for parameter in parameters:
                if isinstance(parameter, dict) and parameter.get("name") is not None:
                    mapped[str(parameter["name"])] = parameter.get(
                        "value", parameter.get("default", 0))
            resource["parameters"] = mapped
            parameters = mapped
            _record(repairs, "normalize_resource_parameters",
                    f"$.resources[{index}]", "parameters", None, mapped,
                    "Converted resource parameter records to a name/value mapping")
        writer_domain = None
        for transition, _ in transitions:
            actions = transition.get("action", transition.get("actions", []))
            actions = actions if isinstance(actions, list) else [actions]
            if any(isinstance(action, str) and
                   re.match(rf"^\s*{re.escape(name)}\s*\[", action)
                   for action in actions):
                writer_domain = transition.get("clock_domain")
                break
        if not writer_domain:
            continue
        width = resource.get("width", parameters.get("width", parameters.get("WIDTH")))
        depth = resource.get("depth", parameters.get("depth", parameters.get("DEPTH")))
        if name in states:
            state = states[name]
            before = dict(state)
            if not state.get("width"):
                state["width"] = width
            if not state.get("depth"):
                state["depth"] = depth
            if str(state.get("clock_domain", "")).lower() in {"", "none", "null"}:
                state["clock_domain"] = writer_domain
            if state != before:
                _record(repairs, "complete_memory_state",
                        f"$.states.{name}", "memory", before, dict(state),
                        "Completed memory state from RAM resource and writer domain")
            continue
        state = {
            "name": name, "type": kind,
            "width": width, "depth": depth,
            "clock_domain": writer_domain, "reset_value": 0,
            "datatype": "logic",
        }
        state_value.append(state)
        states[name] = state
        _record(repairs, "materialize_memory_state", f"$.resources[{index}]",
                "states", None, state,
                "Created memory state from complete RAM resource and writer domain")

    # Some frontends describe the physical RAM resource and the compiler state
    # with different architectural names (for example ``dual_port_ram`` and
    # ``ram_mem``).  When both sides are unique, their width/depth relationship
    # is unambiguous and can be completed without guessing a design mapping.
    memory_states = [
        state for state in states.values()
        if str(state.get("type", "")).lower() in {"memory", "ram"}
    ]
    memory_resources = [
        resource for resource in resources
        if isinstance(resource, dict) and
        str(resource.get("type", "")).lower() in {"memory", "ram"}
    ]
    if len(memory_states) == 1 and len(memory_resources) == 1:
        state = memory_states[0]
        resource = memory_resources[0]
        parameters = resource.get("parameters", {}) or {}
        if isinstance(parameters, list):
            parameters = {
                str(item["name"]): item.get("value", item.get("default"))
                for item in parameters
                if isinstance(item, dict) and item.get("name") is not None
            }
        width = resource.get(
            "width", parameters.get("width", parameters.get("WIDTH")))
        depth = resource.get(
            "depth", parameters.get("depth", parameters.get("DEPTH")))
        before = dict(state)
        if not state.get("width") and width is not None:
            state["width"] = width
        if not state.get("depth") and depth is not None:
            state["depth"] = depth
        if state != before:
            _record(
                repairs, "complete_unique_memory_state", "$.states",
                "memory", before, dict(state),
                "Completed the unique memory state from the unique RAM resource")


def _materialize_synchronous_ram_reads(data, states, repairs):
    """Turn RAM read outputs described as clocked ports into real registers.

    Frontends sometimes preserve the RAM resource's read clock/enable ports but
    also emit its output as a combinational memory lookup.  That changes the
    observable latency of a synchronous dual-port RAM.  When all three pieces
    are present, retain the lookup expression but perform it in the read clock
    domain under the resource's read enable.
    """
    resources = data.get("resources")
    state_value = data.get("states")
    derived_value = data.get("derived_signals")
    transition_value = data.get("transitions")
    if not all(isinstance(value, list) for value in (
            resources, state_value, derived_value, transition_value)):
        return

    domains = list(records(data.get("clock_domains", {}), "clock_domains"))
    derived_by_name = {
        str(item.get("name")): (item, location)
        for item, location in records(derived_value, "derived_signals")}
    existing_transitions = {
        str(item.get("name")) for item, _ in records(transition_value, "transitions")}

    for resource_index, resource in enumerate(resources):
        if (not isinstance(resource, dict) or
                str(resource.get("type", "")).lower() not in {"memory", "ram"}):
            continue
        memory_name = str(resource.get("name", ""))
        resource_ports = [item for item in resource.get("ports", [])
                          if isinstance(item, dict) and item.get("name")]
        port_names = {str(item["name"]) for item in resource_ports}
        read_clock = next((name for name in port_names
                           if re.fullmatch(r"(?:rclk|rd_clk|read_clk|read_clock)",
                                           name, re.I)), None)
        read_enable = next((name for name in port_names
                            if re.fullmatch(r"(?:renc|ren|rd_en|read_en|read_enable)",
                                            name, re.I)), None)
        read_outputs = [str(item["name"]) for item in resource_ports
                        if str(item.get("direction", "")).lower() == "output"]
        if not read_clock or not read_enable:
            continue
        domain = next((item for item, _ in domains
                       if str(item.get("clock")) == read_clock), None)
        if not domain:
            continue
        domain_name = str(domain.get("name"))

        for output_name in read_outputs:
            derived_entry = derived_by_name.get(output_name)
            if not derived_entry or output_name in states:
                continue
            derived, derived_location = derived_entry
            expression = str(derived.get("expression", "")).strip()
            if not re.match(rf"^{re.escape(memory_name)}\s*\[", expression):
                continue
            state = {
                "name": output_name,
                "type": "output_register",
                "width": derived.get("width", 1),
                "clock_domain": domain_name,
                "reset_value": 0,
                "datatype": "logic",
            }
            state_value.append(state)
            states[output_name] = state
            derived_value.remove(derived)
            transition_name = f"{output_name}_ram_read"
            suffix = 2
            while transition_name in existing_transitions:
                transition_name = f"{output_name}_ram_read_{suffix}"
                suffix += 1
            existing_transitions.add(transition_name)
            transition = {
                "name": transition_name,
                "clock_domain": domain_name,
                "trigger": f"posedge {read_clock}",
                "guard": read_enable,
                "action": f"{output_name} = {expression}",
                "latency": 0,
                "transition_type": "sequential",
                "update_semantics": "non_blocking",
            }
            transition_value.append(transition)
            processes = data.get("sequential_processes")
            if isinstance(processes, list):
                process = next((item for item in processes
                                if isinstance(item, dict) and
                                str(item.get("clock_domain")) == domain_name), None)
                if process is not None and isinstance(process.get("transitions"), list):
                    process["transitions"].append(transition_name)
            _record(repairs, "materialize_synchronous_ram_read", derived_location,
                    "derived_signal", derived, state,
                    f"Registered {output_name} on {read_clock} when {read_enable} is asserted")


def _normalize_parameterized_async_fifo(data, states, repairs):
    """Repair the canonical two-clock FIFO topology when examples got hard-coded."""
    parameters = data.get("module", {}).get("parameters", [])
    parameter_names = {str(item.get("name")) for item, _ in records(
        parameters, "module.parameters")}
    required_states = {
        "waddr_bin", "raddr_bin", "wptr", "rptr",
        "wptr_buff", "rptr_buff", "wptr_syn", "rptr_syn",
    }
    if not {"DEPTH", "WIDTH"}.issubset(parameter_names):
        return
    if not required_states.issubset(states):
        return
    domains = {str(item.get("name")): item for item, _ in records(
        data.get("clock_domains", {}), "clock_domains")}
    write_domain = next((name for name, item in domains.items()
                         if str(item.get("clock")) == "wclk"), None)
    read_domain = next((name for name, item in domains.items()
                        if str(item.get("clock")) == "rclk"), None)
    if not write_domain or not read_domain:
        return

    pointer_width = "$clog2(DEPTH) + 1"
    for name in required_states:
        state = states[name]
        if str(state.get("width")) != pointer_width:
            before = state.get("width")
            state["width"] = pointer_width
            _record(repairs, "parameterize_fifo_pointer_width", f"$.states.{name}",
                    "width", before, pointer_width,
                    "Derived FIFO pointer width from DEPTH instead of an example constant")

    target_domains = {
        "waddr_bin": write_domain, "wptr": write_domain,
        "rptr_buff": write_domain, "rptr_syn": write_domain,
        "raddr_bin": read_domain, "rptr": read_domain,
        "wptr_buff": read_domain, "wptr_syn": read_domain,
    }
    for name, domain_name in target_domains.items():
        state = states[name]
        if str(state.get("clock_domain")) != domain_name:
            before = state.get("clock_domain")
            state["clock_domain"] = domain_name
            _record(repairs, "place_fifo_synchronizer_stage", f"$.states.{name}",
                    "clock_domain", before, domain_name,
                    "Placed both pointer synchronizer stages in the receiving clock domain")
        for domain in domains.values():
            owned = domain.get("states")
            if isinstance(owned, list) and name in owned:
                owned.remove(name)
        owned = domains[domain_name].setdefault("states", [])
        if isinstance(owned, list) and name not in owned:
            owned.append(name)

    ports = _ports(data)
    for name in ("wdata", "rdata"):
        if name in ports and str(ports[name].get("width")) != "WIDTH":
            before = ports[name].get("width")
            ports[name]["width"] = "WIDTH"
            _record(repairs, "parameterize_fifo_data_width",
                    f"$.module.ports.{name}", "width", before, "WIDTH",
                    "Derived FIFO data width from WIDTH")
    for state in states.values():
        if str(state.get("type", "")).lower() in {"memory", "ram"}:
            state["width"] = "WIDTH"
            state["depth"] = "DEPTH"
    if "rdata_reg" in states:
        states["rdata_reg"]["width"] = "WIDTH"

    derived = {str(item.get("name")): item for item, _ in _derived_records(data)}
    address_width = "$clog2(DEPTH)"
    for prefix in ("w", "r"):
        name = f"{prefix}addr"
        signal = derived.get(name)
        if signal:
            signal["width"] = address_width
            signal["expression"] = f"{prefix}addr_bin[$clog2(DEPTH)-1:0]"
    full = derived.get("wfull")
    if full:
        full["expression"] = (
            "(wptr == {~rptr_syn[$clog2(DEPTH):$clog2(DEPTH)-1], "
            "rptr_syn[$clog2(DEPTH)-2:0]})")

    transition_records = list(records(data.get("transitions", {}), "transitions"))
    for transition, location in transition_records:
        assignments = _initial_assignments(
            transition.get("action", transition.get("actions"))) or []
        targets = {target.split("[", 1)[0].strip() for target, _ in assignments}
        matching_states = targets & required_states
        if matching_states:
            target = next(iter(matching_states))
            desired_domain = target_domains[target]
            old_domain = domains.get(str(transition.get("clock_domain")), {})
            old_reset = str(old_domain.get("reset", ""))
            new_reset = str(domains[desired_domain].get("reset", ""))
            transition["clock_domain"] = desired_domain
            transition["trigger"] = (
                f"posedge {domains[desired_domain].get('clock')}")
            if old_reset and new_reset and old_reset != new_reset:
                transition["guard"] = re.sub(
                    rf"\b{re.escape(old_reset)}\b", new_reset,
                    str(transition.get("guard", "")))
        action_key = "action" if "action" in transition else "actions"
        action = transition.get(action_key)
        values = action if isinstance(action, list) else [action]
        changed = []
        for value in values:
            if isinstance(value, str):
                for memory_name, memory_state in states.items():
                    if str(memory_state.get("type", "")).lower() not in {"memory", "ram"}:
                        continue
                    value = re.sub(
                        rf"\b{re.escape(memory_name)}\s*\[\s*waddr_bin(?:\s*\[[^]]+\])?\s*\]",
                        f"{memory_name}[waddr]", value)
                    value = re.sub(
                        rf"\b{re.escape(memory_name)}\s*\[\s*raddr_bin(?:\s*\[[^]]+\])?\s*\]",
                        f"{memory_name}[raddr]", value)
            changed.append(value)
        transition[action_key] = changed if isinstance(action, list) else changed[0]

    transitions_by_name = {str(item.get("name")): item
                           for item, _ in transition_records}
    processes = data.get("sequential_processes")
    if isinstance(processes, list):
        displaced = []
        for process in processes:
            if not isinstance(process, dict) or not isinstance(process.get("transitions"), list):
                continue
            kept = []
            for name in process["transitions"]:
                transition = transitions_by_name.get(str(name))
                if transition and str(transition.get("clock_domain")) != str(process.get("clock_domain")):
                    displaced.append(str(name))
                else:
                    kept.append(name)
            process["transitions"] = kept
        for name in displaced:
            domain_name = str(transitions_by_name[name].get("clock_domain"))
            process = next((item for item in processes if isinstance(item, dict) and
                            str(item.get("clock_domain")) == domain_name), None)
            if process is not None:
                process.setdefault("transitions", []).append(name)


def _remove_duplicate_cdc_metadata(data, repairs):
    cdc = data.get("cdc")
    if not isinstance(cdc, (dict, list)) or not cdc:
        return
    transition_targets = set()
    for transition, _ in records(data.get("transitions", {}), "transitions"):
        assignments = _initial_assignments(
            transition.get("action", transition.get("actions"))) or []
        transition_targets.update(target for target, _ in assignments)
    registers = []
    cdc_entries = cdc if isinstance(cdc, list) else [cdc]
    for entry in cdc_entries:
        if not isinstance(entry, dict):
            continue
        for key, value in entry.items():
            if not str(key).lower().startswith("synchronizer") or not isinstance(value, dict):
                continue
            raw = value.get("registers", []) or []
            registers.extend(raw if isinstance(raw, list) else [raw])
    conflicts = sorted(set(map(str, registers)) & transition_targets)
    if conflicts:
        data["cdc"] = []
        _record(repairs, "remove_duplicate_cdc_metadata", "$.cdc", "cdc",
                cdc, [],
                f"Removed CDC metadata duplicated by explicit transition writers: {conflicts}")


def _normalize_cdc_entries(data, states, repairs):
    entries = []
    source_keys = []
    for key in list(data):
        if not re.fullmatch(r"cdc(?:_\d+)?", str(key)):
            continue
        value = data.get(key)
        if isinstance(value, dict) and value:
            entries.append(value)
            source_keys.append(key)
        elif isinstance(value, list):
            entries.extend(item for item in value if isinstance(item, dict))
            source_keys.append(key)
    if not entries:
        return
    nonempty_entries = []
    removed_empty = []
    for entry in entries:
        source = entry.get("source")
        destination = entry.get("destination")
        synchronizer = entry.get("synchronizer")
        empty_source = source is None or source == "" or source == [] or source == {}
        empty_destination = (
            destination is None or destination == "" or
            destination == [] or destination == {})
        empty_synchronizer = synchronizer is None or synchronizer == {} or (
            isinstance(synchronizer, dict) and
            synchronizer.get("stages") in {None, 0, "0"} and
            synchronizer.get("registers") in (None, ""))
        if isinstance(synchronizer, dict) and synchronizer.get("registers") == []:
            empty_synchronizer = (
                synchronizer.get("stages") in {None, 0, "0"} and
                not synchronizer.get("latency"))
        if empty_source and empty_destination and empty_synchronizer:
            removed_empty.append(entry)
        else:
            nonempty_entries.append(entry)
    if removed_empty:
        before = data.get("cdc")
        entries = nonempty_entries
        data["cdc"] = entries
        _record(repairs, "remove_empty_cdc_metadata", "$.cdc", "cdc",
                before, entries,
                "Removed placeholder CDC metadata with no crossing or synchronizer")
        if not entries:
            return
    changed = len(source_keys) > 1 or source_keys != ["cdc"] or isinstance(data.get("cdc"), dict)
    for entry in entries:
        source = str(entry.get("source", ""))
        candidate = f"{source}_syn"
        synchronizer = entry.get("synchronizer")
        if candidate in states and isinstance(synchronizer, dict):
            registers = synchronizer.get("registers", []) or []
            if isinstance(registers, str):
                registers = [registers]
            if registers and str(registers[-1]) != candidate:
                registers[-1] = candidate
                synchronizer["registers"] = registers
                changed = True
    if not changed:
        return
    before = {key: data.get(key) for key in source_keys}
    for key in source_keys:
        if key != "cdc":
            data.pop(key, None)
    data["cdc"] = entries
    _record(repairs, "normalize_cdc_entries", "$.cdc", "cdc",
            before, entries,
            "Merged numbered CDC entries and bound final stages to declared synchronizer states")


def _remove_whole_memory_reset_transitions(data, states, repairs):
    domains = {str(item.get("name")): item
               for item, _ in records(data.get("clock_domains", {}), "clock_domains")}
    transitions_value = data.get("transitions", [])
    removed = set()
    for transition, location in records(transitions_value, "transitions"):
        domain = domains.get(str(transition.get("clock_domain", "")))
        reset = str((domain or {}).get("reset", ""))
        guard = str(transition.get("guard", ""))
        if not reset or reset not in guard:
            continue
        assignments = _initial_assignments(
            transition.get("action", transition.get("actions")))
        if not assignments:
            continue
        if all(target in states and
               str(states[target].get("type", "")).lower() in {"memory", "ram"} and
               "[" not in target and expression.strip() in {"0", "'0"}
               for target, expression in assignments):
            name = str(transition.get("name", ""))
            removed.add(name)
            _record(repairs, "remove_whole_memory_reset", location,
                    "transition", transition, None,
                    "Removed unsupported scalar reset assignment to unpacked memory")
    if not removed:
        return
    if isinstance(transitions_value, list):
        data["transitions"] = [item for item in transitions_value
                               if not isinstance(item, dict) or
                               str(item.get("name", "")) not in removed]
    elif isinstance(transitions_value, dict):
        for key in list(transitions_value):
            item = transitions_value[key]
            name = item.get("name", key) if isinstance(item, dict) else key
            if str(name) in removed:
                del transitions_value[key]
    _remove_transition_references(data, removed)


def _initial_assignments(action):
    values = action if isinstance(action, list) else [action]
    assignments = []
    for value in values:
        if isinstance(value, str):
            match = ASSIGNMENT.match(value)
            if not match:
                return None
            assignments.append((match.group(1), match.group(2).strip()))
        elif isinstance(value, dict):
            if len(value) == 1:
                assignments.extend((str(target), str(expression))
                                   for target, expression in value.items())
            elif any(key in value for key in ("target", "lhs", "state")):
                target = value.get("target", value.get("lhs", value.get("state")))
                expression = value.get("expression", value.get("rhs", value.get("value")))
                if target is None or expression is None:
                    return None
                assignments.append((str(target), str(expression)))
            else:
                assignments.extend((str(target), str(expression))
                                   for target, expression in value.items())
        else:
            return None
    return assignments


def _normalize_mapping_actions(data, repairs):
    """Convert YAML assignment mappings into the compiler's textual form."""
    for transition, location in records(data.get("transitions", {}), "transitions"):
        field = "action" if "action" in transition else "actions"
        original = transition.get(field)
        values = original if isinstance(original, list) else [original]
        normalized = []
        changed = False
        for value in values:
            if not isinstance(value, dict):
                normalized.append(value)
                continue
            assignments = _initial_assignments(value)
            if not assignments:
                normalized.append(value)
                continue
            normalized.extend(f"{target} = {expression}"
                              for target, expression in assignments)
            changed = True
        if not changed:
            continue
        transition[field] = normalized if isinstance(original, list) else normalized[0]
        _record(repairs, "normalize_mapping_action", location, field,
                original, transition[field],
                "Converted YAML assignment mapping to textual hardware assignment")


def _assignment_target(value):
    if isinstance(value, str):
        match = ASSIGNMENT.match(value)
        return match.group(1) if match else None
    if isinstance(value, dict):
        target = value.get("target", value.get("lhs", value.get("state")))
        if target is not None:
            return str(target)
        if len(value) == 1:
            return str(next(iter(value)))
    return None


def _action_base_targets(transition):
    """Return state names written by one raw transition record."""
    targets = set()
    for field in ("action", "actions", "else_action", "else_actions"):
        if field not in transition:
            continue
        raw = transition.get(field)
        values = raw if isinstance(raw, list) else [raw]
        for value in values:
            target = _assignment_target(value)
            if target is None and isinstance(value, str):
                match = re.match(
                    r"^\s*([A-Za-z_][A-Za-z0-9_$]*)\s*(?:\[[^]]+\])?\s*"
                    r"(?:<=|=(?!=))", value, re.S)
                target = match.group(1) if match else None
            if target:
                targets.add(str(target).split("[", 1)[0])
    return targets


def _merge_conflicting_sequential_processes(data, repairs):
    """Merge same-domain process components with overlapping RTL drivers.

    Separate always blocks may coexist when their targets are disjoint.  If
    they share a transition or write target, however, the IR describes
    multiple procedural drivers.  Merging that connected component is a
    deterministic representation repair: all blocks have the same clock and
    reset domain, and declared process/transition order is preserved.
    """
    processes = data.get("sequential_processes")
    if not isinstance(processes, list) or len(processes) < 2:
        return
    transitions = {
        str(item.get("name")): item
        for item, _ in records(data.get("transitions", {}), "transitions")
        if item.get("name")
    }
    valid = [index for index, process in enumerate(processes)
             if isinstance(process, dict)]
    transition_names = {}
    targets = {}
    for index in valid:
        raw = processes[index].get("transitions", []) or []
        names = raw if isinstance(raw, list) else [raw]
        transition_names[index] = [str(name) for name in names]
        targets[index] = set()
        for name in transition_names[index]:
            transition = transitions.get(name)
            if transition:
                targets[index].update(_action_base_targets(transition))

    parent = {index: index for index in valid}

    def find(index):
        while parent[index] != index:
            parent[index] = parent[parent[index]]
            index = parent[index]
        return index

    def union(left, right):
        left_root, right_root = find(left), find(right)
        if left_root != right_root:
            parent[right_root] = left_root

    for position, left in enumerate(valid):
        left_domain = str(processes[left].get("clock_domain", ""))
        for right in valid[position + 1:]:
            if left_domain != str(processes[right].get("clock_domain", "")):
                continue
            shared_transition = bool(
                set(transition_names[left]) & set(transition_names[right]))
            shared_target = bool(targets[left] & targets[right])
            if shared_transition or shared_target:
                union(left, right)

    components = {}
    for index in valid:
        components.setdefault(find(index), []).append(index)
    merged_components = [indices for indices in components.values()
                         if len(indices) > 1]
    if not merged_components:
        return

    first_to_merged = {}
    removed = set()
    for indices in merged_components:
        indices.sort()
        first = indices[0]
        before = [copy.deepcopy(processes[index]) for index in indices]
        merged = copy.deepcopy(processes[first])
        merged_names = []
        for index in indices:
            for name in transition_names[index]:
                if name not in merged_names:
                    merged_names.append(name)
        merged["transitions"] = merged_names
        first_to_merged[first] = merged
        removed.update(indices[1:])
        _record(
            repairs, "merge_conflicting_sequential_processes",
            "$.sequential_processes", "sequential_processes", before,
            copy.deepcopy(merged),
            "Merged same-clock-domain processes sharing transitions or state "
            "write targets; preserved first-declared transition order")

    data["sequential_processes"] = [
        first_to_merged.get(index, process)
        for index, process in enumerate(processes)
        if index not in removed
    ]


def _deduplicate_nonblocking_actions(transition, location, repairs):
    field = "action" if "action" in transition else "actions"
    actions = transition.get(field)
    if not isinstance(actions, list):
        return
    targets = [_assignment_target(action) for action in actions]
    duplicates = {target for target in targets
                  if target is not None and targets.count(target) > 1}
    if not duplicates:
        return
    seen = set()
    kept_reversed = []
    for action, target in reversed(list(zip(actions, targets))):
        if target in duplicates and target in seen:
            continue
        kept_reversed.append(action)
        if target is not None:
            seen.add(target)
    normalized = list(reversed(kept_reversed))
    transition[field] = normalized
    _record(
        repairs, "deduplicate_nonblocking_actions", location, field,
        actions, normalized,
        "Kept the last assignment to each repeated lvalue, matching non-blocking assignment semantics")


def _strip_quoted_action_rhs(transition, location, repairs):
    field = "action" if "action" in transition else "actions"
    original = transition.get(field)
    is_list = isinstance(original, list)
    actions = original if is_list else [original]
    normalized = []
    changed = False
    pattern = re.compile(
        r"^(?P<prefix>\s*[A-Za-z_$][A-Za-z0-9_$]*(?:\[[^]]+\])?\s*"
        r"(?:<=|=(?!=))\s*)(?P<quote>[\"'])(?P<rhs>.*)(?P=quote)\s*$", re.S)
    for action in actions:
        if isinstance(action, str):
            match = pattern.match(action)
            if match:
                action = match.group("prefix") + match.group("rhs")
                changed = True
        normalized.append(action)
    if not changed:
        return
    transition[field] = normalized if is_list else normalized[0]
    _record(repairs, "strip_quoted_action_rhs", location, field,
            original, transition[field],
            "Removed redundant quotes around hardware assignment RHS")


def _remove_transition_references(data, removed_names):
    for process, _ in records(data.get("sequential_processes", {}),
                              "sequential_processes"):
        transitions = process.get("transitions")
        if isinstance(transitions, list):
            process["transitions"] = [name for name in transitions
                                      if str(name) not in removed_names]
        elif isinstance(transitions, str) and transitions in removed_names:
            process["transitions"] = []

    concurrency = data.get("concurrency")
    if isinstance(concurrency, dict) and isinstance(concurrency.get("groups"), list):
        groups = []
        for group in concurrency["groups"]:
            if isinstance(group, list):
                filtered = [name for name in group if str(name) not in removed_names]
                if filtered:
                    groups.append(filtered)
            elif str(group) not in removed_names:
                groups.append(group)
        concurrency["groups"] = groups

    priorities = data.get("priority", data.get("priorities"))
    if isinstance(priorities, list):
        filtered = []
        for entry in priorities:
            if isinstance(entry, dict):
                names = []
                for field in ("higher", "lower"):
                    value = entry.get(field, [])
                    names.extend(value if isinstance(value, list) else [value])
                if any(str(name) in removed_names for name in names):
                    continue
            elif isinstance(entry, list):
                entry = [name for name in entry if str(name) not in removed_names]
                if len(entry) < 2:
                    continue
            filtered.append(entry)
        if "priority" in data:
            data["priority"] = filtered
        else:
            data["priorities"] = filtered


def _migrate_initial_transitions(data, states, repairs, warnings):
    transitions_value = data.get("transitions", [])
    migrations = []
    for transition, location in list(records(transitions_value, "transitions")):
        trigger = str(transition.get("trigger", "")).strip().lower()
        if trigger != "initial":
            continue
        name = str(transition.get("name", ""))
        guard = transition.get("guard", "1'b1")
        if str(guard).strip().lower() not in {"1", "1'b1", "true"}:
            warnings.append({
                "location": location,
                "message": f"Cannot migrate guarded initial transition {name!r}",
            })
            continue
        assignments = _initial_assignments(
            transition.get("action", transition.get("actions")))
        if not assignments or any(target not in states for target, _ in assignments):
            warnings.append({
                "location": location,
                "message": f"Cannot migrate initial transition {name!r}: actions must target complete declared states",
            })
            continue
        conflict = False
        for target, expression in assignments:
            existing = states[target].get("initial_value")
            if existing is not None and str(existing) != expression:
                warnings.append({
                    "location": location,
                    "message": f"Cannot migrate {name!r}: state {target} already has a different initial_value",
                })
                conflict = True
        if conflict:
            continue
        migrations.append((name, location, assignments))

    removed_names = set()
    for name, location, assignments in migrations:
        for target, expression in assignments:
            states[target]["initial_value"] = expression
        removed_names.add(name)
        _record(
            repairs, "migrate_initial_transition", location, "trigger",
            {"transition": name, "trigger": "initial"},
            {target: {"initial_value": expression}
             for target, expression in assignments},
            "Moved explicit initialization from transition to state.initial_value")

    if removed_names:
        if isinstance(transitions_value, list):
            data["transitions"] = [
                transition for transition in transitions_value
                if not isinstance(transition, dict) or
                str(transition.get("name", "")) not in removed_names]
        elif isinstance(transitions_value, dict):
            for key in list(transitions_value):
                body = transitions_value[key]
                name = body.get("name", key) if isinstance(body, dict) else key
                if str(name) in removed_names:
                    del transitions_value[key]
        _remove_transition_references(data, removed_names)


def _synchronize_reset_values(data, states, repairs):
    domains = {str(item.get("name")): item
               for item, _ in records(data.get("clock_domains", {}), "clock_domains")}
    for transition, location in records(data.get("transitions", {}), "transitions"):
        domain = domains.get(str(transition.get("clock_domain", "")))
        if not domain or not domain.get("reset"):
            continue
        trigger = str(transition.get("trigger", "")).strip().lower()
        reset = str(domain.get("reset", "")).strip().lower()
        if trigger != "reset" and reset not in trigger.split():
            continue
        assignments = _initial_assignments(
            transition.get("action", transition.get("actions")))
        if not assignments:
            continue
        for target, expression in assignments:
            if target not in states:
                continue
            before = states[target].get("reset_value")
            if str(before) == expression:
                continue
            states[target]["reset_value"] = expression
            _record(repairs, "synchronize_reset_value", location,
                    f"states.{target}.reset_value", before, expression,
                    "Synchronized state reset_value from explicit reset transition")


def _remove_redundant_combinational_self_drivers(data, derived_by_name, repairs):
    transitions_value = data.get("transitions", [])
    removed = set()
    for transition, location in records(transitions_value, "transitions"):
        kind = str(transition.get(
            "transition_type", transition.get("type", ""))).lower()
        guard = str(transition.get("guard", "")).strip().lower()
        if kind != "combinational" or guard not in {"1", "1'b1", "true"}:
            continue
        assignments = _initial_assignments(
            transition.get("action", transition.get("actions")))
        if assignments and all(
                target in derived_by_name and (
                    target == expression or
                    str(derived_by_name[target].get("expression", "")).strip() ==
                    expression.strip())
                for target, expression in assignments):
            name = str(transition.get("name", ""))
            removed.add(name)
            _record(repairs, "remove_redundant_self_driver", location,
                    "transition", transition, None,
                    "Removed combinational assignment redundant with an equivalent derived driver")
    if not removed:
        return
    if isinstance(transitions_value, list):
        data["transitions"] = [item for item in transitions_value
                               if not isinstance(item, dict) or
                               str(item.get("name", "")) not in removed]
    elif isinstance(transitions_value, dict):
        for key in list(transitions_value):
            item = transitions_value[key]
            name = item.get("name", key) if isinstance(item, dict) else key
            if str(name) in removed:
                del transitions_value[key]
    _remove_transition_references(data, removed)


def normalize_ir(data):
    repairs = []
    warnings = []
    _normalize_mapping_actions(data, repairs)
    ports = _ports(data)
    states = _states(data)

    resources = data.get("resources")
    if isinstance(resources, list):
        kept = []
        for index, resource in enumerate(resources):
            inert_memory = (
                isinstance(resource, dict) and
                str(resource.get("type", "")).lower() in {"memory", "ram"} and
                str(resource.get("name", "")) not in states and
                str(resource.get("implementation", "")).lower() in {"", "none"} and
                not resource.get("ports") and not resource.get("parameters") and
                not resource.get("width") and not resource.get("depth"))
            if inert_memory:
                _record(repairs, "remove_inert_memory_resource",
                        f"$.resources[{index}]", "resources", resource, None,
                        "Removed unreferenced empty memory resource")
            else:
                kept.append(resource)
        data["resources"] = kept
    _materialize_memory_states(data, states, repairs)
    _materialize_synchronous_ram_reads(data, states, repairs)
    _normalize_parameterized_async_fifo(data, states, repairs)
    _normalize_cdc_entries(data, states, repairs)
    _remove_duplicate_cdc_metadata(data, repairs)

    state_value = data.get("states", data.get("places", {}))
    for state, location in records(state_value, "states"):
        _normalize_field(state, "type", STATE_TYPE_ALIASES, location, repairs)
        if "datatype" not in state:
            state["datatype"] = "logic"
            _record(repairs, "add_default_datatype", location, "datatype",
                    None, "logic", "Added compiler-default logic datatype")

    for domain, location in records(data.get("clock_domains", {}), "clock_domains"):
        _normalize_field(domain, "edge", EDGE_ALIASES, location, repairs)
        _normalize_field(domain, "reset_type", RESET_TYPE_ALIASES, location, repairs)
        _normalize_field(domain, "reset_polarity", RESET_POLARITY_ALIASES, location, repairs)
        reset = str(domain.get("reset", ""))
        if reset and str(domain.get("reset_polarity", "")).lower() in {
                "active_high", "high"}:
            reset_guards = [str(transition.get("guard", "")).replace(" ", "")
                            for transition, _ in records(
                                data.get("transitions", {}), "transitions")
                            if str(transition.get("clock_domain", "")) == str(domain.get("name", ""))
                            and re.search(r"(?:reset|rst|init)",
                                          str(transition.get("name", "")), re.I)]
            if f"!{reset}" in reset_guards:
                before = domain.get("reset_polarity")
                domain["reset_polarity"] = "active_low"
                _record(repairs, "infer_active_low_reset", location,
                        "reset_polarity", before, "active_low",
                        "Inferred active-low polarity from explicit !reset transition guard")

    domain_records = {str(domain.get("name")): (domain, location)
                      for domain, location in records(
                          data.get("clock_domains", {}), "clock_domains")}
    for state_name, state in states.items():
        domain_name = str(state.get("clock_domain", ""))
        if domain_name not in domain_records:
            continue
        domain, location = domain_records[domain_name]
        owned = domain.get("states")
        if owned is None:
            owned = []
            domain["states"] = owned
        if isinstance(owned, list) and state_name not in map(str, owned):
            owned.append(state_name)
            _record(repairs, "add_clock_domain_state", location, "states",
                    None, state_name,
                    f"Added state {state_name} from its clock_domain ownership")

    for transition, location in records(data.get("transitions", {}), "transitions"):
        kind_field = "transition_type" if "transition_type" in transition else "type"
        _normalize_field(
            transition, kind_field, TRANSITION_TYPE_ALIASES, location, repairs)
        kind = str(transition.get(kind_field, "")).lower()
        guard = transition.get("guard")
        if kind == "combinational" and str(guard).strip().lower() in {
                "", "null", "none", "~"}:
            transition["guard"] = "1'b1"
            _record(repairs, "normalize_unconditional_guard", location,
                    "guard", guard, "1'b1",
                    "Converted null combinational guard to unconditional true")
        semantics = transition.get("update_semantics")
        if isinstance(semantics, dict):
            semantics = semantics.get("type")
        if semantics is None and kind in {"sequential", "combinational"}:
            normalized = "non_blocking" if kind == "sequential" else "blocking"
            transition["update_semantics"] = normalized
            _record(repairs, "add_update_semantics", location,
                    "update_semantics", None, normalized,
                    "Added semantics implied by transition_type")
        elif isinstance(semantics, str):
            normalized = SEMANTICS_ALIASES.get(semantics.lower())
            if normalized:
                transition["update_semantics"] = normalized
                _record(repairs, "normalize_update_semantics", location,
                        "update_semantics", semantics, normalized,
                        "Normalized assignment semantics alias")
        effective_semantics = transition.get("update_semantics", semantics)
        if effective_semantics == "non_blocking":
            _strip_quoted_action_rhs(transition, location, repairs)
            _deduplicate_nonblocking_actions(transition, location, repairs)

    _migrate_initial_transitions(data, states, repairs, warnings)
    _synchronize_reset_values(data, states, repairs)
    _remove_whole_memory_reset_transitions(data, states, repairs)
    _merge_conflicting_sequential_processes(data, repairs)

    derived = _derived_records(data)
    derived_by_name = {str(item.get("name")): item for item, _ in derived}
    derived_names = {str(item.get("name")) for item, _ in derived}
    _remove_redundant_combinational_self_drivers(data, derived_by_name, repairs)
    known_names = set(ports) | set(states) | derived_names
    for signal, location in derived:
        name = str(signal.get("name", ""))
        expression = signal.get("expression")
        if isinstance(expression, str):
            stripped = expression.strip()
            if ("\n" in stripped and len(stripped) >= 2 and
                    stripped[0] == stripped[-1] and stripped[0] in {'"', "'"}):
                unquoted = stripped[1:-1]
                signal["expression"] = unquoted
                expression = unquoted
                _record(repairs, "strip_multiline_expression_quotes", location,
                        "expression", stripped, unquoted,
                        "Removed redundant quotes around multiline Verilog expression")
            match = ASSIGNMENT.match(expression)
            if match and match.group(1) == name:
                signal["expression"] = match.group(2).strip()
                _record(repairs, "strip_derived_assignment", location,
                        "expression", expression, signal["expression"],
                        "Converted derived assignment to RHS-only expression")
        if signal.get("width") is None:
            width = None
            if name in ports:
                width = ports[name].get("width")
            elif name.endswith("_next") and name[:-5] in states:
                width = states[name[:-5]].get("width")
            elif name.startswith("next_") and name[5:] in states:
                width = states[name[5:]].get("width")
            if width is not None:
                signal["width"] = width
                _record(repairs, "infer_derived_width", location, "width",
                        None, width, "Inferred derived width from output/state")

    drivers = set(states) | derived_names
    for transition, _ in records(data.get("transitions", {}), "transitions"):
        kind = str(transition.get(
            "transition_type", transition.get("type", ""))).lower()
        if kind != "combinational":
            continue
        action = transition.get("action", transition.get("actions"))
        actions = action if isinstance(action, list) else [action]
        for text in actions:
            if isinstance(text, str):
                match = ASSIGNMENT.match(text)
                if match:
                    drivers.add(match.group(1))

    for transformation, location in _transformations(data.get("data_model", {})):
        match = ASSIGNMENT.match(transformation)
        if not match:
            natural_mapping = re.match(
                r"^\s*([A-Za-z_][A-Za-z0-9_$]*)\s+equals\s+"
                r"([A-Za-z_][A-Za-z0-9_$]*)\s*$",
                transformation, re.I)
            if natural_mapping and natural_mapping.group(2) in known_names:
                match = natural_mapping
        if not match:
            warnings.append({
                "location": location,
                "message": f"Ignored non-assignment data transformation: {transformation}",
            })
            continue
        target, expression = match.group(1), match.group(2).strip()
        if target not in ports or str(ports[target].get("direction", "")).lower() != "output":
            warnings.append({
                "location": location,
                "message": f"Transformation target {target!r} is not a module output",
            })
            continue
        if target in drivers:
            continue
        signal = {
            "name": target,
            "width": ports[target].get("width", 1),
            "expression": expression,
            "dependencies": _dependencies(expression, known_names),
        }
        _add_derived(data, signal)
        drivers.add(target)
        derived_names.add(target)
        _record(repairs, "lower_output_transformation", location,
                "transformation", transformation, signal,
                "Lowered data_model output mapping into a derived signal")

    # A common front-end representation stores an output in ``<port>_reg``
    # but omits the purely structural port mapping.  The matching name and
    # width make this deterministic rather than a behavioral inference.
    for name, port in ports.items():
        if str(port.get("direction", "")).lower() != "output" or name in drivers:
            continue
        state_name = f"{name}_reg"
        state = states.get(state_name)
        if not state or str(state.get("width", 1)) != str(port.get("width", 1)):
            continue
        signal = {
            "name": name,
            "width": port.get("width", 1),
            "expression": state_name,
            "dependencies": [state_name],
        }
        _add_derived(data, signal)
        drivers.add(name)
        derived_names.add(name)
        _record(repairs, "map_output_register", f"$.module.ports.{name}",
                "driver", None, signal,
                f"Mapped output {name} to matching state {state_name}")
    _prune_unused_derived(data, ports, repairs)
    return repairs, warnings
