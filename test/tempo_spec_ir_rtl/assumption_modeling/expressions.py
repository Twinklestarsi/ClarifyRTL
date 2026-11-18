"""Restricted Verilog AST with unsigned, context-sized bitvector semantics.

No eval, dynamic part selects, unsized arithmetic constants, four-state or mixed signed math.
Unsupported syntax fails closed for semantic checks and dependency extraction.
"""
import re
from dataclasses import dataclass


class Unsupported(ValueError):
    pass


_TOKEN = re.compile(r"\s*(\d+'[bBdDhH][0-9a-fA-F_]+|[A-Za-z_][A-Za-z0-9_$]*|\d+|&&|\|\||==|!=|<=|>=|<<|>>|[(){}\[\],?:!~+\-&|^<>])")
_PRECEDENCE = {'||': 1, '&&': 2, '|': 3, '^': 4, '&': 5,
               '==': 6, '!=': 6, '<': 7, '>': 7, '<=': 7, '>=': 7, '<<': 8, '>>': 8, '+': 9, '-': 9}
_BOOLEAN = {'||', '&&', '==', '!=', '<', '>', '<=', '>='}


@dataclass(frozen=True)
class Expr:
    op: str
    args: tuple

    def symbols(self):
        if self.op == 'var':
            return {self.args[0]}
        return set().union(*(x.symbols() for x in self.args if isinstance(x, Expr)))


def parse(text):
    text = str(text).strip()
    tokens, position = [], 0
    while position < len(text):
        match = _TOKEN.match(text, position)
        if not match:
            raise Unsupported(f'unsupported expression syntax at {text[position:position+24]!r}')
        tokens.append(match.group(1))
        position = match.end()
    index = 0

    def expression(minimum=0):
        nonlocal index
        if index >= len(tokens):
            raise Unsupported('incomplete expression')
        token = tokens[index]
        index += 1
        if token in ('!', '~', '-', '+'):
            left = Expr('unary' + token, (expression(10),))
        elif token == '{':
            parts = [expression()]
            while index < len(tokens) and tokens[index] == ',':
                index += 1
                parts.append(expression())
            if index >= len(tokens) or tokens[index] != '}':
                raise Unsupported('unbalanced/unsupported concatenation')
            index += 1
            left = Expr('concat', tuple(parts))
        elif token == '(':
            left = expression()
            if index >= len(tokens) or tokens[index] != ')':
                raise Unsupported('unbalanced parentheses')
            index += 1
        elif re.fullmatch(r"\d+'[bBdDhH][0-9a-fA-F_]+", token):
            width, body = token.split("'", 1)
            try:
                number = int(body[1:].replace('_', ''), {'b': 2, 'd': 10, 'h': 16}[body[0].lower()])
            except ValueError as exc:
                raise Unsupported('invalid sized literal') from exc
            width = int(width)
            if not 1 <= width <= 4096:
                raise Unsupported('literal width outside 1..4096')
            left = Expr('const', (number % (1 << width), width))
        elif token.isdigit():
            left = Expr('unsized', (int(token),))
        elif re.fullmatch(r'[A-Za-z_][A-Za-z0-9_$]*', token):
            left = Expr('var', (token,))
        else:
            raise Unsupported(f'unexpected token {token}')
        while index < len(tokens):
            op = tokens[index]
            if op == '[':
                index += 1
                if index >= len(tokens) or not tokens[index].isdigit():
                    raise Unsupported('only constant bit/part selects are supported')
                high = low = int(tokens[index]); index += 1
                if index < len(tokens) and tokens[index] == ':':
                    index += 1
                    if index >= len(tokens) or not tokens[index].isdigit():
                        raise Unsupported('only constant bit/part selects are supported')
                    low = int(tokens[index]); index += 1
                if index >= len(tokens) or tokens[index] != ']' or low > high:
                    raise Unsupported('invalid constant part select')
                index += 1
                left = Expr('select', (left, high, low))
                continue
            if op == '?' and minimum == 0:
                index += 1
                yes = expression()
                if index >= len(tokens) or tokens[index] != ':':
                    raise Unsupported('missing ternary colon')
                index += 1
                left = Expr('?:', (left, yes, expression()))
                continue
            rank = _PRECEDENCE.get(op, -1)
            if rank < minimum:
                break
            index += 1
            left = Expr(op, (left, expression(rank + 1)))
        return left

    result = expression()
    if index != len(tokens):
        raise Unsupported(f'unexpected trailing token {tokens[index]}')
    return result


def width_of(ast, widths):
    if ast.op == 'var':
        if ast.args[0] not in widths:
            raise Unsupported(f'unknown symbol {ast.args[0]}')
        return widths[ast.args[0]]
    if ast.op == 'const':
        return ast.args[1]
    if ast.op == 'unsized':
        raise Unsupported('unsized literal inside expression')
    if ast.op == 'concat':
        return sum(width_of(arg, widths) for arg in ast.args)
    if ast.op == 'select':
        value, high, low = ast.args
        if high >= width_of(value, widths):
            raise Unsupported('part select outside operand width')
        return high - low + 1
    if ast.op in ('<<', '>>'):
        width_of(ast.args[1], widths)
        return width_of(ast.args[0], widths)
    if ast.op in _BOOLEAN or ast.op == 'unary!':
        # Still validate the operands.
        for arg in ast.args:
            width_of(arg, widths)
        return 1
    if ast.op == '?:':
        width_of(ast.args[0], widths)
        return max(width_of(arg, widths) for arg in ast.args[1:])
    return max(width_of(arg, widths) for arg in ast.args)


def resize(value, width):
    import z3
    old = value.size()
    return z3.ZeroExt(width - old, value) if old < width else value if old == width else z3.Extract(width - 1, 0, value)


def evaluate(ast, values, widths, context_width=None):
    import z3
    if ast.op == 'unsized':
        # Standalone constants (e.g. reset_value: 0) have an explicit destination.
        if context_width is None or ast.args[0] >= (1 << context_width):
            raise Unsupported('unsized constant requires an explicit destination and fitting value')
        return z3.BitVecVal(ast.args[0], context_width)
    natural = width_of(ast, widths)
    width = max(natural, context_width or natural)
    op, args = ast.op, ast.args
    if op == 'var':
        return resize(values[args[0]], width)
    if op == 'const':
        return z3.BitVecVal(args[0], width)
    if op == 'concat':
        parts = [evaluate(arg, values, widths) for arg in args]
        return resize(z3.Concat(*parts) if len(parts) > 1 else parts[0], width)
    if op == 'select':
        return resize(z3.Extract(args[1], args[2], evaluate(args[0], values, widths)), width)
    if op in ('<<', '>>'):
        left = evaluate(args[0], values, widths, width)
        amount = evaluate(args[1], values, widths)
        # Test before resizing the shift count: truncation must not turn a
        # large shift into a small one.
        check_width = max(amount.size(), width.bit_length())
        excessive = z3.UGE(resize(amount, check_width), z3.BitVecVal(width, check_width))
        shifted = left << resize(amount, width) if op == '<<' else z3.LShR(left, resize(amount, width))
        return z3.If(excessive, z3.BitVecVal(0, width), shifted)
    truth = lambda x: x != z3.BitVecVal(0, x.size())
    bit = lambda x: z3.If(x, z3.BitVecVal(1, 1), z3.BitVecVal(0, 1))
    if op == 'unary!':
        return resize(bit(z3.Not(truth(evaluate(args[0], values, widths)))), width)
    if op == '?:':
        test = truth(evaluate(args[0], values, widths))
        return z3.If(test, evaluate(args[1], values, widths, width), evaluate(args[2], values, widths, width))
    if op.startswith('unary'):
        a = evaluate(args[0], values, widths, width)
        return {'unary~': lambda: ~a, 'unary-': lambda: -a, 'unary+': lambda: a}[op]()
    if op in _BOOLEAN:
        operand_width = max(width_of(a, widths) for a in args)
    else:
        operand_width = width
    a, b = (evaluate(arg, values, widths, operand_width) for arg in args)
    operations = {'+': lambda: a + b, '-': lambda: a - b, '&': lambda: a & b,
                  '|': lambda: a | b, '^': lambda: a ^ b, '==': lambda: a == b,
                  '!=': lambda: a != b, '<': lambda: z3.ULT(a, b), '>': lambda: z3.UGT(a, b),
                  '<=': lambda: z3.ULE(a, b), '>=': lambda: z3.UGE(a, b),
                  '&&': lambda: z3.And(truth(a), truth(b)), '||': lambda: z3.Or(truth(a), truth(b))}
    value = operations[op]()
    return resize(bit(value) if op in _BOOLEAN else value, width)
