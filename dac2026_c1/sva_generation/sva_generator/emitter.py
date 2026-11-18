"""Render the SVA AST into a deterministic include fragment."""

from .model import DirectBody, ImplicationBody, RawBody, SVAProgram


def _indented_expression(expression, indent="  "):
    return "\n".join(indent + line for line in expression.splitlines())


COMPATIBILITY_PROFILES = (
    "default",
    "verilator_exact_ranges",
    "verilator_minimal_actions",
    "verilator_inline_properties",
)


def _profile_options(profile):
    if profile not in COMPATIBILITY_PROFILES:
        raise ValueError(f"unknown SVA compatibility profile: {profile}")
    level = COMPATIBILITY_PROFILES.index(profile)
    return {
        "exact_delay_ranges": level >= 1,
        "minimal_actions": level >= 2,
        "inline_properties": level >= 3,
    }


def _delay_prefix(minimum, maximum, *, exact_delay_ranges=False):
    if minimum == 0 and maximum == 0:
        return ""
    if minimum == maximum:
        if exact_delay_ranges and minimum > 1:
            return f"##[{minimum}:{maximum}] "
        return f"##{minimum} "
    return f"##[{minimum}:{maximum}] "


def _render_body(body, *, exact_delay_ranges=False):
    if isinstance(body, ImplicationBody):
        if body.delay_min == 1 and body.delay_max == 1:
            return f"({body.antecedent}) |=> ({body.consequent});"
        delay = _delay_prefix(
            body.delay_min, body.delay_max,
            exact_delay_ranges=exact_delay_ranges)
        return f"({body.antecedent}) |-> {delay}({body.consequent});"
    if isinstance(body, DirectBody):
        return f"({body.expression});"
    if isinstance(body, RawBody):
        return body.text.rstrip().rstrip(";") + ";"
    raise TypeError(f"unsupported SVA body AST: {type(body).__name__}")


def _sv_string(value):
    return str(value).replace("\\", "\\\\").replace('"', '\\"')


def _assert_action(prop, *, minimal=False):
    """Render a structured, machine-searchable assertion failure action."""
    fields = [
        f"property={_sv_string(prop.name)}",
        f"source={_sv_string(prop.source_property)}",
        f"category={_sv_string(prop.category)}",
        f"role={_sv_string(prop.role)}",
        "time=%0t",
        f"clock={prop.clock.edge}:{_sv_string(prop.clock.signal)}",
    ]
    if prop.requirement_id:
        fields.append(f"requirement={_sv_string(prop.requirement_id)}")
    if prop.spec_source:
        fields.append(f"spec_source={_sv_string(prop.spec_source)}")
    arguments = ["$time"]
    if prop.generate_loop is not None:
        fields.append("element=%0d")
        # A genvar becomes an implicit localparam inside its generated scope;
        # report it directly rather than trying to sample an elaboration value.
        arguments.append(prop.generate_loop.index)
    for label, expression in (() if minimal else prop.diagnostics):
        fields.append(f"{_sv_string(label)}=%0h")
        arguments.append(f"$sampled(({expression}))")
    message = "SVA_FAIL " + " ".join(fields)
    lines = ["else begin"]
    if len(arguments) == 1:
        lines.append(f'  $error("{message}", {arguments[0]});')
    else:
        lines.append(f'  $error("{message}",')
        for index, argument in enumerate(arguments):
            comma = "," if index + 1 < len(arguments) else ""
            lines.append(f"         {argument}{comma}")
        lines.append("  );")
    lines.append("end")
    return lines


def _inline_property_lines(prop, *, exact_delay_ranges=False):
    lines = [f"  @({prop.clock.edge} {prop.clock.signal})"]
    if prop.disable:
        lines.append(f"  disable iff ({prop.disable})")
    body = _render_body(
        prop.property_expression,
        exact_delay_ranges=exact_delay_ranges).rstrip().rstrip(";")
    lines.append(_indented_expression(body))
    return lines


def _property_lines(prop, options):
    """Render one property without any optional generate-loop wrapper."""
    lines = []
    if prop.requirement_id or prop.spec_source:
        lines.append(
            "// Spec trace: " + " ".join(filter(None, (
                f"requirement={prop.requirement_id}" if prop.requirement_id else "",
                f"source={prop.spec_source}" if prop.spec_source else ""))))
    lines.append(f"// Property IR: {prop.source_property} ({prop.role})")
    if options["inline_properties"]:
        inline = _inline_property_lines(
            prop, exact_delay_ranges=options["exact_delay_ranges"])
        if prop.emit_assert:
            lines.append(f"assert_{prop.name}: assert property (")
            lines.extend(inline)
            lines.append(")")
            lines.extend(_assert_action(
                prop, minimal=options["minimal_actions"]))
        if prop.emit_cover:
            lines.append(f"cover_{prop.name}: cover property (")
            lines.extend(inline)
            lines.append(");")
    else:
        lines.extend([
            f"property {prop.name};",
            f"  @({prop.clock.edge} {prop.clock.signal})",
        ])
        if prop.disable:
            lines.append(f"  disable iff ({prop.disable})")
        lines.extend([
            _indented_expression(_render_body(
                prop.property_expression,
                exact_delay_ranges=options["exact_delay_ranges"])),
            "endproperty",
        ])
        if prop.emit_assert:
            lines.append(f"assert_{prop.name}: assert property ({prop.name})")
            lines.extend(_assert_action(
                prop, minimal=options["minimal_actions"]))
        if prop.emit_cover:
            lines.append(f"cover_{prop.name}: cover property ({prop.name});")
    return lines


def emit_sva(program: SVAProgram, compatibility_profile="default"):
    options = _profile_options(compatibility_profile)
    lines = [
        "// Generated by spec2rtl_v5. Do not edit manually.",
        f"// Target module: {program.module_name}",
        f"// Compatibility profile: {compatibility_profile}",
        "// Include this file inside the target or bound checker module scope.",
        "",
    ]
    for sequence in program.sequences:
        sequence_body = sequence.body.rstrip().rstrip(";") + ";"
        lines.extend([
            f"// Property IR: {sequence.source_property}",
            f"sequence {sequence.name};",
            _indented_expression(sequence_body),
            "endsequence",
            "",
        ])
    for prop in program.properties:
        rendered = _property_lines(prop, options)
        if prop.generate_loop is None:
            lines.extend(rendered)
        else:
            loop = prop.generate_loop
            lines.extend([
                f"genvar {loop.index};",
                "generate",
                f"  for ({loop.index} = 0; {loop.index} < {loop.stop}; "
                f"{loop.index}++) begin : {loop.label}",
            ])
            lines.extend(("    " + line) if line else "" for line in rendered)
            lines.extend([
                "  end",
                "endgenerate",
            ])
        lines.append("")
    return "\n".join(lines).rstrip() + "\n"
