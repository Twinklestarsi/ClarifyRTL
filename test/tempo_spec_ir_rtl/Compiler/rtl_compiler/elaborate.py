"""Compile-time elaboration: functions, generate loops and register arrays.

The rest of the compiler only understands flat IR (scalar states, one
expression per action).  Instead of teaching every pass about abstraction, this
module lowers the three new constructs *before* the model is built:

* ``functions``  -- pure combination helpers, inlined into expressions;
* ``generate``   -- parameter-resolved, optionally nested loops that emit
                    transitions / derived signals / states with compile-time
                    placeholders such as ``{i}`` or ``{i + 1}`` substituted;
* ``shape``      -- register arrays expanded into scalar states (memory and ram
                    keep their array form because the backend already supports
                    depth).

Anything that cannot be lowered deterministically raises ``ElaborationError``
with the offending construct, so the failure is never silent.
"""

from __future__ import annotations

import copy
import re
from typing import Any

MAX_EXPANSION = 4096
_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_$]*")


class ElaborationError(ValueError):
    pass


# --------------------------------------------------------------------------- #
# helpers
# --------------------------------------------------------------------------- #
_PLACEHOLDER = re.compile(r"\{([^{}]+)\}")


def _compile_time_value(value: Any, symbols: dict[str, Any], where: str) -> int:
    """Resolve one generate bound using module parameters and outer indices."""
    from .parameters import ParameterError, evaluate

    if isinstance(value, int) and not isinstance(value, bool):
        return value
    text = str(value).strip()
    try:
        return int(evaluate(text, {name: str(item) for name, item in symbols.items()}))
    except ParameterError as exc:
        raise ElaborationError(
            f"{where}: expected a compile-time integer expression ({exc})") from exc


def _substitute_compile_time(text: str, symbols: dict[str, Any]) -> str:
    """Evaluate ``{expr}`` placeholders without touching Verilog braces.

    Failed evaluations are deliberately left alone.  This preserves ordinary
    concatenation/replication syntax such as ``{a, b}`` and
    ``{WIDTH{1'b0}}`` while still resolving placeholders whose identifiers are
    module parameters or generate indices.
    """
    from .parameters import ParameterError, evaluate

    values = {name: str(item) for name, item in symbols.items()}

    def replace(match):
        expression = match.group(1).strip()
        # A Verilog literal inside braces is a concatenation/replication body,
        # never a compile-time placeholder in this IR syntax.
        if "'" in expression:
            return match.group(0)
        try:
            return str(evaluate(expression, values))
        except ParameterError:
            return match.group(0)

    return _PLACEHOLDER.sub(replace, str(text))


def _walk_strings(node: Any, transform):
    if isinstance(node, str):
        return transform(node)
    if isinstance(node, list):
        return [_walk_strings(item, transform) for item in node]
    if isinstance(node, dict):
        return {key: _walk_strings(value, transform) for key, value in node.items()}
    return node


def _replace_identifiers(text: str, mapping: dict[str, str]) -> str:
    """Word-boundary identifier replacement (never touches substrings)."""
    if not mapping:
        return text

    def replace(match):
        name = match.group(0)
        return mapping.get(name, name)

    return _IDENT.sub(replace, text)


# --------------------------------------------------------------------------- #
# functions
# --------------------------------------------------------------------------- #
_FUNC_SELECT = re.compile(r"(?<![A-Za-z0-9_$])([A-Za-z_][A-Za-z0-9_$]*)\s*\[")


def _function_locals(function: dict) -> set[str]:
    names = {str(item.get("name")) for item in function.get("params") or []
             if isinstance(item, dict) and item.get("name")}

    def walk(node: Any) -> None:
        if isinstance(node, dict):
            assign = node.get("assign")
            if isinstance(assign, dict) and assign.get("target"):
                names.add(str(assign["target"]).split("[", 1)[0].strip())
            for value in node.values():
                walk(value)
        elif isinstance(node, list):
            for item in node:
                walk(item)
    walk(function.get("body"))
    return names


def _check_function_selects(functions: dict[str, dict]) -> None:
    """Reject slicing a function local: inlining would emit ``(rvalue)[i]``.

    A call is substituted into one Verilog expression, so a part/bit select can
    only apply to an identifier that still exists after inlining (a port, state,
    parameter or derived signal).  Slicing a local must be written as a shift
    (or moved into a declared derived signal) instead of producing invalid RTL
    that only lint would catch later.
    """
    for name, function in functions.items():
        locals_ = _function_locals(function)
        sliced: set[str] = set()

        def scan(node: Any) -> None:
            if isinstance(node, dict):
                for key, value in node.items():
                    if key in {"assign", "return", "expr", "expression", "if"}:
                        scan(value)
                    elif isinstance(value, (dict, list)):
                        scan(value)
            elif isinstance(node, list):
                for item in node:
                    scan(item)
            elif isinstance(node, str):
                sliced.update(base for base in _FUNC_SELECT.findall(node)
                              if base in locals_)
        scan(function.get("body"))
        if sliced:
            raise ElaborationError(
                f"function {name}: part-select of function local(s) {sorted(sliced)}; "
                "a call is inlined into one Verilog expression, so slice a declared "
                "signal or use a shift instead")


def _evaluate_body(statements: list[dict], env: dict[str, str], depth: int,
                   functions: dict[str, dict], active: tuple[str, ...]) -> str:
    local = dict(env)
    for statement in statements:
        if "assign" in statement:
            item = statement["assign"]
            local[item["target"]] = _inline_into(str(item["expr"]), local,
                                                 functions, depth, active)
        elif "if" in statement:
            condition = _inline_into(str(statement["if"]), local, functions,
                                     depth, active)
            then_env = _evaluate_body_env(statement.get("then", []), local,
                                          functions, depth, active)
            else_env = _evaluate_body_env(statement.get("else", []), local,
                                          functions, depth, active)
            returned_then, returned_else = then_env.get("__return__"), else_env.get("__return__")
            if returned_then is not None and returned_else is not None:
                return f"(({condition}) ? ({returned_then}) : ({returned_else}))"
            for name in (set(then_env) | set(else_env)) - {"__return__"}:
                before, after_then, after_else = local.get(name), then_env.get(name), else_env.get(name)
                if after_then == after_else:
                    local[name] = after_then
                else:
                    local[name] = (f"(({condition}) ? ({after_then}) : ({after_else}))"
                                   if after_then and after_else else local.get(name, before))
        elif "return" in statement:
            value = statement["return"]
            text = str(value)
            value = text if re.fullmatch(r"[A-Za-z_][\w$]*", text) and text in local else text
            if re.fullmatch(r"[A-Za-z_][\w$]*", value) and value in local:
                return local[value]
            return _inline_into(value, local, functions, depth, active)
        else:
            raise ElaborationError(f"unsupported function statement: {sorted(statement)}")
    raise ElaborationError("function body has no return statement")


def _evaluate_body_env(statements, env, functions, depth, active):
    local = dict(env)
    for statement in statements:
        if "assign" in statement:
            item = statement["assign"]
            local[item["target"]] = _inline_into(str(item["expr"]), local,
                                                 functions, depth, active)
        elif "if" in statement:
            raise ElaborationError("nested if inside a function branch is not supported")
        elif "return" in statement:
            value = str(statement["return"])
            return_env = dict(local)
            return_env["__return__"] = (_inline_into(value, local, functions, depth, active)
                                        if value not in local else local[value])
            return return_env
        else:
            raise ElaborationError(f"unsupported function statement: {sorted(statement)}")
    return local


def _inline_into(text: str, env: dict[str, str], functions: dict[str, dict],
                 depth: int, active: tuple[str, ...]) -> str:
    if depth > 16:
        raise ElaborationError("function inlining exceeded the recursion limit")
    replaced = _replace_identifiers(text, {k: f"({v})" for k, v in env.items()
                                           if k != "__return__"})
    for name, function in functions.items():
        if name in active:
            continue
        pattern = re.compile(rf"\b{re.escape(name)}\s*\(")

        def expand(match):
            start = match.end()
            args, end = _split_arguments(replaced, start)
            params = [str(p.get("name")) for p in function.get("params", [])]
            if len(args) != len(params):
                raise ElaborationError(
                    f"function {name} expects {len(params)} argument(s), got {len(args)}")
            local_env = dict(zip(params, args))
            body = function.get("body") or []
            body = _walk_strings(body, lambda s: _replace_identifiers(s, local_env))
            value = _evaluate_body(body, {}, depth + 1, functions, (*active, name))
            return f"({value})"

        while True:
            match = pattern.search(replaced)
            if not match:
                break
            start = match.end()
            args, end = _split_arguments(replaced, start)
            params = [str(p.get("name")) for p in function.get("params", [])]
            if len(args) != len(params):
                raise ElaborationError(
                    f"function {name} expects {len(params)} argument(s), got {len(args)}")
            value = _evaluate_body(function.get("body") or [],
                                   dict(zip(params, args)), depth + 1,
                                   functions, (*active, name))
            replaced = replaced[:match.start()] + f"({value})" + replaced[end:]
    return replaced


def _split_arguments(text: str, start: int) -> tuple[list[str], int]:
    """Split ``a, b[0], c ? d : e`` starting just after ``(``; return (args, end)."""
    depth = 0
    current = ""
    args: list[str] = []
    for index in range(start, len(text)):
        char = text[index]
        if char in "([{":
            depth += 1
        elif char in ")]}":
            if depth == 0 and char == ")":
                if current.strip() or args:
                    args.append(current.strip())
                return args, index + 1
            depth -= 1
        if char == "," and depth == 0:
            args.append(current.strip())
            current = ""
            continue
        current += char
    raise ElaborationError("unbalanced function call")


def _inline_functions(data: dict) -> dict:
    functions = {str(fn.get("name")): fn for fn in data.get("functions") or []
                 if isinstance(fn, dict) and fn.get("name")}
    if not functions:
        data.pop("functions", None)
        return data

    _check_function_selects(functions)

    def transform(text: str) -> str:
        return _inline_into(text, {}, functions, 0, ())

    for key in ("transitions", "derived_signals", "generate"):
        if key in data:
            data[key] = _walk_strings(data[key], transform)
    data.pop("functions", None)
    return data


# --------------------------------------------------------------------------- #
# generate loops
# --------------------------------------------------------------------------- #
def _expand_generate(data: dict) -> dict:
    blocks = data.get("generate") or []
    if not blocks:
        data.pop("generate", None)
        return data
    parameters = _module_parameters(data)
    for block in blocks:
        if not isinstance(block, dict):
            raise ElaborationError("each generate entry must be a mapping")
        name = block.get("name", "?")
        raw_loops = block.get("for") or {}
        loops = raw_loops if isinstance(raw_loops, list) else [raw_loops]
        if not loops or any(not isinstance(loop, dict) for loop in loops):
            raise ElaborationError(
                f"generate {name}: for must be a mapping or a list of mappings")
        variables = [str(loop.get("var", "i")) for loop in loops]
        if len(variables) != len(set(variables)):
            raise ElaborationError(f"generate {name}: loop variables must be unique")

        environments: list[dict[str, int]] = []

        def expand_loop(level: int, indices: dict[str, int]) -> None:
            if level == len(loops):
                environments.append(dict(indices))
                if len(environments) > MAX_EXPANSION:
                    raise ElaborationError(
                        f"generate {name}: more than {MAX_EXPANSION} iterations exceed the limit")
                return
            loop = loops[level]
            var = variables[level]
            symbols = {**parameters, **indices}
            if "start" not in loop or "stop" not in loop:
                raise ElaborationError(
                    f"generate {name}: loop {var} needs start/stop")
            start = _compile_time_value(
                loop["start"], symbols, f"generate {name} loop {var} start")
            stop = _compile_time_value(
                loop["stop"], symbols, f"generate {name} loop {var} stop")
            step = _compile_time_value(
                loop.get("step", 1), symbols, f"generate {name} loop {var} step")
            if step == 0:
                raise ElaborationError(f"generate {name}: loop {var} step cannot be zero")
            values = range(start, stop, step)
            if not values:
                raise ElaborationError(f"generate {name}: loop {var} has an empty range")
            for index in values:
                indices[var] = index
                expand_loop(level + 1, indices)
            indices.pop(var, None)

        expand_loop(0, {})
        for indices in environments:
            symbols = {**parameters, **indices}
            emitted = _walk_strings(
                copy.deepcopy(block.get("emit") or []),
                lambda text, env=symbols: _substitute_compile_time(text, env))
            for item in emitted:
                if not isinstance(item, dict):
                    raise ElaborationError(
                        f"generate {name}: emit items must be mappings, "
                        f"got {type(item).__name__}")
                if "transition" in item:
                    data.setdefault("transitions", []).append(item["transition"])
                if "derived_signal" in item:
                    data.setdefault("derived_signals", []).append(item["derived_signal"])
                if "state" in item:
                    data.setdefault("states", []).append(item["state"])
    data.pop("generate", None)
    return data


# --------------------------------------------------------------------------- #
# register arrays
# --------------------------------------------------------------------------- #
def _module_parameters(document: dict) -> dict[str, Any]:
    module = document.get("module") or {}
    result: dict[str, Any] = {}
    parameters = module.get("parameters") or []
    if isinstance(parameters, dict):
        for name, item in parameters.items():
            if isinstance(item, dict):
                item = item.get("value", item.get("default", 0))
            result[str(name)] = item
        return result
    for item in parameters:
        if isinstance(item, dict) and item.get("name") is not None:
            result[str(item["name"])] = item.get("value", item.get("default", 0))
        elif isinstance(item, str):
            result[item] = 0
    return result


def _resolve_size(value: Any, parameters: dict[str, Any], where: str) -> int:
    from .parameters import ParameterError, evaluate

    if isinstance(value, int):
        return value
    text = str(value).strip().strip("[]")
    if re.fullmatch(r"\d+", text):
        return int(text)
    try:
        return int(evaluate(text, {k: str(v) for k, v in parameters.items()}))
    except ParameterError as exc:
        raise ElaborationError(f"{where}: shape must be a constant ({exc})") from exc


def _array_shape(state: dict, parameters: dict[str, Any]) -> list[int] | None:
    shape = state.get("shape")
    if shape is None:
        return None
    name = state.get("name")
    if isinstance(shape, (int, str)):
        return [_resolve_size(shape, parameters, f"state {name}")]
    if isinstance(shape, list) and shape:
        return [_resolve_size(item, parameters, f"state {name}") for item in shape]
    try:
        size = int(size)
    except (TypeError, ValueError) as exc:
        raise ElaborationError(
            f"state {state.get('name')}: shape must be a constant") from exc
    if size < 1:
        raise ElaborationError(f"state {state.get('name')}: shape must be positive")
    return size


def _variable_indexed(data: dict, candidates: set[str]) -> set[str]:
    """Names of register arrays that are read with a non-constant index."""
    found: set[str] = set()
    pattern = re.compile(rf"\b({'|'.join(map(re.escape, candidates))})\s*\[\s*([^\]]+?)\s*\]")

    def scan(text: str) -> str:
        for match in pattern.finditer(text):
            index = match.group(2).strip()
            if _constant(index.split(":")[0]) is None:
                found.add(match.group(1))
        return text

    for key in ("transitions", "derived_signals"):
        if key in data:
            _walk_strings(data[key], scan)
    return found


def _expand_arrays(data: dict) -> dict:
    parameters = _module_parameters(data)
    array_states: dict[str, list[int]] = {}
    kept_states = []
    register_arrays = {str(state.get("name")): _array_shape(state, parameters)
                       for state in data.get("states") or []
                       if isinstance(state, dict)
                       and str(state.get("type", "register")).lower() not in {"memory", "ram"}
                       and _array_shape(state, parameters) is not None}
    promoted = _variable_indexed(data, set(register_arrays)) if register_arrays else set()

    for state in data.get("states") or []:
        if not isinstance(state, dict):
            kept_states.append(state)
            continue
        dims = _array_shape(state, parameters)
        kind = str(state.get("type", "register")).lower()
        if dims is None:
            kept_states.append(state)
            continue
        if kind not in {"memory", "ram"} and str(state.get("name")) in promoted:
            # A variable index needs addressable storage; a memory has exactly
            # the same clocked-write / combinational-read behaviour.
            state = dict(state)
            state["type"] = "memory"
            state.setdefault("depth", dims[0])
            state.pop("shape", None)
            if len(dims) > 1:
                raise ElaborationError(
                    f"state {state.get('name')}: multi-dimensional memory is not supported")
            kept_states.append(state)
            continue
        if kind in {"memory", "ram"}:
            state = dict(state)
            state.setdefault("depth", dims[0])
            state.pop("shape", None)
            kept_states.append(state)
            continue
        name = str(state.get("name"))
        array_states[name] = dims
        for suffix in _index_tuples(dims):
            item = dict(state)
            item.pop("shape", None)
            item["name"] = f"{name}_{suffix}"
            kept_states.append(item)
    if not array_states:
        data["states"] = kept_states
        return data
    data["states"] = kept_states

    def rewrite(text: str) -> str:
        # Two-dimensional select first: name[i][j].
        def replace2(match):
            name = match.group(1)
            if name not in array_states or len(array_states[name]) < 2:
                return match.group(0)
            first = _constant(match.group(2).strip())
            second = _constant(match.group(3).strip())
            if first is None or second is None:
                raise ElaborationError(
                    f"array {name}: variable indices require type memory/ram")
            return f"{name}_{first}_{second}"

        text = re.sub(rf"\b({_IDENT.pattern})\s*\[\s*([^\]]+?)\s*\]\s*\[\s*([^\]]+?)\s*\]",
                      replace2, text)

        def replace_slice(match):
            name, high, low = match.group(1), match.group(2).strip(), match.group(3).strip()
            if name not in array_states or len(array_states[name]) != 1:
                return match.group(0)
            top, bottom = _constant(high), _constant(low)
            if top is None or bottom is None:
                raise ElaborationError(f"array {name}[{high}:{low}]: slice bounds must be constant")
            size = array_states[name][0]
            if not (0 <= bottom <= top < size):
                raise ElaborationError(f"array {name}[{high}:{low}]: slice outside 0..{size - 1}")
            items = [f"{name}_{index}" for index in range(top, bottom - 1, -1)]
            return "{" + ", ".join(items) + "}" if len(items) > 1 else items[0]

        text = re.sub(rf"\b({_IDENT.pattern})\s*\[\s*(\d+)\s*:\s*(\d+)\s*\]", replace_slice, text)

        def replace(match):
            name, index = match.group(1), match.group(2).strip()
            if name not in array_states:
                return match.group(0)
            constant = _constant(index)
            if constant is None:
                raise ElaborationError(
                    f"array {name}[{index}]: variable indices require type memory/ram")
            dims = array_states[name]
            if constant < 0 or constant >= dims[0]:
                raise ElaborationError(
                    f"array {name}[{index}]: index outside 0..{dims[0] - 1}")
            return f"{name}_{constant}" if len(dims) == 1 else f"{name}_{constant}_0"

        pattern = re.compile(rf"\b({_IDENT.pattern})\s*\[\s*([^\]]+?)\s*\]")
        return pattern.sub(replace, text)

    for key in ("states", "transitions", "derived_signals"):
        if key in data:
            data[key] = _walk_strings(data[key], rewrite)
    for domain in data.get("clock_domains") or []:
        if isinstance(domain, dict) and isinstance(domain.get("states"), list):
            expanded = []
            for name in domain["states"]:
                if name in array_states:
                    expanded += [f"{name}_{suffix}"
                                 for suffix in _index_tuples(array_states[name])]
                else:
                    expanded.append(name)
            domain["states"] = expanded
    return data


def _constant(token: str) -> int | None:
    text = token.strip().replace("_", "")
    if re.fullmatch(r"\d+", text):
        return int(text)
    return None


# --------------------------------------------------------------------------- #
# entry point
# --------------------------------------------------------------------------- #
def _strip_assignment_prefix(data: dict) -> dict:
    """Turn ``x = expr`` inside a derived expression into ``expr``.

    Models occasionally emit the assignment form; the backend already tolerates
    it when the left-hand side matches the signal name, but not when the model
    used a different name.  The expression position can only hold a right-hand
    side, so dropping the prefix is the only deterministic repair.

    ``<=`` is ambiguous: it is also Verilog's less-than-or-equal.  ``in_op <=
    3'd3`` is a comparison, not an assignment, so a ``<=`` prefix is only
    removed when the left-hand side is the signal's own name.
    """
    pattern = re.compile(
        r"^\s*([A-Za-z_][A-Za-z0-9_$]*)\s*(<=|=(?!=))\s*(.+)$", re.S)
    for signal in data.get("derived_signals") or []:
        if not isinstance(signal, dict):
            continue
        expression = signal.get("expression")
        if not isinstance(expression, str):
            continue
        match = pattern.match(expression)
        if not match:
            continue
        if match.group(2) == "<=" and match.group(1) != str(signal.get("name", "")).strip():
            continue
        signal["expression"] = match.group(3).strip()
    return data


def elaborate(document: dict) -> dict:
    """Lower ``functions``/``generate``/``shape`` into flat IR (in place copy)."""
    if not isinstance(document, dict):
        raise ElaborationError("IR root must be a mapping")
    data = copy.deepcopy(document)
    data = _strip_assignment_prefix(data)
    data = _inline_functions(data)
    data = _expand_generate(data)
    data = _inline_functions(data)
    data = _expand_arrays(data)
    return data

def _index_tuples(dims: list[int]) -> list[str]:
    """``[2, 3]`` -> ``["0_0", "0_1", ..., "1_2"]``."""
    tuples = [""]
    for size in dims:
        tuples = [f"{prefix}_{index}" if prefix else str(index)
                  for prefix in tuples for index in range(size)]
    return tuples
