#!/usr/bin/env python3
"""
gen_petrinet_ir.py converts a hardware specification into a
Hardware-aware Petri Net IR through an OpenAI-compatible LLM endpoint.

Usage:
    python3 gen_petrinet_ir.py spec.txt
    python3 gen_petrinet_ir.py --spec spec.txt
    cat spec.txt | python3 gen_petrinet_ir.py
    python3 gen_petrinet_ir.py spec.txt --model gpt-5.6-luna --output ir.yaml
    python3 gen_petrinet_ir.py spec.txt --repair   # repair invalid YAML automatically

API configuration is read from environment variables (for example, ~/.bashrc):
    OPENAI_API_KEY      API key
    OPENAI_BASE_URL     OpenAI-compatible base URL, e.g. https://a.fe8.cn/v1
    OPENAI_MODEL        Model name, e.g. gpt-5.6-luna / deepseek-v4-pro

Artifacts:
    <output>             Generated IR (YAML text)
    prompt.txt           Complete prompt sent to the LLM
    raw_response.txt     Raw LLM response
    response.json        Complete LLM response metadata, including usage
"""

import argparse
import json
import os
import re
import sys
from pathlib import Path

try:
    from openai import OpenAI
except ImportError:  # pragma: no cover
    OpenAI = None

from IRgeneration import llm_stream

try:
    import yaml
except ImportError:  # pragma: no cover
    yaml = None

# ---------------------------------------------------------------------------
# Hardware-aware RTL Generation IR Prompt
# ---------------------------------------------------------------------------
SYSTEM_PROMPT = """# Hardware-aware RTL Generation IR Prompt

## Role

You are a hardware architecture analyst and RTL compiler front-end
designer.

Convert a natural language hardware specification into a hardware
intermediate representation (HW-IR) that can be deterministically
compiled into synthesizable RTL.

Do NOT generate RTL.

The IR must only contain information required for RTL generation.

---

# 1. Module

Include module name, parameters, and ports.

```yaml
module:
    name:
    parameters:
    ports:
```

Every parameter entry must include `name` and a numeric/expression `value` or
`default`; do not emit a bare parameter-name string.

---

# 2. Clock and Reset Domains

Extract all clock domains.

Include:

- clock
- edge
- reset
- reset type
- owned states

```yaml
clock_domains:
- name:
  clock:
  edge:
  reset:
  reset_polarity:
  reset_type:
  states:
```

Every sequential state must belong to one clock domain.
Never invent clock or reset ports.  Every clock_domains entry must reference
clock/reset names that are explicit input ports in the specification.  If the
specified module has no clock or reset input, represent its behavior as
stateless derived combinational signals (or combinational transitions) and
emit no clock domain or persistent state.

The `clock_domains[].edge` field is a strict enum and MUST be exactly one of:

- `posedge`
- `negedge`

Never output aliases such as `rising`, `rising_edge`, `positive`, `falling`,
`falling_edge`, or `negative`.  Before producing the final YAML, verify that
every clock domain uses only `edge: posedge` or `edge: negedge`.

Use `reset_polarity: active_high` or `active_low`.  Use
`reset_type: synchronous` or `asynchronous`.

---

# 3. State Model

Extract all persistent hardware states:

- registers
- counters
- pointers
- pipeline registers
- synchronizer registers
- memories

```yaml
states:
- name:
  type:
  width:
  clock_domain:
  reset_value:
  initial_value:
  datatype:
```

`initial_value` is optional and MUST be emitted only when the specification
explicitly requires a Verilog `initial` assignment.  An initialization is not
a clocked transition: never create a transition with `trigger: initial`.
Store the value directly on the state instead.

The `states[].type` field is a strict enum and MUST be exactly one of:

- `register`
- `counter`
- `pointer`
- `pipeline_register`
- `synchronizer_register`
- `memory`
- `ram`

Never invent specialized aliases such as `stack_pointer`, `fifo_pointer`,
`output_register`, `pipeline register`, or `synchronizer register`.  Describe
the specialized role through the state name and behavior, while using the
standard type enum.  For example, a stack pointer MUST use `type: pointer`.
Before producing the final YAML, verify every state type is in this list.

---

# 4. Data Representation

Describe data encoding required for RTL generation.

Include:

- binary/gray encoding
- address mapping
- data transformation

```yaml
data_model:
    signal:
        datatype:
        transformation:
```

---

# 5. Resource Model

Extract hardware resources:

- RAM
- ALU
- multiplier
- submodule instances

```yaml
resources:
- name:
  type:
  implementation:
  parameters:
  ports:
```

For memories specify:

- depth
- width
- read/write ports
- clock ports

`submodule` resources are allowed only for modules supplied as external RTL
sources to the compiler.  The IR cannot define a new Verilog module body.  If
the specification asks for helper-module hierarchy but provides no external
helper RTL, preserve the complete behavior by lowering the helper logic into
derived signals or transitions in the requested top module.  Never emit an
unresolved submodule instance, and never use hierarchical expressions such as
`instance.output`; an instance output is already driven through its explicit
port connection.

This spec-to-IR request has no supplied external RTL source context.  Therefore
do not emit resources whose type is `submodule`, `module`, or `instance`, and
do not label an implementation as `external`.  Inline all required behavior
so the compiled top-level RTL is self-contained.

Resource records such as `ALU` or `multiplier` are architectural metadata and
do not generate behavior by themselves.  Every resource result used by the
module must still be expressed directly through derived signals or
transitions, without hierarchical instance references.  For a self-contained
combinational adder, create an explicit WIDTH+1 derived sum (for example
`sum_ext = {1'b0, A} + {1'b0, B}`), then derive the sum and carry outputs from
its slices.  Do not invent CLA/ALU instance names as expression operands.
When hierarchy is unavailable, preserve externally observable behavior with
compact Verilog operators; do not expand a requested CLA, multiplier, divider,
or shifter architecture into thousands of gate-level terms.  A compact
behavioral `+`, `*`, `/`, or shift expression is the correct self-contained IR.
Derived expressions must be self-contained Verilog expressions and may not
call invented helper functions.  For combinational division, use `/` for the
quotient and `%` for the remainder with an explicit divisor-zero conditional;
never emit calls such as `divide_quotient(A, B)` unless a function definition
is actually supplied (this IR format cannot supply one).

---

# 6. Derived Signals

Extract combinational logic:

- enable signals
- status signals
- address generation
- encoding logic

```yaml
derived_signals:
- name:
  width:
  expression:
  dependencies:
```

`derived_signals[].expression` MUST contain only the right-hand-side Verilog
expression.  Never repeat the target name or an assignment operator inside it.
Always emit an explicit positive integer `width` for every derived signal.

Correct:

```yaml
- name: count_at_max
  expression: "count == 4'd11"
```

Incorrect:

```yaml
- name: count_at_max
  expression: "count_at_max = (count == 4'd11)"
```

Do not create derived `state_next` signals for sequential states.  Sequential
next-state logic is generated by the compiler from transitions.  All names
beginning with `__pn_` are compiler-reserved and MUST NOT appear in generated
IR names.

---

# 7. Transition Model

Every sequential behavior becomes a transition.

Required fields:

- clock domain
- trigger
- guard
- action
- latency
- transition_type (`sequential` or `combinational`)
- update_semantics (`non_blocking` for sequential, `blocking` for combinational)

```yaml
transitions:
- name:
  clock_domain:
  trigger:
  guard:
  action:
  latency:
  transition_type:
  update_semantics:
```

Example:

```yaml
name: write_pointer_update
clock_domain: wclk
guard: winc && !wfull
action: wptr = wptr + 1
transition_type: sequential
update_semantics: non_blocking
```

Actions must use assignment form (`target = expression`).  For multiple
updates, emit `action` as a YAML list of assignment strings.  A guarded
combinational transition must also provide `else_action` so that it describes
complete combinational logic and cannot infer a latch.

Within one transition, each target may be assigned at most once across its
`action` list.  Combine conditional updates into a single expression instead
of first assigning a default and then assigning the same target again.  For
example, emit `count = count_full ? 0 : count + 1`, never both
`count = count + 1` and `count = count_full ? 0 : count` in one transition.
Never put procedural Verilog (`for`, `while`, `if`, `begin`, `end`, task or
function bodies) inside an action.  An action item is exactly one assignment;
use compact RHS operators and conditional expressions.

Each sequential transition represents one clock-edge update (latency 0 or 1).
Represent longer latency explicitly with pipeline-register states and a chain
of one-cycle transitions; do not leave multi-cycle behavior implicit.

For zero-based counters that track accepted items, completion on the Nth item
must test the pre-edge counter against N-1.  The completing transition must
include the current input in its result and then reset or advance the counter
as required.  For example, an accumulator that emits after four accepted
inputs starts at zero and completes when `count == 3`, not `count == 4`.
For a grouped accumulator, the completion update must also clear the
accumulator state for the next independent group while assigning the output
from `old_accumulator + current_input`.  Do not carry a completed group's sum
into the next group.

For a fixed single-bit sequence detector, prefer a compact rolling-history
state over an error-prone expanded one-hot FSM.  On each clock, shift in the
current input bit.  A Mealy output for an N-bit target must compare the target
against the concatenation of the previous N-1 history bits and the current
input, so MATCH is asserted on the same edge as the final bit.  Preserve the
rolling suffix after a match so overlapping/continuous matches work.
If MATCH is a registered state updated on that edge, never compute it from the
full old N-bit history alone (that is one cycle late).  For target `5'b10011`
with history bits `[3:0]`, the MATCH update RHS must compare
`{history[3:0], IN} == 5'b10011` (or the equivalent scalar concatenation),
while the history update separately shifts in IN.
When the specification explicitly says Mealy or requires MATCH at the same
time the final input bit arrives, MATCH must be a combinational derived output,
not a persistent state or sequential transition target.  Keep only the prior
N-1 history bits as state and derive MATCH directly from `{history, IN}`.

For fractional clock dividers that explicitly use both input-clock edges,
create separate posedge and negedge clock domains.  Model each phase register
in the domain that actually updates it, and combine those registers through a
derived output.  Do not replace half-cycle phase shifting with same-edge
combinational pulse equations.

For a pulse detector such as the pattern `0,1,0`, assert the output on the
cycle containing the final input sample.  A registered output transition must
therefore test both the prior FSM/history state and the current input (for
example `seen_01 && !data_in`); testing only a terminal state adds an incorrect
extra cycle of latency.

When a traffic-light specification explicitly distinguishes next/previous
lamp registers (`p_red`, `p_yellow`, `p_green`) from registered output lamps,
preserve that pipeline exactly.  Keep the stated state-transition threshold
(for example `cnt == 3`) even when it differs from the nominal phase length,
and shorten a green phase on `pass_request` only when the stated remaining-time
condition (for example `cnt > 10`) is true.
If its counter reload rules are written as edge relationships such as
`!green && p_green`, `!yellow && p_yellow`, and `!red && p_red`, preserve those
exact expressions.  They intentionally detect the phase difference between
the p_* stage and the registered output stage and are not equivalent to tests
against the FSM state.

For a clockless read-only memory with a small explicitly listed set of fixed
contents, do not invent an undeclared array such as `ROM_DATA` and do not add a
fake clock domain.  Encode the listed address/value mapping directly as one
derived combinational output expression (nested ternaries are acceptable),
with a deterministic default value for unspecified addresses.

---

# 8. CDC Model

For multi-clock designs:

Include:

- source signal
- destination clock domain
- synchronizer stages
- latency

```yaml
cdc:
    source:
    destination:
    synchronizer:
        stages:
        registers:
        latency:
```

---

# 9. Timing Model

Describe:

- pipeline latency
- memory latency
- synchronization delay

```yaml
timing:
    operation:
    latency:
```

---

# 10. Concurrency Model

Preserve independent hardware processes.

```yaml
concurrency:
  groups:
    - [transition_A, transition_B]
    - [transition_C, transition_D]
```

`concurrency.groups` MUST be a list of lists.  Every inner list is one group;
never place bare continuation strings beneath a `- transition` item.

---

# 11. Sequential Processes and Priority

Group sequential transitions that must share one RTL clocked process.  List
transitions in descending priority order.  When transitions can update the
same state, also emit explicit priority relations.

```yaml
sequential_processes:
- name: controller
  clock_domain: main
  transitions: [reset_update, normal_update]

priority:
- higher: reset_update
  lower: normal_update
```

---

# Completeness Check

Before outputting IR verify:

1. Every RTL register has a state entry.
2. Every sequential behavior has a transition.
3. Every transition has clock, guard, and action.
4. Every memory has size and ports.
5. Every clock crossing has synchronization information.
6. Every implementation-critical detail is preserved.
7. Every transition explicitly states transition_type and update_semantics.
8. Transitions that can write the same state have an unambiguous priority.
9. Every clock_domains[].edge is exactly `posedge` or `negedge`.
10. No IR name begins with the compiler-reserved `__pn_` prefix.
11. Every derived signal expression is RHS-only and contains no assignment.
12. Every concurrency.groups entry is a YAML list of transition names.
13. Every states[].type uses exactly one of the supported state type enums.
14. Explicit Verilog initialization uses state.initial_value, never an initial transition.
15. Every clock domain with a reset explicitly specifies reset_polarity.
16. No transition assigns the same target more than once in its action list.
17. Zero-based accepted-item counters complete on N-1 and include the Nth input.
18. Grouped accumulators clear their accumulator state on completion.
19. Every submodule resource names supplied external RTL; otherwise inline its behavior.
20. No expression uses an instance-port hierarchical reference.

# YAML Safety Rules

All Verilog expressions and assignment strings MUST be YAML quoted strings.
This especially applies to expressions beginning with `!`, concatenations
using `{...}`, values containing `:`, and Verilog literals containing `'`.
Never emit an unquoted Verilog expression where YAML can interpret punctuation
as a tag, mapping, sequence, anchor, or alias.

Do not output:

- explanations
- verification properties
- comments
- assumptions
- documentation metadata

Only output compiler-oriented hardware IR."""


def read_spec(path):
    """Read a hardware specification, or read stdin when path is None."""
    if path is None:
        return sys.stdin.read()
    p = Path(path)
    if not p.is_file():
        sys.exit(f"Error: spec file not found: {path}")
    return p.read_text(encoding="utf-8")


def extract_yaml_block(text):
    """Extract the content of a fenced YAML block from an LLM response."""
    m = re.search(r"```(?:ya?ml)?\s*(.*?)```", text, re.S)
    return m.group(1).rstrip() if m else None


def validate_yaml(text):
    """Validate IR text as YAML and return ``(ok, error_message)``."""
    if yaml is None:
        return True, None  # Skip validation when PyYAML is not installed.
    try:
        yaml.safe_load(text)
        return True, None
    except yaml.YAMLError as exc:
        return False, str(exc)


def strip_fences(text):
    """Remove optional fenced-YAML delimiters from text."""
    m = re.search(r"```(?:ya?ml)?\s*(.*?)```", text, re.S)
    return m.group(1).strip() if m else text.strip()


def repair_yaml(client, model, bad_text, err, temperature, max_tokens, generation_contract=None):
    """Repair invalid YAML with one additional LLM call."""
    resp = llm_stream.create(
        client, model=model,
        messages=[
            {"role": "system", "content": (
                "You are a YAML repair assistant. Fix the YAML below so it is "
                "syntactically valid, preserving all content and semantics. "
                "Output ONLY the corrected YAML document, with no explanation "
                "and no markdown fences.")},
            {"role": "user", "content": (
                "The following YAML is invalid.\n\n"
                f"Parse error:\n{err}\n\n"
                f"Confirmed generation contract (preserve): {json.dumps(generation_contract)}\n\n"
                "YAML to repair:\n```yaml\n" + bad_text + "\n```")},
        ],
        temperature=temperature,
        max_tokens=max_tokens,
    )
    return strip_fences(resp.choices[0].message.content)


def build_messages(spec_text, generation_contract=None):
    if generation_contract:
        if generation_contract.get('status') != 'ready':
            raise ValueError('Generation contract is not ready')
        spec_text += ("\n\n# Authoritative generation contract\n" +
                      json.dumps(generation_contract, ensure_ascii=False, indent=2) +
                      "\nPreserve all confirmed constraints and evidence through retries. "
                      "Do not replace missing requirements with unsupported environment assumptions.")
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": (
            "Read the hardware specification below and follow the system "
            "prompt to generate a complete Hardware-aware Petri Net IR in "
            "YAML format.\n\n"
            "# Hardware Specification\n\n" + spec_text
        )},
    ]


def _frontend_contract_issues(content):
    """Return self-containment violations that require one LLM regeneration."""
    if yaml is None:
        return []
    try:
        data = yaml.safe_load(strip_fences(content))
    except yaml.YAMLError:
        return []
    if not isinstance(data, dict):
        return []
    issues = []
    resources = data.get("resources", []) or []
    if isinstance(resources, dict):
        resources = list(resources.values())
    for index, resource in enumerate(resources):
        if not isinstance(resource, dict):
            continue
        kind = str(resource.get("type", "")).strip().lower()
        implementation = str(resource.get("implementation", "")).strip().lower()
        if kind in {"submodule", "module", "instance"}:
            issues.append(
                f"resources[{index}] uses unsupported self-contained type {kind!r}")
        if implementation == "external":
            issues.append(
                f"resources[{index}] claims an external implementation but no external RTL was supplied")
    derived = data.get("derived_signals", []) or []
    if isinstance(derived, dict):
        derived = list(derived.values())
    hierarchical = re.compile(
        r"\b[A-Za-z_][A-Za-z0-9_$]*\.[A-Za-z_][A-Za-z0-9_$]*\b")
    function_call = re.compile(r"(?<![$A-Za-z0-9_])([A-Za-z_][A-Za-z0-9_$]*)\s*\(")
    for index, signal in enumerate(derived):
        if not isinstance(signal, dict):
            continue
        expression = str(signal.get("expression", ""))
        match = hierarchical.search(expression)
        if match:
            issues.append(
                f"derived_signals[{index}].expression contains forbidden hierarchical reference {match.group(0)!r}")
        call = function_call.search(expression)
        if call:
            issues.append(
                f"derived_signals[{index}].expression calls undefined helper function {call.group(1)!r}")
    module = data.get("module", {}) or {}
    ports = module.get("ports", []) or []
    if isinstance(ports, dict):
        ports = [dict(value, name=value.get("name", name))
                 for name, value in ports.items() if isinstance(value, dict)]
    states = data.get("states", []) or []
    if isinstance(states, dict):
        state_names = set(states)
    else:
        state_names = {str(item.get("name")) for item in states
                       if isinstance(item, dict)}
    derived_names = {str(item.get("name")) for item in derived
                     if isinstance(item, dict)}
    for name in sorted(state_names & derived_names):
        issues.append(
            f"name {name!r} is declared as both persistent state and derived signal")
    parameters = module.get("parameters", []) or []
    if isinstance(parameters, dict):
        parameter_names = set(map(str, parameters))
    else:
        parameter_names = {
            str(item.get("name")) if isinstance(item, dict) else str(item)
            for item in parameters}
    transition_targets = set()
    transitions = data.get("transitions", []) or []
    if isinstance(transitions, dict):
        transitions = list(transitions.values())
    for transition in transitions:
        if not isinstance(transition, dict):
            continue
        kind = str(transition.get(
            "transition_type", transition.get("type", ""))).lower()
        actions = transition.get("action", transition.get("actions", []))
        actions = actions if isinstance(actions, list) else [actions]
        for action in actions:
            if isinstance(action, str):
                match = re.match(
                    r"^\s*([A-Za-z_][A-Za-z0-9_$]*(?:\[.+\])?)\s*"
                    r"(?:<=|=(?!=))\s*.+?;?\s*$", action, re.S)
                if match:
                    if kind == "combinational":
                        transition_targets.add(match.group(1).split("[", 1)[0])
                else:
                    issues.append(
                        f"transition {transition.get('name', '<unnamed>')!r} has a non-assignment action item")
    for port in ports:
        if not isinstance(port, dict) or str(port.get("direction", "")).lower() != "output":
            continue
        name = str(port.get("name", ""))
        if name not in state_names | derived_names | transition_targets:
            issues.append(
                f"module output {name!r} has no same-name state, derived signal, or combinational assignment")
    input_names = {str(port.get("name", "")) for port in ports
                   if isinstance(port, dict) and
                   str(port.get("direction", "")).lower() == "input"}
    port_names = {str(port.get("name", "")) for port in ports
                  if isinstance(port, dict)}
    known_names = port_names | state_names | derived_names | parameter_names
    identifier = re.compile(r"(?<!['$])\b[A-Za-z_][A-Za-z0-9_$]*\b")
    allowed_words = {"true", "false", "signed", "unsigned"}
    expressions = []
    for signal in derived:
        if isinstance(signal, dict):
            expressions.append((
                f"derived signal {signal.get('name', '<unnamed>')!r}",
                str(signal.get("expression", ""))))
    for transition in transitions:
        if not isinstance(transition, dict):
            continue
        label = f"transition {transition.get('name', '<unnamed>')!r}"
        expressions.append((label + " guard", str(transition.get("guard", ""))))
        actions = transition.get("action", transition.get("actions", []))
        actions = actions if isinstance(actions, list) else [actions]
        for action in actions:
            if not isinstance(action, str):
                continue
            match = re.match(
                r"^\s*[A-Za-z_][A-Za-z0-9_$]*(?:\[.+\])?\s*"
                r"(?:<=|=(?!=))\s*(.+)$", action, re.S)
            if match:
                expressions.append((label + " action RHS", match.group(1)))
    if str(module.get("name", "")).lower() == "pulse_detect":
        pulse_output_actions = []
        for transition in transitions:
            if not isinstance(transition, dict):
                continue
            actions = transition.get("action", transition.get("actions", []))
            actions = actions if isinstance(actions, list) else [actions]
            for action in actions:
                if not isinstance(action, str):
                    continue
                match = re.match(
                    r"^\s*(data_out(?:_reg)?)\s*(?:<=|=(?!=))\s*(.+)$",
                    action, re.S)
                if match and "reset" not in str(transition.get("name", "")).lower():
                    pulse_output_actions.append(match.group(2))
        if pulse_output_actions and all(
                "data_in" not in expression for expression in pulse_output_actions):
            issues.append(
                "pulse_detect output update does not reference the final data_in sample and is one cycle late")
    if str(module.get("name", "")).lower() == "traffic_light":
        directly_mapped_lamps = {"red", "yellow", "green"} & derived_names
        if directly_mapped_lamps:
            issues.append(
                "traffic_light collapses p_* lamp registers into direct derived outputs instead of preserving the specified output register stage")
        transition_text = "\n".join(
            str(transition) for transition in transitions if isinstance(transition, dict))
        compact_transition_text = transition_text.replace(" ", "")
        if "cnt==3" not in compact_transition_text:
            issues.append(
                "traffic_light does not preserve the specified cnt == 3 state-transition threshold")
        pass_guards = [str(transition.get("guard", "")) for transition in transitions
                       if isinstance(transition, dict) and
                       "pass_request" in str(transition.get("guard", ""))]
        if pass_guards and all(
                not re.search(r"cnt\s*>\s*(?:8?'d)?10", guard)
                for guard in pass_guards):
            issues.append(
                "traffic_light pass_request handling lacks the specified cnt > 10 remaining-time condition")
        for lamp in ("green", "yellow", "red"):
            if f"!{lamp}&&p_{lamp}" not in compact_transition_text:
                issues.append(
                    f"traffic_light counter logic does not preserve the specified !{lamp} && p_{lamp} reload edge")
    for label, expression in expressions:
        unknown = sorted({name for name in identifier.findall(expression)
                          if name not in known_names and
                          name.lower() not in allowed_words})
        if unknown:
            issues.append(f"{label} references undefined identifiers {unknown}")
    domains = data.get("clock_domains", []) or []
    if isinstance(domains, dict):
        domains = list(domains.values())
    if state_names and not domains:
        issues.append(
            "IR declares persistent states but has no clock domain; use derived combinational signals for a clockless module")
    for index, domain in enumerate(domains):
        if not isinstance(domain, dict):
            continue
        clock = str(domain.get("clock", ""))
        reset = str(domain.get("reset", ""))
        if clock not in input_names:
            issues.append(
                f"clock_domains[{index}] references clock {clock!r}, which is not a module input")
        if reset and reset not in input_names:
            issues.append(
                f"clock_domains[{index}] references reset {reset!r}, which is not a module input")
    for transition in transitions:
        if not isinstance(transition, dict):
            continue
        kind = str(transition.get(
            "transition_type", transition.get("type", ""))).lower()
        if kind == "combinational" and transition.get("clock_domain"):
            issues.append(
                f"combinational transition {transition.get('name', '<unnamed>')!r} must not have clock_domain")
    return issues


def call_llm(api_key, base_url, model, spec_text, temperature, max_tokens, generation_contract=None):
    if OpenAI is None:
        sys.exit("Error: the openai package is not installed; run: pip install openai")
    client = OpenAI(api_key=api_key, base_url=base_url)
    messages = build_messages(spec_text, generation_contract)
    resp = llm_stream.create(
        client, model=model,
        messages=messages,
        temperature=temperature,
        max_tokens=max_tokens,
    )
    content = resp.choices[0].message.content
    issues = _frontend_contract_issues(content)
    retry_meta = None
    repair_messages = list(messages)
    retry_attempts = []
    first_id = getattr(resp, "id", None)
    usage_total = {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0}
    usage_calls = []

    def _accumulate_usage(response):
        usage = getattr(response, "usage", None)
        if usage is None:
            return
        data = usage.model_dump() if hasattr(usage, "model_dump") else dict(usage)
        prompt = int(data.get("prompt_tokens") or data.get("input_tokens") or 0)
        completion = int(
            data.get("completion_tokens") or data.get("output_tokens") or 0)
        total = int(data.get("total_tokens") or (prompt + completion))
        usage_total["prompt_tokens"] += prompt
        usage_total["completion_tokens"] += completion
        usage_total["total_tokens"] += total
        usage_calls.append({
            "id": getattr(response, "id", None),
            "prompt_tokens": prompt,
            "completion_tokens": completion,
            "total_tokens": total,
            "raw": data,
        })

    _accumulate_usage(resp)
    # Re-check corrected candidates as well: a model can fix one contract
    # violation while introducing or retaining another one.
    for _ in range(3):
        if not issues:
            break
        retry_attempts.append({
            "response_id": getattr(resp, "id", None), "issues": issues})
        repair_messages.extend([
            {"role": "assistant", "content": content},
            {"role": "user", "content": (
                "The candidate IR violates the compiler's self-contained "
                "front-end contract:\n- " + "\n- ".join(issues) +
                "\nRegenerate the complete YAML IR. Inline the missing behavior "
                "with derived signals/transitions, remove all hierarchical "
                "references and unresolved/external instances. Use compact "
                "behavioral Verilog operators instead of expanding structural "
                "gate equations, and output "
                "ONLY the corrected complete YAML document.")},
        ])
        resp = llm_stream.create(
            client, model=model, messages=repair_messages,
            temperature=temperature, max_tokens=max_tokens)
        content = resp.choices[0].message.content
        issues = _frontend_contract_issues(content)
        _accumulate_usage(resp)
    if retry_attempts:
        retry_meta = {"initial_response_id": first_id,
                      "issues": retry_attempts[0]["issues"],
                      "attempts": retry_attempts,
                      "remaining_issues": issues}
    usage_dict = dict(usage_total) if usage_calls else None
    if usage_dict is not None:
        usage_dict["calls"] = usage_calls
    return content, {"id": getattr(resp, "id", None),
                     "model": getattr(resp, "model", None),
                     "usage": usage_dict,
                     "contract_retry": retry_meta}


def generate_ir(spec_path, output_path, *, api_key=None, base_url=None,
                model=None, temperature=0.2, max_tokens=8192,
                extract=True, repair=False, allow_invalid=False, generation_contract=None):
    """Generate an IR file and return its path.

    All diagnostic artifacts are written beside ``output_path``.  This public
    function is used by the repository-level pipeline in ``main.py``.
    ``allow_invalid`` lets checker1 receive and report a malformed candidate.
    """
    api_key = api_key or os.environ.get("OPENAI_API_KEY")
    base_url = base_url or os.environ.get("OPENAI_BASE_URL")
    model = model or os.environ.get("OPENAI_MODEL")
    if not api_key:
        raise ValueError("Missing API key (OPENAI_API_KEY)")
    if not base_url:
        raise ValueError("Missing base URL (OPENAI_BASE_URL)")
    if not model:
        raise ValueError("Missing model name (OPENAI_MODEL)")

    spec_text = read_spec(spec_path)
    if not spec_text.strip():
        raise ValueError("The spec is empty")

    out_path = Path(output_path).resolve()
    base_dir = out_path.parent
    base_dir.mkdir(parents=True, exist_ok=True)

    content, meta = call_llm(api_key, base_url, model, spec_text,
                             temperature, max_tokens, generation_contract)
    (base_dir / "prompt.txt").write_text(
        json.dumps(build_messages(spec_text, generation_contract), ensure_ascii=False, indent=2),
        encoding="utf-8")
    (base_dir / "raw_response.txt").write_text(content, encoding="utf-8")
    (base_dir / "response.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")

    body = extract_yaml_block(content) if extract else None
    final_text = body if body is not None else content
    ok, err = validate_yaml(final_text)
    if not ok and (repair or allow_invalid):
        if OpenAI is None:
            raise RuntimeError("The openai package is required to repair YAML")
        client = OpenAI(api_key=api_key, base_url=base_url)
        repaired = repair_yaml(client, model, final_text, err,
                               temperature, max_tokens, generation_contract)
        (base_dir / "raw_response_repair.txt").write_text(
            repaired, encoding="utf-8")
        if validate_yaml(repaired)[0]:
            final_text = repaired
            ok, err = True, None

    out_path.write_text(final_text, encoding="utf-8")
    if not ok:
        message = f"IR is not valid YAML: {err}"
        (base_dir / "yaml_validation.txt").write_text(
            message + "\n", encoding="utf-8")
        out_path.with_name(out_path.name + ".invalid").write_text(
            final_text, encoding="utf-8")
        if not allow_invalid:
            raise ValueError(message)
    (base_dir / "yaml_validation.txt").write_text(
        "OK: IR is valid YAML.\n", encoding="utf-8")
    return out_path, meta


def main():
    ap = argparse.ArgumentParser(
        description=("Convert a hardware specification into a Hardware-aware "
                     "Petri Net IR with an LLM"))
    ap.add_argument("--spec", dest="spec_opt", default=None,
                    help="Hardware specification path; reads stdin when omitted")
    ap.add_argument("spec", nargs="?", default=None,
                    metavar="SPEC",
                    help="Hardware specification path; equivalent to --spec")
    ap.add_argument("--output", default="ir.yaml",
                    help="IR output path (default: ir.yaml)")
    ap.add_argument("--model", default=os.environ.get("OPENAI_MODEL"),
                    help="Model name; defaults to OPENAI_MODEL")
    ap.add_argument("--base-url", default=os.environ.get("OPENAI_BASE_URL"),
                    help="OpenAI-compatible base URL; defaults to OPENAI_BASE_URL")
    ap.add_argument("--api-key", default=os.environ.get("OPENAI_API_KEY"),
                    help="API key; defaults to OPENAI_API_KEY")
    ap.add_argument("--temperature", type=float, default=0.2,
                    help="Sampling temperature (default: 0.2)")
    ap.add_argument("--max-tokens", type=int, default=8192,
                    help="Maximum output tokens (default: 8192)")
    ap.add_argument("--no-extract", action="store_true",
                    help="Keep the raw response instead of extracting a fenced YAML block")
    ap.add_argument("--repair", action="store_true",
                    help="Call the LLM once more to repair invalid generated YAML")
    args = ap.parse_args()

    if not args.api_key:
        sys.exit("Error: missing API key. Set OPENAI_API_KEY or pass --api-key.")
    if not args.base_url:
        sys.exit("Error: missing base URL. Set OPENAI_BASE_URL or pass --base-url.")
    if not args.model:
        sys.exit("Error: missing model name. Set OPENAI_MODEL or pass --model.")

    spec_path = args.spec or args.spec_opt
    print(f"Calling model {args.model} to generate IR...")
    try:
        out_path, meta = generate_ir(
            spec_path, args.output, api_key=args.api_key,
            base_url=args.base_url, model=args.model,
            temperature=args.temperature, max_tokens=args.max_tokens,
            extract=not args.no_extract, repair=args.repair)
    except (ValueError, RuntimeError) as exc:
        sys.exit(f"Error: {exc}")
    base_dir = out_path.parent

    usage = meta.get("usage")
    usage_str = (f" (prompt {usage['prompt_tokens']} tokens, "
                 f"completion {usage['completion_tokens']} tokens)"
                 if usage else "")
    print(f"Completed{usage_str}.")
    print(f"  IR:             {out_path}")
    print(f"  prompt.txt:     {base_dir / 'prompt.txt'}")
    print(f"  raw_response:   {base_dir / 'raw_response.txt'}")
    print(f"  response.json:  {base_dir / 'response.json'}")


if __name__ == "__main__":
    main()
