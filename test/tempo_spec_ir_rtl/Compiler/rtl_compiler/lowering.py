from collections import defaultdict
import re

from .model import SequentialProcess


def is_reset_transition(model, transition):
    """Return whether a transition duplicates its clock-domain reset branch."""
    if transition.transition_type != "sequential":
        return False
    domain = model.domain_map().get(transition.clock_domain)
    if not domain or not domain.reset:
        return False
    trigger = str(transition.trigger or "").strip().lower()
    reset = str(domain.reset).strip().lower()
    if trigger == "reset":
        return True
    trigger_identifiers = re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", trigger)
    return bool(reset and reset in trigger_identifiers)


def build_processes(model):
    """Return complete process list, auto-grouping unlisted transitions."""
    result = list(model.processes)
    listed = {name for process in result for name in process.transitions}
    by_domain = defaultdict(list)
    for transition in model.transitions:
        if transition.transition_type == "sequential" and transition.name not in listed:
            by_domain[transition.clock_domain].append(transition.name)
    existing_names = {process.name for process in result}
    for domain, transitions in by_domain.items():
        name = f"{domain}_auto_process"
        suffix = 2
        while name in existing_names:
            name = f"{domain}_auto_process_{suffix}"
            suffix += 1
        existing_names.add(name)
        result.append(SequentialProcess(name, domain, transitions))
    return result


def ordered_for_target(model, process, target):
    """Order target writers from highest to lowest priority."""
    transition_map = model.transition_map()
    names = [name for name in process.transitions
             if not is_reset_transition(model, transition_map[name])
             and any(a.base_target == target for a in transition_map[name].actions)]
    if len(names) < 2:
        return names

    edges = {(high, low) for high, low in model.priorities
             if high in names and low in names}
    # Stable topological sort: process order breaks otherwise unrelated ties.
    remaining = list(names)
    ordered = []
    while remaining:
        ready = [name for name in remaining
                 if not any(low == name and high in remaining for high, low in edges)]
        if not ready:  # validator currently catches references, this protects cycles.
            ready = [remaining[0]]
        chosen = ready[0]
        ordered.append(chosen)
        remaining.remove(chosen)
    return ordered
