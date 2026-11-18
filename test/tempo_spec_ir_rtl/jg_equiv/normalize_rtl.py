#!/usr/bin/env python3
"""Mechanical legalization of the generated RTL so that JasperGold can parse it.

The deterministic RTL backend emits parameter defaults that are copied from the
natural-language specification (``clog2(...)``, ``max(1, ...)``, an empty string
parameter, an unquoted string parameter).  Those constructs are not legal
SystemVerilog, so an equivalence checker cannot even read the file.  This module
performs the smallest mechanical rewrite that turns such expressions into legal
constant expressions.  It never touches the design structure (no logic, no
state, no port changes): only parameter default expressions are rewritten and
the module name is renamed so spec and implementation can coexist in one
JasperGold session.

Every transformation is recorded in ``.normalization.json`` next to the
rewritten file so the experiment stays auditable.
"""

from __future__ import annotations

import json
import re
import sys
from pathlib import Path

_IDENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\Z")
_MAX_CALL = re.compile(r"(?<![A-Za-z0-9_$.])max\s*\(")
_CLOG2_CALL = re.compile(r"(?<![A-Za-z0-9_$])clog2\s*\(")


def _split_top_level(text: str, separator: str = ",") -> list[str]:
    parts: list[str] = []
    depth = 0
    current: list[str] = []
    for char in text:
        if char in "([{":
            depth += 1
        elif char in ")]}":
            depth -= 1
        if char == separator and depth == 0:
            parts.append("".join(current))
            current = []
        else:
            current.append(char)
    parts.append("".join(current))
    return parts


def _match_paren(text: str, open_index: int) -> int:
    """Return the index just past the parenthesis matching text[open_index]."""
    depth = 0
    for index in range(open_index, len(text)):
        if text[index] == "(":
            depth += 1
        elif text[index] == ")":
            depth -= 1
            if depth == 0:
                return index + 1
    raise ValueError("unbalanced parentheses")


def _rewrite_max(expression: str, log: list[str]) -> str:
    out: list[str] = []
    index = 0
    while index < len(expression):
        match = _MAX_CALL.match(expression, index)
        if not match:
            out.append(expression[index])
            index += 1
            continue
        open_index = match.end() - 1
        close_index = _match_paren(expression, open_index)
        arguments = [
            _rewrite(part, log)
            for part in _split_top_level(expression[open_index + 1 : close_index - 1])
        ]
        value = arguments[-1].strip()
        for argument in reversed(arguments[:-1]):
            argument = argument.strip()
            value = f"((({argument}) > ({value})) ? ({argument}) : ({value}))"
        log.append(f"max({', '.join(a.strip() for a in arguments)}) -> {value}")
        out.append(value)
        index = close_index
    return "".join(out)


def _rewrite(expression: str, log: list[str]) -> str:
    expression = _rewrite_max(expression, log)
    rewritten, count = _CLOG2_CALL.subn("$clog2(", expression)
    if count:
        log.append(f"clog2(...) -> $clog2(...) x{count}")
    return rewritten


def _string_parameters(reference_text: str) -> set[str]:
    return set(re.findall(r"parameter\s+string\s+([A-Za-z_][A-Za-z0-9_]*)", reference_text))


def _module_parameter_block(text: str, module: str) -> tuple[int, int] | None:
    match = re.search(r"\bmodule\s+" + re.escape(module) + r"\s*#\s*\(", text)
    if not match:
        return None
    open_index = match.end() - 1
    return open_index, _match_paren(text, open_index)


def _fix_parameter_block(block: str, string_names: set[str], log: list[str]) -> str:
    body = block[block.index("(") + 1 : -1]
    rebuilt: list[str] = []
    for entry in _split_top_level(body):
        match = re.match(
            r"(\s*parameter\s+)(.*)\b([A-Za-z_][A-Za-z0-9_]*)\s*=\s*(.*)\Z",
            entry,
            re.S,
        )
        if not match:
            rebuilt.append(entry)
            continue
        prefix, name, value = match.group(1) + match.group(2), match.group(3), match.group(4).strip()
        if value == "":
            new_value = '""' if name in string_names else "0"
            log.append(f"{name}: empty default -> {new_value}")
        elif _IDENT.match(value) and name in string_names:
            new_value = f'"{value}"'
            log.append(f"{name}: bare identifier {value!r} -> {new_value}")
        else:
            new_value = _rewrite(value, log)
        rebuilt.append(f"{prefix}{name} = {new_value}")
    return "(" + ",".join(rebuilt) + ")"


def normalize(generated_text: str, reference_text: str, old_module: str, new_module: str) -> tuple[str, list[str]]:
    log: list[str] = []
    string_names = _string_parameters(reference_text)
    located = _module_parameter_block(generated_text, old_module)
    text = generated_text
    if located:
        open_index, close_index = located
        text = (
            text[:open_index]
            + _fix_parameter_block(text[open_index:close_index], string_names, log)
            + text[close_index:]
        )
    text = re.sub(r"\bmodule\s+" + re.escape(old_module) + r"\b", f"module {new_module}", text, count=1)
    log.append(f"module {old_module} -> {new_module}")
    return text, log


def main(argv: list[str]) -> int:
    if len(argv) != 4:
        print("usage: normalize_rtl.py <generated_rtl.v> <reference.sv> <new_module>", file=sys.stderr)
        return 2
    generated = Path(argv[1])
    reference = Path(argv[2])
    new_module = argv[3]
    text = generated.read_text(encoding="utf-8")
    old_module = re.search(r"\bmodule\s+([A-Za-z_][A-Za-z0-9_]*)", text).group(1)
    normalized, log = normalize(text, reference.read_text(encoding="utf-8"), old_module, new_module)
    generated.with_suffix(".normalized.v").write_text(normalized, encoding="utf-8")
    (generated.parent / (generated.stem + ".normalization.json")).write_text(
        json.dumps({"source": str(generated), "module_old": old_module, "module_new": new_module, "steps": log}, indent=2)
        + "\n",
        encoding="utf-8",
    )
    print(json.dumps(log, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))