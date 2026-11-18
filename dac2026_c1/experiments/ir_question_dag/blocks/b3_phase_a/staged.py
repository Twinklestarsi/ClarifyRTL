"""Section-wise IR generation with strict schemas and one repair retry."""
from __future__ import annotations
import json, yaml
from blocks.b1_scaffold.llm import LLM

STATE_TYPES = ["register", "counter", "pointer", "pipeline_register",
               "synchronizer_register", "memory", "ram"]

HEADER = """You write ONE section of a hardware IR document. Reply with YAML only.
The module interface is fixed; never invent or rename ports:
{interface}

Rules that always apply:
- every name matches [A-Za-z_][A-Za-z0-9_$]*
- Verilog expressions are single right-hand sides (no "=", no ";", no case/if)
- never output text outside the YAML block
"""

STATES_PROMPT = HEADER + """
Write exactly two keys: `clock_domains` and `states`.

clock_domains: list of {{name, clock, edge: posedge|negedge, reset,
                        reset_type: synchronous|asynchronous,
                        reset_polarity: active_high|active_low, states: [names]}}
states: list of {{name, type, width, clock_domain, reset_value}}
`type` MUST be one of: {types}.
`width` is required. A memory or ram MUST also carry `depth` (address count).
`reset_value` is required for registers/counters/pointers.
Every state belongs to exactly one clock domain and appears in that domain's `states` list.
clock/reset MUST be input ports of the interface above."""

TRANSITIONS_PROMPT = HEADER + """
Known states: {states}
Declared signals you may reference: {allowed}

Write exactly one key `transitions`. Emit only transitions that assign to one of
these targets: {targets}.  Each entry:
{{name, clock_domain, trigger: posedge, guard, action, transition_type: sequential,
  update_semantics: non_blocking, latency: 1}}
`action` is "target = expression" or a list of such strings. `guard` is a single
expression. Only use names from the declared list above; never invent a signal."""

DERIVED_PROMPT = HEADER + """
Known states: {states}

Write exactly one key `derived_signals`: list of {{name, width, expression}}.
Cover every output port that is not driven directly by a transition.
Expressions are single right-hand sides."""


def _ask(llm, system, user, label, validator, max_tokens=None):
    # Was 3000; the generated document must not be cut mid-section.
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    for attempt in range(2):
        text = llm.chat(messages, label=f"{label}:{attempt}", temperature=0.0,
                        max_tokens=max_tokens)
        from IRgeneration.ir_generate.gen_petrinet_ir import extract_yaml_block
        body = extract_yaml_block(text) or text
        try:
            data = yaml.safe_load(body)
        except Exception as exc:
            problems = [f"YAML does not parse: {exc}"]
            data = None
        else:
            problems = validator(data) if isinstance(data, dict) else ["root must be a mapping"]
        if not problems:
            return data
        messages += [{"role": "assistant", "content": str(body)[:3000]},
                     {"role": "user", "content": "Fix these problems and reply with the corrected YAML only:\n- "
                      + "\n- ".join(problems[:8])}]
    return {}


def _valid_states(data):
    problems = []
    for item in data.get("clock_domains") or []:
        if not isinstance(item, dict):
            problems.append("clock_domains entries must be mappings")
            continue
        if item.get("edge") not in ("posedge", "negedge"):
            problems.append(f"clock domain {item.get('name')}: edge must be posedge/negedge")
        if item.get("reset_type") not in ("synchronous", "asynchronous"):
            problems.append(f"clock domain {item.get('name')}: invalid reset_type")
        if item.get("reset_polarity") not in ("active_high", "active_low"):
            problems.append(f"clock domain {item.get('name')}: invalid reset_polarity")
    for item in data.get("states") or []:
        if not isinstance(item, dict):
            problems.append("states entries must be mappings")
            continue
        if item.get("type") not in STATE_TYPES:
            problems.append(f"state {item.get('name')}: type {item.get('type')!r} not in {STATE_TYPES}")
        if not item.get("clock_domain"):
            problems.append(f"state {item.get('name')}: missing clock_domain")
        if item.get("width") in (None, ""):
            problems.append(f"state {item.get('name')}: missing width")
        if item.get("type") in ("memory", "ram") and item.get("depth") in (None, ""):
            problems.append(f"state {item.get('name')}: memory needs depth")
    if not data.get("states"):
        problems.append("no states")
    return problems


def _valid_transitions(data):
    problems = []
    for item in data.get("transitions") or []:
        if not isinstance(item, dict):
            problems.append("transitions entries must be mappings")
            continue
        for key in ("name", "clock_domain", "guard", "action"):
            if not item.get(key):
                problems.append(f"transition {item.get('name')}: missing {key}")
        if item.get("transition_type") not in ("sequential", "combinational", None):
            problems.append(f"transition {item.get('name')}: bad transition_type")
    if not data.get("transitions"):
        problems.append("no transitions")
    return problems


def _valid_derived(data):
    problems = []
    for item in data.get("derived_signals") or []:
        if not isinstance(item, dict):
            problems.append("derived_signals entries must be mappings")
            continue
        if not item.get("name") or not item.get("expression"):
            problems.append(f"derived signal {item.get('name')}: missing name/expression")
    return problems


def _link(transitions):
    """Assemble scheduling metadata: one process per domain, priority by order."""
    domain_of = {}
    processes = {}
    for item in transitions:
        if not isinstance(item, dict):
            continue
        if item.get("transition_type", "sequential") != "sequential":
            continue
        domain = item.get("clock_domain")
        if not domain:
            continue
        domain_of[item["name"]] = domain
        processes.setdefault(domain, []).append(item["name"])
    writers = {}
    for item in transitions:
        if not isinstance(item, dict):
            continue
        actions = item.get("action")
        actions = actions if isinstance(actions, list) else [actions]
        for action in actions:
            if not isinstance(action, str) or "=" not in action:
                continue
            target = action.split("=", 1)[0].strip()
            writers.setdefault(target, []).append(item["name"])
    priority = []
    for target, names in writers.items():
        for left, right in zip(names, names[1:]):
            if domain_of.get(left) and domain_of.get(left) == domain_of.get(right):
                priority.append({"higher": left, "lower": right})
    return ([{"name": f"{domain}_proc", "clock_domain": domain,
              "transitions": names} for domain, names in processes.items()],
            priority)


REPAIR = HEADER + """
The assembled document failed validation:
{problems}

Known names (ports, states, derived signals, parameters):
{known}

Reply with exactly three keys:
`missing_states`: states that must exist for the referenced signals
  ({{name, type: register, width, clock_domain, reset_value}}), or []
`derived_signals`: corrected list ({{name, width, expression}})
`transitions`: corrected list ({{name, clock_domain, trigger, guard, action}})
Never reference a name that is neither known nor added to `missing_states`."""


def _validate_document(doc, ctx):
    import yaml as _yaml
    from Compiler.rtl_compiler.parser import parse_ir
    from Compiler.rtl_compiler.validator import validate_ir
    path = ctx.path("phase_a/staged_check.yaml")
    path.write_text(_yaml.safe_dump(doc, sort_keys=False, allow_unicode=True), encoding="utf-8")
    try:
        validate_ir(parse_ir(path))
        return []
    except Exception as exc:  # noqa: BLE001
        return [line.strip()[2:] for line in str(exc).splitlines()
                if line.strip().startswith("- ")][:12]


def _repair(doc, problems, llm, spec, iface_text):
    import json as _json
    import yaml as _yaml
    known = sorted({p.get("name") for p in doc["module"]["ports"] if isinstance(p, dict)}
                   | {s.get("name") for s in doc["states"] if isinstance(s, dict)}
                   | {s.get("name") for s in doc["derived_signals"] if isinstance(s, dict)}
                   | {p.get("name") for p in doc["module"].get("parameters") or [] if isinstance(p, dict)})
    prompt = REPAIR.format(interface=iface_text,
                           problems="\n".join(f"- {p}" for p in problems),
                           known=_json.dumps(known, ensure_ascii=False))
    part = _ask(llm, prompt, spec, "section:repair", lambda data: [])
    if not isinstance(part, dict):
        return doc
    existing = {s.get("name") for s in doc["states"] if isinstance(s, dict)}
    added = [s for s in (part.get("missing_states") or [])
             if isinstance(s, dict) and s.get("name") and s.get("name") not in existing]
    if added:
        for item in added:
            item.setdefault("type", "register")
            item.setdefault("width", 1)
            item.setdefault("reset_value", "1'b0")
        doc["states"].extend(added)
        for item in added:
            domain = item.get("clock_domain")
            for entry in doc["clock_domains"]:
                if entry.get("name") == domain:
                    entry.setdefault("states", []).append(item["name"])
    if isinstance(part.get("derived_signals"), list):
        doc["derived_signals"] = [d for d in part["derived_signals"] if isinstance(d, dict)]
    if isinstance(part.get("transitions"), list):
        doc["transitions"] = [t for t in part["transitions"] if isinstance(t, dict)]
        doc["sequential_processes"], doc["priority"] = _link(doc["transitions"])
    return doc


def _walk_strings(node, fn):
    if isinstance(node, str):
        return fn(node)
    if isinstance(node, list):
        return [_walk_strings(i, fn) for i in node]
    if isinstance(node, dict):
        return {k: _walk_strings(v, fn) for k, v in node.items()}
    return node


def _rename(document, old, new):
    import re
    pattern = re.compile(r"\b" + re.escape(old) + r"\b")
    for key in ("transitions", "derived_signals"):
        if key in document:
            document[key] = _walk_strings(document[key], lambda s: pattern.sub(new, s))
    return document


def _normalise(document):
    """One-pass repairs for the defects that are purely structural."""
    domains = [d for d in document.get("clock_domains") or [] if isinstance(d, dict)]
    names = {d.get("name") for d in domains}
    by_clock = {d.get("clock"): d.get("name") for d in domains}
    for transition in document.get("transitions") or []:
        if not isinstance(transition, dict):
            continue
        domain = transition.get("clock_domain")
        if domain in names:
            continue
        if domain in by_clock:
            transition["clock_domain"] = by_clock[domain]
        elif len(names) == 1:
            transition["clock_domain"] = next(iter(names))
    # States must also point at a declared domain (models often write the clock
    # signal name instead of the domain name).
    for state in document.get("states") or []:
        if not isinstance(state, dict):
            continue
        domain = state.get("clock_domain")
        if domain in names:
            continue
        if domain in by_clock:
            state["clock_domain"] = by_clock[domain]
        elif len(names) == 1:
            state["clock_domain"] = next(iter(names))
    for domain_entry in domains:
        owned = domain_entry.setdefault("states", [])
        for state in document.get("states") or []:
            if isinstance(state, dict) and state.get("clock_domain") == domain_entry.get("name"):
                if state.get("name") not in owned:
                    owned.append(state.get("name"))

    # Rebuild scheduling metadata from the (now mapped) transitions; a model-written
    # process may still name a clock signal or mix domains.
    transitions = [t for t in document.get("transitions") or [] if isinstance(t, dict)]
    processes, seen = {}, {}
    for transition in transitions:
        if transition.get("transition_type", "sequential") != "sequential":
            continue
        domain = transition.get("clock_domain")
        if domain in names:
            processes.setdefault(domain, []).append(transition.get("name"))
    if processes:
        document["sequential_processes"] = [
            {"name": f"{domain}_proc", "clock_domain": domain, "transitions": items}
            for domain, items in processes.items()]

    states = {s.get("name") for s in document.get("states") or [] if isinstance(s, dict)}
    for signal in list(document.get("derived_signals") or []):
        if not isinstance(signal, dict):
            continue
        if signal.get("name") in states:
            old = signal["name"]
            new = f"{old}_d"
            suffix = 2
            while new in states:
                new = f"{old}_d{suffix}"; suffix += 1
            signal["name"] = new
            document = _rename(document, old, new)
    return document


def generate_sections(case, ctx, interface, batch=6):
    llm = LLM(ledger=ctx.directory / "llm_calls.jsonl")
    spec = case.prompt_text()[:12000]
    iface_text = json.dumps(interface, ensure_ascii=False)[:6000]
    base = _ask(llm, STATES_PROMPT.format(interface=iface_text, types=STATE_TYPES),
                spec, "section:states", _valid_states)
    states = base.get("states") or []
    if not states:
        return None
    names = [str(s.get("name")) for s in states if isinstance(s, dict)]
    # Derived signals first: their names are part of the symbol table that the
    # transition section is allowed to reference.
    derived = _ask(llm, DERIVED_PROMPT.format(interface=iface_text,
                                              states=json.dumps(names, ensure_ascii=False)),
                   spec, "section:derived", _valid_derived).get("derived_signals") or []
    parameters = [str(p.get("name")) for p in interface.get("parameters") or []
                  if isinstance(p, dict) and p.get("name")]
    allowed = names + parameters + [str(d.get("name")) for d in derived if isinstance(d, dict)]
    transitions, seen = [], set()
    for start in range(0, len(names), batch):
        group = names[start:start + batch]
        part = _ask(llm, TRANSITIONS_PROMPT.format(interface=iface_text,
                                                   states=json.dumps(names, ensure_ascii=False),
                                                   allowed=json.dumps(allowed, ensure_ascii=False),
                                                   targets=json.dumps(group, ensure_ascii=False)),
                    spec, f"section:transitions:{start}", _valid_transitions)
        for item in part.get("transitions") or []:
            if isinstance(item, dict) and item.get("name") not in seen:
                seen.add(item.get("name")); transitions.append(item)
    processes, priority = _link(transitions)
    document = {"module": interface,
            "clock_domains": base.get("clock_domains") or [],
            "states": states,
            "derived_signals": [d for d in derived if isinstance(d, dict)],
            "transitions": transitions,
            "sequential_processes": processes,
            "priority": priority}
    document = _normalise(document)
    problems = _validate_document(document, ctx)
    if problems:
        repaired = _repair(document, problems, llm, spec, iface_text)
        remaining = _validate_document(repaired, ctx)
        # Accept a repair only when it strictly reduces the error count.
        if len(remaining) < len(problems):
            document = repaired
    return document
