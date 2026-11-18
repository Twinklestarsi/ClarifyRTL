"""A small constant-expression evaluator for reference RTL port widths.

The harness that builds the equivalence miter evaluates widths with ``ast`` but
supports only arithmetic, bitwise ops and ``$clog2``.  Real reference designs
also use Verilog ternaries and comparisons (``(SIZE > 1) ? $clog2(SIZE) : 1``),
which made entire Tempo26 cases unverifiable.  This module replaces that
evaluator with an equally strict whitelist that additionally understands
``IfExp``, comparisons and boolean ops.
"""

from __future__ import annotations

import ast
import re

_LITERAL = re.compile(r"(\d+)?'([sS]?)([bBoOdDhH])([0-9a-fA-F_xXzZ?]+)")


class ExprError(ValueError):
    pass


def _literal(match) -> str:
    bits, sign, base, digits = match.groups()
    if re.search(r"[xz?]", digits, re.I):
        raise ExprError("unknown bit in a width expression")
    value = int(digits.replace("_", ""), {"b": 2, "o": 8, "d": 10, "h": 16}[base.lower()])
    if bits:
        width = int(bits)
        if width > 65536:
            raise ExprError("literal too wide")
        value &= (1 << width) - 1
        if sign and width and value & (1 << (width - 1)):
            value -= 1 << width
    return f"({value})"


def _clog2(value: int) -> int:
    return (value - 1).bit_length() if value > 0 else 0


def _strip_outer(text: str) -> str:
    """Remove parentheses that wrap the whole expression."""
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


def _ternary_to_python(text: str) -> str:
    """``cond ? a : b`` -> ``(a if cond else b)``, right-associative."""
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


def evaluate(expression: str, parameters: dict[str, str], active=()) -> int:
    text = _LITERAL.sub(_literal, str(expression))
    text = text.replace("$clog2", "clog2").replace("'0", "(0)").strip()
    text = _ternary_to_python(text)
    try:
        tree = ast.parse(text, mode="eval").body
    except SyntaxError as exc:
        raise ExprError(f"invalid constant expression: {expression}") from exc

    def resolve(name: str) -> int:
        if name in active:
            raise ExprError(f"recursive parameter {name}")
        if name not in parameters:
            raise ExprError(f"unknown parameter {name}")
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
            ops = {ast.Add: lambda a, b: a + b, ast.Sub: lambda a, b: a - b,
                   ast.Mult: lambda a, b: a * b, ast.LShift: lambda a, b: a << b,
                   ast.RShift: lambda a, b: a >> b, ast.BitAnd: lambda a, b: a & b,
                   ast.BitOr: lambda a, b: a | b, ast.BitXor: lambda a, b: a ^ b}
            if type(node.op) in ops:
                return ops[type(node.op)](left, right)
            if isinstance(node.op, (ast.Div, ast.FloorDiv)):
                if not right:
                    raise ExprError("division by zero")
                return abs(left) // abs(right) * (-1 if (left < 0) != (right < 0) else 1)
            if isinstance(node.op, ast.Mod):
                if not right:
                    raise ExprError("division by zero")
                return left % right
        if isinstance(node, ast.IfExp):
            return walk(node.body) if walk(node.test) else walk(node.orelse)
        if isinstance(node, ast.Compare):
            left = walk(node.left)
            for op, comparator in zip(node.ops, node.comparators):
                right = walk(comparator)
                ok = {ast.Gt: left > right, ast.GtE: left >= right,
                      ast.Lt: left < right, ast.LtE: left <= right,
                      ast.Eq: left == right, ast.NotEq: left != right}.get(type(op))
                if ok is None:
                    raise ExprError("unsupported comparison")
                if not ok:
                    return 0
                left = right
            return 1
        if isinstance(node, ast.BoolOp):
            values = [walk(item) for item in node.values]
            if isinstance(node.op, ast.And):
                return int(all(values))
            if isinstance(node.op, ast.Or):
                return int(any(values))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id == "clog2" and len(node.args) == 1 and not node.keywords):
            return _clog2(walk(node.args[0]))
        if (isinstance(node, ast.Call) and isinstance(node.func, ast.Name)
                and node.func.id in {"max", "min"} and node.args and not node.keywords):
            values = [walk(argument) for argument in node.args]
            return max(values) if node.func.id == "max" else min(values)
        raise ExprError(f"unsupported node {type(node).__name__}")

    return walk(tree)


def patch_harness(shared) -> None:
    """Install this evaluator on the reference harness module."""
    if getattr(shared, "_width_evaluator_patched", False):
        return
    original = shared.integer

    def integer(expression, parameters, active=()):
        try:
            return evaluate(expression, parameters, active)
        except ExprError:
            return original(expression, parameters, active)

    shared.integer = integer
    shared._width_evaluator_patched = True