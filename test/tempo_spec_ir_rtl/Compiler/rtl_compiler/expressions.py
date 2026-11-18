"""Validation for the Verilog expressions embedded in the IR.

Every ``guard``, ``action`` right-hand side and ``derived_signals[].expression``
is emitted almost verbatim by ``backend.generate_verilog``, so a malformed
expression only fails later in Verilator or JasperGold.  These checks move that
failure to ``validate_ir``:

* procedural Verilog (``case``/``if``/``begin``/``;``) is rejected;
* an assignment hidden inside an expression is rejected;
* unknown function calls are rejected (``$clog2``, ``clog2``, ``$signed``,
  ``$unsigned``, ``$bits``, ``$high``, ``$low`` are known);
* constant indices outside a declared width are rejected;
* variable bit-selects are accepted for packed vectors and memories;
* literal division/modulo by zero is rejected.
"""

from __future__ import annotations

import re

_PROCEDURAL = re.compile(
    r"\b(case|casez|casex|endcase|begin|end|always|always_ff|always_comb|"
    r"initial|assign|function|endfunction|task|endtask|for|while|repeat|"
    r"forever|return|if|else|generate|endgenerate)\b", re.I)
_ASSIGNMENT = re.compile(r"(?<![=!<>+\-*/&|^])=(?!=)")
_FUNCTION = re.compile(r"\$?([A-Za-z_]\w*)\s*\(")
_SELECT = re.compile(r"\b([A-Za-z_][\w$]*)\s*\[\s*([^\[\]]*?)\s*\]")
_DIV_ZERO = re.compile(r"[\/%]\s*\(?\s*0\s*\)?")

KNOWN_FUNCTIONS = {
    "clog2", "signed", "unsigned", "bits", "high", "low", "ceil", "floor",
    "countones", "onehot", "onehot0",
}
# Identifier followed by "(" that are language keywords, not functions.
_KEYWORD_CALLS = {"if", "case", "for", "while", "repeat", "return", "assign"}


def _constant(token: str) -> int | None:
    text = token.strip().replace("_", "")
    if re.fullmatch(r"\d+", text):
        return int(text)
    match = re.fullmatch(r"\d*'[sS]?([bBoOdDhH])([0-9a-fA-F]+)", text)
    if match:
        base = {"b": 2, "o": 8, "d": 10, "h": 16}[match.group(1).lower()]
        try:
            return int(match.group(2), base)
        except ValueError:
            return None
    return None


_RESERVED = {
    "module", "endmodule", "input", "output", "inout", "wire", "reg", "logic",
    "begin", "end", "if", "else", "case", "casez", "casex", "endcase",
    "posedge", "negedge", "always", "assign", "parameter", "localparam",
    "signed", "unsigned", "default", "for", "while", "return", "function",
    "endfunction", "task", "endtask", "generate", "endgenerate", "initial",
}


def _identifiers(text: str) -> set[str]:
    cleaned = re.sub(r"\d*'[sS]?[bBoOdDhH][0-9a-fA-F_xXzZ?]+", " ", text)
    cleaned = re.sub(r"\$?\b[A-Za-z_]\w*\s*\(", " ", cleaned)  # drop call names
    return {token for token in re.findall(r"[A-Za-z_][A-Za-z0-9_$]*", cleaned)
            if token.lower() not in _RESERVED}


def _unbalanced(value: str) -> str:
    """First delimiter problem in ``value``, or an empty string.

    Counting alone misses the common ``{{W{1'b0}}`` typo (open and close
    counts can still match in a different order), so track nesting.
    """
    pairs = {"(": ")", "[": "]", "{": "}"}
    stack: list[str] = []
    quoted = False
    for char in value:
        if char == '"':
            quoted = not quoted
            continue
        if quoted:
            continue
        if char in pairs:
            stack.append(pairs[char])
        elif char in ")]}":
            if not stack or stack.pop() != char:
                return f"unexpected '{char}'"
    if stack:
        return "missing " + "".join(reversed(stack))
    return ""


def validate_expression(text: str, *, widths: dict[str, int],
                        memories: set[str], where: str,
                        declared: set[str] | None = None) -> list[str]:
    errors: list[str] = []
    value = "" if text is None else str(text)
    if not value.strip():
        return [f"{where}: expression is empty"]
    if ";" in value:
        errors.append(f"{where}: ';' is not allowed in an expression")
    if _PROCEDURAL.search(value):
        errors.append(f"{where}: procedural Verilog is not allowed in an expression")
    if _ASSIGNMENT.search(value):
        errors.append(f"{where}: assignment inside an expression "
                      "(use the target's action field instead)")
    for name in _FUNCTION.findall(value):
        if name.lower() in _KEYWORD_CALLS:
            continue
        if name not in KNOWN_FUNCTIONS:
            errors.append(f"{where}: unknown function '{name}('")
    if _DIV_ZERO.search(value):
        errors.append(f"{where}: literal division or modulo by zero")
    problem = _unbalanced(value)
    if problem:
        errors.append(f"{where}: unbalanced parentheses, brackets or braces "
                      f"({problem})")
        return errors
    for bracket in re.finditer(r"\[", value):
        prefix = value[:bracket.start()].rstrip()
        if not re.search(r"[A-Za-z_][A-Za-z0-9_$]*$", prefix):
            errors.append(
                f"{where}: bit/part-select must apply to a declared identifier, "
                "not to an expression; assign the expression to a derived signal "
                "or use a shift")
            break
    known = declared if declared is not None else set(widths)
    unknown = sorted(_identifiers(value) - known)
    if unknown:
        errors.append(f"{where}: undeclared identifier(s) {unknown}")
    for name, index in _SELECT.findall(value):
        if name not in widths:
            continue
        pieces = [piece.strip() for piece in index.split(":")]
        if len(pieces) == 2:
            high, low = _constant(pieces[0]), _constant(pieces[1])
            width = widths[name]
            if high is not None and (high < 0 or high >= width):
                errors.append(f"{where}: part-select {name}[{index}] exceeds "
                              f"the declared width {width}")
            if low is not None and (low < 0 or low >= width):
                errors.append(f"{where}: part-select {name}[{index}] exceeds "
                              f"the declared width {width}")
            continue
        constant = _constant(index)
        if constant is not None:
            width = widths[name]
            if constant < 0 or constant >= width:
                errors.append(f"{where}: index {name}[{index}] is outside "
                              f"the declared width {width}")
        # Runtime bit-selects are legal for ordinary packed vectors as well as
        # memories.  Register arrays have already been flattened or promoted
        # to memories by elaborate.py, so accepting the select here cannot
        # accidentally preserve an unpacked array reference.
    return errors


def validate_action(target: str, expression: str, *, widths: dict[str, int],
                    memories: set[str], where: str,
                    known: set[str] | None = None) -> list[str]:
    declared_names = known if known is not None else set(widths)
    errors = validate_expression(expression, widths=widths, memories=memories,
                                 where=where, declared=declared_names)
    base = target.split("[", 1)[0].strip()
    if "[" in target:
        index = target[target.index("[") + 1:target.rindex("]")]
        constant = _constant(index.strip())
        if constant is not None and base in widths and constant >= widths[base]:
            errors.append(f"{where}: write index {target} is outside the "
                          f"declared width {widths[base]}")
    declared_names = known if known is not None else set(widths)
    if base not in declared_names:
        errors.append(f"{where}: assignment target '{base}' is not a declared "
                      "port, state or memory")
    return errors
