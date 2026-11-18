"""LLM front-end that extracts Temporal Property IR from the specification.

The HW-IR is deliberately reduced to a structural symbol table before it is
shown to the model.  It is used only to bind specification concepts to legal
identifiers, widths, clock-domain ownership, and explicit CDC synchronizer
stages; transition guards, actions, derived expressions, reset values, and
other implementation behavior are never included.
"""

import json
import os
import re
from pathlib import Path

import yaml

from IRgeneration.ir_generate.gen_petrinet_ir import OpenAI, read_spec, strip_fences
from Compiler.rtl_compiler.parser import parse_ir
from IRgeneration import llm_stream


SPEC_PROPERTY_SYSTEM_PROMPT = r"""You are a hardware specification property
engineer. Extract a Temporal Property IR from the ORIGINAL SPECIFICATION.

The properties describe what the design is required to do. Derive every
trigger, expected value, ordering rule, delay, duration, reset behavior, and
CDC requirement only from explicit statements in the specification. The
provided HW symbol table is not a behavioral oracle: use it only to spell legal
module, signal, state, parameter, and clock-domain names. Never invent behavior
from a signal name, and never reconstruct behavior from the provided HW-IR.
Here, the SPEC section literally titled "Implementation:" is different: it is
the authoritative natural-language requirement source defined below.

SECTION AUTHORITY IS STRICT. When the user message reports detected
"Module name:" and "Implementation:" headings, apply this hierarchy:
- Text before "Module name:" is BACKGROUND ONLY. It may explain the kind of
  component and define standard terms or algorithms. It MUST NOT create a
  standalone property or override Implementation. However, when Implementation
  explicitly implements that same named behavior, a compatible, more precise
  background definition MUST be used to disambiguate shorthand details.
- Text from "Module name:" through the line before "Implementation:" is an
  INTERFACE DECLARATION region. Use it only for module name, parameters, ports,
  widths, binding terminology, and explicitly stated operation-legality
  meanings. A declaration such as "full ... cannot accept further writes" may
  qualify the trigger of a write operation already required by Implementation;
  it cannot create an unrelated operation or property.
- Only text AFTER the "Implementation:" heading is the NORMATIVE BEHAVIOR
  region. Only behaviors named in this region may become properties; compatible
  earlier definitions may qualify those named behaviors as described above.
- Read the complete, coherent Implementation region before encoding a
  requirement. A later sentence inherits terminology and structural context
  established by earlier Implementation sentences. Do not interpret an
  isolated sentence while discarding that context.
- If background or declarations conflict with the normative Implementation
  region, ignore the conflicting earlier text. Do not merge or reconcile it.
- source_requirement.text and source_requirement line numbers for behavioral
  properties must cite the normative behavior region, never the background.
If no "Implementation:" heading is detected, use the entire specification as
the normative behavior region for backward compatibility.

BACKGROUND may be consulted only to disambiguate terminology or complete a
compatible algorithm definition already named by an explicit Implementation
requirement. It cannot independently create a property or override
Implementation. Interface declarations may supply explicit blocking/acceptance
semantics for an operation already named by Implementation. If the wording
remains ambiguous after this contextual reading and structural binding, omit
the property instead of guessing.

Output ONLY one complete YAML document with this shape:

generation_source: spec
source_spec: <source file path>
sva_properties:
  - name: <legal unique SystemVerilog identifier>
    generation_source: spec
    source_requirement:
      id: <stable requirement identifier>
      text: <exact or concise faithful specification statement>
      line_start: <1-based source line or null>
      line_end: <1-based source line or null>
    category: <supported category>
    scope:
      related_transitions: []
      related_states: []
      related_signals: []
      related_cdc: []
    clock:
      domain: <legal clock-domain name>
    disable:
      expression: <reset assertion expression, or omit>
    trigger:
      expression: <Verilog expression, or omit when signal is used>
      signal: <single signal for an event, or omit>
      event: level | rose | fell | changed | stable
    temporal:
      relation: same_cycle | next_cycle | fixed_delay | bounded_delay | hold | hold_until | stable | response | pipeline | ordered_sequence | pulse_width | cdc_sync
      delay: {min: <integer>, max: <integer>, unit: cycle}
    effect:
      type: condition | state_update | stable | state_transition | pipeline_output
      target: <optional target>
      expression: <expected expression when applicable>
      reference_time: current | trigger | previous
    measurement:
      generate_assert: true
      generate_cover: false
      generate_delay_covers: false

Supported categories are reset, state_update, fixed_delay, bounded_delay,
pipeline_latency, hold, stable, edge_event, request_response, handshake,
event_ordering, fsm_transition, cdc, pulse_width, mutual_exclusion, and
state_invariant. Do not emit priority properties because they require HW-IR
transition behavior.

Category-specific forms:
- state_invariant uses invariant: {expression: "..."}.
- handshake uses handshake: {request: <signal>, acknowledge: <signal>,
  response_delay: {min: N, max: M}, request_hold_until_ack: <bool>,
  acknowledge_requires_request: <bool>}.
- event_ordering uses sequence: [{event: <expression>},
  {event: <expression>, delay: {min: N, max: M}}].
- pulse_width uses trigger.signal plus duration:
  {level: high | low, min: N, max: M}.
- mutual_exclusion uses participants: [<signal>, ...] and
  constraint: {max_active: 1}.
- pipeline_latency uses effect.valid.expression and/or effect.data with target,
  source, and reference_time.
- cdc uses cdc with source_clock_domain, destination_clock_domain,
  source_signal, destination_signal, synchronization: {type: multi_flop,
  stages: N}, and delay: {min: N, max: M, unit: destination_cycle}.

All Verilog expressions must be YAML strings. Use only identifiers present in
the symbol table. The symbol table contains STRUCTURAL BINDING metadata, not a
behavioral oracle: state clock-domain ownership and CDC register ordering may
be used to bind an explicit Spec requirement, but they must never be used to
invent one. Keep related_transitions and related_cdc empty because no
implementation behavior is supplied. Put ports/derived signals in
related_signals and persistent state names in related_states.

Apply these binding rules to every non-CDC property:
- Its clock.domain must equal the clock_domain of every referenced persistent
  state, including related_states, trigger/invariant/effect expressions, and
  the effect target.
- Never reference a raw state owned by another clock domain. When the
  Implementation explicitly requires a synchronized value, use the
  destination-domain final_stage named by the matching cdc_binding, never its
  raw source_signal or an intermediate stage.
- A reset property must use the reset signal of the target state's clock
  domain. A normal property's disable expression must use that same domain's
  reset.
- A value assigned by a posedge sequential/register operation is a next-cycle
  state_update with reference_time: trigger. Do not encode a registered update
  as a same-cycle state_invariant merely because the Spec states an assignment
  formula.

MANDATORY OPERATION-ACCEPTANCE AUDIT:
- Distinguish a raw request from an accepted operation. Before emitting any
  request-driven state update, search the complete Spec context for explicit
  blocking/status conditions on that operation and include them in the
  trigger. For example, if winc requests a write and wfull says the FIFO cannot
  accept a write, the update trigger is "winc && !wfull", not "winc". Likewise,
  a read requested by rinc while rempty blocks reads uses
  "rinc && !rempty". Derive such gates only when their roles are explicitly
  stated; never infer them from names alone.
- The effect describes one accepted operation. Also emit a hold/stability
  property for a blocked request only when that behavior is explicitly stated
  and can be encoded without guessing.

MANDATORY MULTI-SENTENCE FORMULA AUDIT:
- Preserve every bit, slice, concatenation term, and negation stated by the
  most precise compatible definition in the complete Spec context. Do not
  shorten a multi-bit condition because a later Implementation sentence uses
  singular shorthand.
- In particular, when an explicitly described Gray-pointer full rule says the
  highest AND second-highest synchronized pointer bits are opposite and all
  remaining bits match, negate the two-bit slice, for example
  "{~synced_ptr[ADDR_WIDTH:ADDR_WIDTH-1],
  synced_ptr[ADDR_WIDTH-2:0]}". Negating only synced_ptr[ADDR_WIDTH] is a
  different rule and is forbidden.

MANDATORY COMBINATIONAL/SEQUENTIAL AUDIT:
- A state or register written on a stated clock edge is observed as a
  next-cycle state_update. Its expected expression is evaluated from
  trigger-cycle values (reference_time: trigger).
- A signal described only as the result of a comparison or expression, with no
  statement that it is registered or updated on an edge, is combinational:
  encode its current relation as a same-cycle state_invariant. Do not add a
  pipeline cycle and do not compare against a speculative next-pointer value.
- If a registered Gray pointer is stated to store the conversion of the current
  binary pointer at an edge, encode next-cycle pointer == Gray(trigger-cycle
  binary pointer). Do not silently convert a next binary candidate unless the
  Spec explicitly says so.
- Before returning YAML, audit every property and label in your reasoning
  whether its trigger is raw or accepted, and whether its effect is
  combinational/current or registered/next-cycle. Return only the audited YAML.

If the specification does not state a requirement precisely enough to encode
it, or the required identifier cannot be bound unambiguously, omit that
property instead of copying or guessing implementation behavior. For a
clockless combinational module, return an empty sva_properties list because
this SVA backend currently emits clocked properties only.

For a next-cycle state update whose expected expression uses the old state or
antecedent-cycle inputs, use effect.type: state_update and
reference_time: trigger. For an absolute value that must be observed in the
consequent cycle, use effect.type: condition and reference_time: current.
When effect.target is present, effect.expression MUST contain only the expected
RHS value. For example, use target: valid with expression: "1'b0"; NEVER use
target: valid with expression: "valid == 1'b0", because the deterministic
lowering already constructs target == expression.

Example:
sva_properties:
  - name: enabled_counter_increment
    generation_source: spec
    source_requirement: {id: REQ_COUNT, text: "When enable is high, count increments on the next clock.", line_start: 12, line_end: 12}
    category: state_update
    scope: {related_transitions: [], related_states: [count], related_signals: [enable], related_cdc: []}
    clock: {domain: main}
    disable: {expression: "!rst_n"}
    trigger: {expression: "enable", event: level}
    temporal: {relation: next_cycle, delay: {min: 1, max: 1, unit: cycle}}
    effect: {type: state_update, target: count, expression: "count + 1'b1", reference_time: trigger}
    measurement: {generate_assert: true, generate_cover: false, generate_delay_covers: false}
"""


SPEC_PROPERTY_SEMANTIC_REVIEW_PROMPT = r"""Perform a final semantic-fidelity
review of the complete candidate against the ORIGINAL SPECIFICATION and the
section policy. Return the full corrected YAML document only.

This is not a formatting review. Re-read all compatible context that qualifies
behaviors explicitly implemented by the Implementation section, then audit:
1. For each request-driven update, is the trigger the accepted operation,
   including every explicitly stated full/empty/busy/ready blocking condition,
   rather than an unconditional raw request?
2. For each concatenation, Gray-pointer full comparison, slice, and negation,
   did the expression preserve every bit from the most precise compatible Spec
   definition? A rule saying the highest and second-highest bits are opposite
   requires both bits to be inverted, never only the highest bit.
3. Is every clock-edge register assignment represented as next-cycle state,
   using trigger-cycle source values, while every unregistered comparison-
   defined signal remains a same-cycle invariant over current registered
   values?
4. Did any property use the raw cross-domain source instead of the final
   destination-domain synchronizer stage?

Correct every semantic issue you find. Do not copy behavior from HW-IR, do not
invent an unstated requirement, and do not explain the review."""


def _cdc_structural_binding(item, index, model):
    """Normalize supported CDC schemas without exposing behavioral logic."""
    states = model.state_map()
    domains = model.domain_map()
    source = item.get("source", item.get("source_signal"))
    source_domain = item.get(
        "source_clock_domain", item.get("source_domain"))
    if isinstance(source, dict):
        source_domain = source.get(
            "clock_domain", source.get("domain", source_domain))
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

    synchronizer = item.get("synchronizer", {}) or {}
    registers = synchronizer.get(
        "registers", item.get("synchronizer_stages", [])) or []
    if isinstance(registers, str):
        registers = [registers]
    registers = [str(register) for register in registers]
    final_stage = registers[-1] if registers else destination_signal
    if destination_signal is None:
        destination_signal = final_stage
    if destination_domain is None and final_stage in states:
        destination_domain = states[final_stage].clock_domain
    if source_domain is None and source in states:
        source_domain = states[source].clock_domain

    stages = synchronizer.get("stages", item.get("stages"))
    if isinstance(stages, list):
        stages = len(stages)
    try:
        stages = int(stages)
    except (TypeError, ValueError):
        stages = len(registers) or None

    name = item.get("name") or f"cdc_{index}"
    binding = {
        "name": str(name),
        "source_signal": source,
        "source_clock_domain": source_domain,
        "destination_clock_domain": destination_domain,
        "synchronizer": {
            "stages": stages,
            "registers": registers,
            "final_stage": final_stage,
        },
    }
    if source and final_stage and destination_domain:
        binding["semantic_binding"] = {
            "meaning": (
                f"synchronized value of {source} in {destination_domain}"),
            "signal": final_stage,
        }
    return binding


def _inferred_cdc_structural_bindings(model):
    """Recover only unambiguous chains of explicit synchronizer registers.

    Some normalized IRs represent synchronizer flops as ordinary sequential
    state updates and therefore leave ``cdc`` empty.  This inference exposes
    only the resulting connectivity; transition names, guards, and expressions
    are not returned to the property model.
    """
    states = model.state_map()
    ports = {port.name for port in model.ports}
    synchronizers = {
        state.name for state in model.states
        if "synchronizer" in state.type
    }
    sources_by_target = {}
    for transition in model.transitions:
        if transition.transition_type != "sequential":
            continue
        for assignment in transition.actions + transition.else_actions:
            target = assignment.base_target
            if target not in synchronizers:
                continue
            source = str(assignment.expression).strip()
            while source.startswith("(") and source.endswith(")"):
                source = source[1:-1].strip()
            if not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_$]*", source):
                continue
            if source not in states and source not in ports:
                continue
            sources_by_target.setdefault(target, set()).add(source)

    # A target with more than one non-constant source is not structurally
    # unambiguous and is deliberately excluded.
    source_for = {
        target: next(iter(sources))
        for target, sources in sources_by_target.items() if len(sources) == 1
    }
    next_for = {}
    for target, source in source_for.items():
        next_for.setdefault(source, []).append(target)

    bindings = []
    for first_stage, source in sorted(source_for.items()):
        destination_domain = states[first_stage].clock_domain
        source_state = states.get(source)
        if (source in synchronizers and source_state is not None and
                source_state.clock_domain == destination_domain):
            continue
        if (source_state is not None and
                source_state.clock_domain == destination_domain):
            continue
        registers = [first_stage]
        visited = {first_stage}
        current = first_stage
        while True:
            candidates = [
                target for target in next_for.get(current, [])
                if target in synchronizers and
                states[target].clock_domain == destination_domain and
                target not in visited
            ]
            if len(candidates) != 1:
                break
            current = candidates[0]
            registers.append(current)
            visited.add(current)
        if len(registers) < 2:
            continue
        final_stage = registers[-1]
        bindings.append({
            "name": f"inferred_{source}_to_{destination_domain}",
            "binding_source": "explicit_synchronizer_register_chain",
            "source_signal": source,
            "source_clock_domain": (
                source_state.clock_domain if source_state is not None else None),
            "destination_clock_domain": destination_domain,
            "synchronizer": {
                "stages": len(registers),
                "registers": registers,
                "final_stage": final_stage,
            },
            "semantic_binding": {
                "meaning": (
                    f"synchronized value of {source} in {destination_domain}"),
                "signal": final_stage,
            },
        })
    return bindings


def _structural_cdc_bindings(model):
    explicit = [
        _cdc_structural_binding(item, index, model)
        for index, item in enumerate(model.cdc, 1)
    ]
    seen = {
        (item.get("source_signal"), item.get("destination_clock_domain"),
         (item.get("synchronizer") or {}).get("final_stage"))
        for item in explicit
    }
    for item in _inferred_cdc_structural_bindings(model):
        key = (item.get("source_signal"),
               item.get("destination_clock_domain"),
               item["synchronizer"].get("final_stage"))
        if key not in seen:
            explicit.append(item)
            seen.add(key)
    return explicit


def build_spec_symbol_table(hw_ir_path):
    """Return structural binding names without exposing HW-IR behavior."""
    model = parse_ir(hw_ir_path)
    return {
        "module": model.module_name,
        "parameters": [parameter.name for parameter in model.parameters],
        "ports": [
            {"name": port.name, "direction": port.direction,
             "width": str(port.width)}
            for port in model.ports
        ],
        "clock_domains": [
            {"name": domain.name, "clock": domain.clock,
             "edge": domain.edge, "reset": domain.reset,
             "reset_polarity": domain.reset_polarity,
             "reset_type": domain.reset_type}
            for domain in model.clock_domains
        ],
        # State ownership/type are structural bindings. Reset values and
        # transition/action behavior remain deliberately hidden.
        "states": [
            {"name": state.name, "type": state.type,
             "width": str(state.width),
             "clock_domain": state.clock_domain}
            for state in model.states
        ],
        "cdc_bindings": _structural_cdc_bindings(model),
        # Do not expose derived expressions or dependencies.
        "derived_signals": [
            {"name": str(signal.get("name", "")),
             "width": str(signal.get("width", ""))}
            for signal in model.derived_signals if signal.get("name")
        ],
    }


def build_spec_section_policy(spec_text):
    """Describe which numbered SPEC lines are authoritative for behavior."""
    lines = spec_text.splitlines()

    def heading(pattern):
        for index, line in enumerate(lines, 1):
            if re.match(pattern, line, flags=re.IGNORECASE):
                return index
        return None

    module_line = heading(r"^\s*Module\s+name\s*:")
    implementation_line = heading(r"^\s*Implementation\s*:")
    if implementation_line is None:
        return {
            "mode": "whole_spec_fallback",
            "module_heading_line": module_line,
            "implementation_heading_line": None,
            "normative_behavior_lines": {
                "start": 1, "end": len(lines)},
            "rule": (
                "No Implementation heading was detected; the complete "
                "specification is authoritative for behavior."),
        }

    background_end = ((module_line - 1) if module_line is not None
                      else implementation_line - 1)
    declaration_start = module_line
    declaration_end = implementation_line - 1
    return {
        "mode": "implementation_authoritative",
        "module_heading_line": module_line,
        "implementation_heading_line": implementation_line,
        "background_lines": {
            "start": 1, "end": max(0, background_end)},
        "interface_declaration_lines": (
            {"start": declaration_start, "end": declaration_end}
            if declaration_start is not None else None),
        "normative_behavior_lines": {
            "start": implementation_line + 1, "end": len(lines)},
        "rules": [
            "Only normative_behavior_lines may create a property.",
            "Read normative_behavior_lines as one coherent Implementation block.",
            "Earlier text may precisely clarify a compatible algorithm or "
            "acceptance constraint already used by Implementation, but cannot "
            "create a standalone property.",
            "Normative Implementation text wins every conflict.",
            "Behavioral source_requirement citations must fall inside the "
            "normative behavior line range.",
        ],
    }


def build_spec_property_messages(spec_text, symbol_table, source_spec):
    numbered = "\n".join(
        f"{index}: {line}" for index, line in enumerate(spec_text.splitlines(), 1))
    section_policy = build_spec_section_policy(spec_text)
    return [
        {"role": "system", "content": SPEC_PROPERTY_SYSTEM_PROMPT},
        {"role": "user", "content": (
            "Extract the complete Spec-derived Temporal Property IR.\n\n"
            "# AUTHORITATIVE SPEC SECTION POLICY\n" +
            yaml.safe_dump(
                section_policy, allow_unicode=True, sort_keys=False) +
            "Follow this computed policy exactly. Background and interface "
            "lines cannot create standalone properties, but their compatible "
            "algorithm definitions and explicit operation-legality constraints "
            "must qualify behavior already required by Implementation.\n\n"
            "# ORIGINAL SPECIFICATION (line numbered)\n" + numbered +
            "\n\n# LEGAL HW SYMBOL TABLE (binding only; no behavior)\n" +
            yaml.safe_dump(symbol_table, allow_unicode=True, sort_keys=False) +
            "\n# SOURCE FILE\n" + str(source_spec))},
    ]


def _response_meta(response):
    usage = getattr(response, "usage", None)
    return {
        "id": getattr(response, "id", None),
        "model": getattr(response, "model", None),
        "usage": usage.model_dump() if usage is not None else None,
    }


def _normalize_document(content, source_spec):
    try:
        data = yaml.safe_load(strip_fences(content))
    except yaml.YAMLError as exc:
        raise ValueError(f"Spec Property IR is not valid YAML: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("The Spec Property IR root must be a mapping")
    properties = data.get("sva_properties")
    if not isinstance(properties, list):
        raise ValueError("Spec Property IR must contain an sva_properties list")
    source = str(Path(source_spec).resolve())
    data["generation_source"] = "spec"
    data["source_spec"] = source
    for index, prop in enumerate(properties):
        if not isinstance(prop, dict):
            raise ValueError(f"sva_properties[{index}] must be a mapping")
        prop["generation_source"] = "spec"
        if prop.get("category") == "priority":
            raise ValueError(
                f"sva_properties[{index}] priority depends on an HW-IR transition "
                "and cannot be used as a spec-derived property")
        requirement = prop.get("source_requirement")
        if isinstance(requirement, str):
            requirement = {"text": requirement}
        if not isinstance(requirement, dict) or not str(
                requirement.get("text", "")).strip():
            raise ValueError(
                f"sva_properties[{index}] is missing source_requirement.text")
        requirement.setdefault("id", prop.get("name", f"requirement_{index + 1}"))
        requirement["file"] = source
        requirement.setdefault("line_start", None)
        requirement.setdefault("line_end", requirement.get("line_start"))
        prop["source_requirement"] = requirement
        scope = prop.get("scope")
        if not isinstance(scope, dict):
            scope = {}
        # Spec properties must not acquire semantics by binding to transitions.
        scope["related_transitions"] = []
        scope.setdefault("related_states", [])
        scope.setdefault("related_signals", [])
        scope["related_cdc"] = []
        prop["scope"] = scope
        measurement = prop.get("measurement")
        if not isinstance(measurement, dict):
            measurement = {}
        measurement.setdefault("generate_assert", True)
        measurement.setdefault("generate_cover", False)
        measurement.setdefault("generate_delay_covers", False)
        prop["measurement"] = measurement
    return data


def generate_spec_property_ir(spec_path, hw_ir_path, output_path, *,
                              api_key=None, base_url=None, model=None,
                              temperature=0.2, max_tokens=8192):
    """Extract and persist a Spec-derived Temporal Property IR once."""
    api_key = api_key or os.environ.get("OPENAI_API_KEY")
    base_url = base_url or os.environ.get("OPENAI_BASE_URL")
    model = model or os.environ.get("OPENAI_MODEL")
    if not api_key:
        raise ValueError("API key is required to generate Property IR from a spec")
    if not base_url:
        raise ValueError("Base URL is required to generate Property IR from a spec")
    if not model:
        raise ValueError("Model name is required to generate Property IR from a spec")
    if OpenAI is None:
        raise RuntimeError(
            "The openai package is required to generate Property IR from a spec")

    spec = Path(spec_path).resolve()
    spec_text = read_spec(spec)
    if not spec_text.strip():
        raise ValueError("The spec is empty; Property IR cannot be generated")
    symbols = build_spec_symbol_table(hw_ir_path)
    messages = build_spec_property_messages(spec_text, symbols, spec)
    client = OpenAI(api_key=api_key, base_url=base_url)
    output = Path(output_path).resolve()
    output.parent.mkdir(parents=True, exist_ok=True)
    prompt_path = output.with_name("property_ir_prompt.json")
    raw_path = output.with_name("property_ir_raw_response.txt")
    response_path = output.with_name("property_ir_response.json")
    generation_check = output.with_name("property_ir_generation_check.json")
    prompt_path.write_text(
        json.dumps(messages, ensure_ascii=False, indent=2), encoding="utf-8")

    retry_messages = list(messages)
    attempts = []
    response = None
    content = ""
    issues = []
    semantic_review_requested = False
    for attempt in range(3):
        response = llm_stream.create(
            client, model=model, messages=retry_messages,
            temperature=temperature, max_tokens=max_tokens)
        content = response.choices[0].message.content
        try:
            data = _normalize_document(content, spec)
            output.write_text(yaml.safe_dump(
                data, allow_unicode=True, sort_keys=False,
                default_flow_style=False), encoding="utf-8")
            # Import locally to avoid a package import cycle: checker imports a
            # legacy helper from property_ir.generator.
            from .checker import check_property_ir
            report = check_property_ir(
                hw_ir_path, output, generation_check, raise_on_error=False)
            issues = report["errors"]
        except (OSError, UnicodeError, ValueError) as exc:
            issues = [{"code": "PROPERTY_GENERATION_INVALID",
                       "location": "$", "message": str(exc)}]
        attempts.append({
            "attempt": attempt + 1,
            "phase": ("semantic_fidelity_review"
                      if semantic_review_requested else "generation"),
            "response": _response_meta(response),
            "issues": issues,
        })
        if not issues:
            if not semantic_review_requested and attempt < 2:
                retry_messages.extend([
                    {"role": "assistant", "content": content},
                    {"role": "user", "content":
                     SPEC_PROPERTY_SEMANTIC_REVIEW_PROMPT},
                ])
                semantic_review_requested = True
                continue
            break
        if attempt < 2:
            retry_messages.extend([
                {"role": "assistant", "content": content},
                {"role": "user", "content": (
                    "The candidate Spec Property IR failed deterministic "
                    "validation. Correct every listed issue while preserving "
                    "the requirements from the authoritative Implementation "
                    "region identified in the original section policy. Do not "
                    "use or infer HW implementation behavior. Return the full "
                    "corrected YAML only.\n\nValidation errors:\n" +
                    json.dumps(issues, ensure_ascii=False, indent=2))},
            ])

    raw_path.write_text(content, encoding="utf-8")
    meta = _response_meta(response)
    meta.update({
        "generation_source": "spec", "source_spec": str(spec),
        "hw_symbol_table_only": True, "output": str(output),
        "spec_section_policy": build_spec_section_policy(spec_text),
        "semantic_fidelity_review_requested": semantic_review_requested,
        "prompt": str(prompt_path), "raw_response": str(raw_path),
        "validation_attempts": attempts,
        "generation_check": str(generation_check),
    })
    meta["response"] = str(response_path)
    response_path.write_text(
        json.dumps(meta, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    if issues:
        raise ValueError(
            "Spec Property IR failed deterministic validation three times; see "
            f"{generation_check}")
    return output, meta
