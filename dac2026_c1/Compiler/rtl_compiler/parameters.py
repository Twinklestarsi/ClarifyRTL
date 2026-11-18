"""Constant evaluation for ``module.parameters``.

Parameters are emitted verbatim by ``backend.generate_verilog``.  A model that
writes ``SIZE_LOG2 = clog2(STRBW)`` (no ``$``) therefore produces RTL that no
tool accepts.  This module evaluates parameter expressions with
``$clog2``/``clog2``, ``max``/``min``, arithmetic, comparisons and the ternary
operator, so the compiler can emit a numeric constant and report errors early.
"""

from __future__ import annotations

import ast
import re

_LITERAL = re.compile(r"(\d+)?'([sS]?)([bBoOdDhH])([0-9a-fA-F_xXzZ?]+)")
_BARE_CLOG2 = re.compile(r"(?<!\$)\bclog2\s*\(")


class ParameterError(ValueError):
    pass


def normalise(text: str) -> str:
    """Rewrite a bare ``clog2(`` call to the system function ``$clog2(``."""
    return _BARE_CLOG2.sub("$clog2(", str(text))


def _literal(match) -> str:
    bits, sign, base, digits = match.groups()
    if re.search(r"[xz?]", digits, re.I):
        raise ParameterError("unknown bit in a parameter literal")
    value = int(digits.replace("_", ""), {"b": 2, "o": 8, "d": 10, "h": 16}[base.lower()])
    if bits:
        width = int(bits)
        if width > 65536:
            raise ParameterError("parameter literal is too wide")
        value &= (1 << width) - 1
        if sign and width and value & (1 << (width - 1)):
            value -= 1 << width
    return f"({value})"


def _ternary_to_python(text: str) -> str:
    text = _strip_outer(text)
    for _ in range(32):
        question = _find_top(text, "?")
        if question is None:
            return text
        colon = _matching_colon(text, question + 1)
        if colon is None:
            return text
        text = (f"(({_ternary_to_python(text[question + 1:colon])}) if "
                f"({_ternary_to_python(text[:question])}) else "
                f"({_ternary_to_python(text[colon + 1:])}))")
    return text


def _strip_outer(text: str) -> str:
    value = text.strip()
    while value.startswith("(") and value.endswith(")"):
        depth = 0
        for index, char in enumerate(value):
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
                if depth == 0 and index != len(value) - 1:
                    return value
        value = value[1:-1].strip()
    return value


def _find_top(text: str, needle: str) -> int | None:
    depth = 0
    for index, char in enumerate(text):
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif char == needle and depth == 0:
            return index
    return None


def _matching_colon(text: str, start: int) -> int | None:
    depth = pending = 0
    for index in range(start, len(text)):
        char = text[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        elif depth == 0 and char == "?":
            pending += 1
        elif depth == 0 and char == ":":
            if pending:
                pending -= 1
            else:
                return index
    return None


def _clog2(value: int) -> int:
    return (value - 1).bit_length() if value > 0 else 0


def evaluate(expression: str, parameters: dict[str, str], active=()) -> int:
    text = _LITERAL.sub(_literal, normalise(expression)).replace("$clog2", "clog2")
    text = _ternary_to_python(text)
    try:
        tree = ast.parse(text, mode="eval").body
    except SyntaxError as exc:
        raise ParameterError(f"invalid parameter expression: {expression}") from exc

    def resolve(name: str) -> int:
        if name in active:
            raise ParameterError(f"recursive parameter {name}")
        if name not in parameters:
            raise ParameterError(f"unknown parameter {name}")
        return evaluate(parameters[name], parameters, (*active, name))

    def walk(node) -> int:
        if isinstance(node, ast.Constant) and type(node.value) is int:
            return node.value
        if isinstance(node, ast.Name):
            return resolve(node.id)
        if isinstance(node, ast.UnaryOp):
            value = walk(node.operand)
            if isinstance(node.op, ast.UAdd):
                return value
            if isinstance(node.op, ast.USub):
                return -value
            if isinstance(node.op, ast.Invert):
                return ~value
        if isinstance(node, ast.BinOp):
            left, right = walk(node.left), walk(node.right)
            table = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
                     ast.Mult: lambda a, b: a * b, ast.LShift: lambda a, b: a << b,
                     ast.RShift: lambda a, b: a >> b, ast.BitAnd: lambda a, b: a & b,
                     ast.BitOr: lambda a, b: a | b, ast.BitXor: lambda a, b: a ^ b}
            if type(node.op) in table:
                return table[type(node.op)](left, right)
            if isinstance(node.op, (ast.Div, ast.FloorDiv)):
                if not right:
                    raise ParameterError("division by zero in a parameter")
                return abs(left) // abs(right) * (-1 if (left < 0) != (right < 0) else 1)
            if isinstance(node.op, ast.Mod):
                if not right:
                    raise ParameterError("modulo by zero in a parameter")
                return left % right
        if isinstance(node, ast.IfExp):
            return walk(node.body) if walk(node.test) else walk(node.orelse)
        if isinstance(node, ast.Compare):
            left = walk(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = walk(comparator)
                outcome = {ast.Gt: left > right, ast.GtE: left >= right,
                           ast.Lt: left < right, ast.LtE: left <= right,
                           ast.Eq: left == right, ast.NotEq: left != right}.get(type(op))
                if outcome is None:
                    raise ParameterError("unsupported comparison in a parameter")
                if not outcome:
                    return 0
                left = right
            return 1
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in {"clog2", "max", "min"} and not node.keywords):
            values = [walk(argument) for argument in node.args]
            if not values:
                raise ParameterError(f"{node.func.id}() needs arguments")
            if node.func.id == "clog2":
                if len(values) != 1:
                    raise ParameterError("$clog2 takes one argument")
                return _clog2(values[0])
            return max(values) if node.func.id == "max" else min(values)
        raise ParameterError(f"unsupported parameter expression node {type(node).__name__}")

    return walk(tree)


def evaluate_all(parameters: dict[str, str]) -> tuple[dict[str, int], list[str]]:
    """Evaluate every parameter; return values and one error per failure."""
    values: dict[str, int] = {}
    errors: list[str] = []
    for name, text in parameters.items():
        try:
            values[name] = evaluate(text, parameters)
        except ParameterError as exc:
            errors.append(f"parameter {name}: {exc}")
    return values, errors