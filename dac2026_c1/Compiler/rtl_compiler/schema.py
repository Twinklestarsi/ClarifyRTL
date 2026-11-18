"""Strict key schema for the hardware IR document.

The parser historically ignored anything it did not recognise, so an IR that
declared ``functions:`` or ``states[].shape`` silently lost that behaviour.
This module makes the accepted vocabulary explicit:

* keys the compiler actually reads are *supported*;
* keys the prompt documents but the compiler does not use are *ignored* and
  reported as warnings (never silently);
* anything else is an error.
"""

from __future__ import annotations

TOP_LEVEL_SUPPORTED = {
    "module", "clock_domains", "states", "derived_signals", "transitions",
    "sequential_processes", "priority", "priorities", "resources", "cdc",
    # Compile-time abstraction, lowered by elaborate.py before the model is built.
    "functions", "generate",
    # Parser aliases kept for older documents.
    "places",
}
TOP_LEVEL_IGNORED = {
    # Documented in the generation prompt but not consumed by the compiler.
    "concurrency", "timing", "data_model",
}
MODULE_KEYS = {"name", "parameters", "ports"}
PORT_KEYS = {"name", "direction", "dir", "width", "datatype", "type", "signed",
             # Accepted but not compiled: reported as a warning, never dropped silently.
             "msb", "lsb"}
STATE_KEYS = {"name", "type", "width", "clock_domain", "reset_value",
              "initial_value", "initial_enable", "write_mask",
              "datatype", "depth", "shape"}
TRANSITION_KEYS = {"name", "clock_domain", "trigger", "guard", "action",
                   "else_action", "latency", "transition_type",
                   "update_semantics"}
CLOCK_DOMAIN_KEYS = {"name", "clock", "edge", "reset", "reset_polarity",
                     "reset_type", "states"}
DERIVED_KEYS = {"name", "width", "expression", "dependencies"}
# Redundant annotations emitted by models.  They are surfaced as warnings rather
# than errors because dropping them cannot change behaviour.
ANNOTATION_KEYS = {"range", "reg", "is_reg", "description", "comment", "note",
                   "doc", "role", "purpose"}


def _check_items(items, allowed: set[str], label: str) -> tuple[list[str], list[str]]:
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(items, list):
        return errors, warnings
    for index, item in enumerate(items):
        if not isinstance(item, dict):
            continue
        extra = set(item) - allowed
        structural = sorted(extra - ANNOTATION_KEYS)
        annotations = sorted(extra & ANNOTATION_KEYS)
        if structural:
            errors.append(f"{label}[{index}]: unknown key(s) {structural}; "
                          f"the compiler would silently ignore them")
        if annotations:
            warnings.append(f"{label}[{index}]: annotation key(s) {annotations} "
                            f"are ignored")
    return errors, warnings


def check_keys(data: dict) -> tuple[list[str], list[str]]:
    """Return ``(errors, warnings)`` for the document's key vocabulary."""
    errors: list[str] = []
    warnings: list[str] = []
    if not isinstance(data, dict):
        return ["IR root must be a mapping"], warnings

    unknown = sorted(set(data) - TOP_LEVEL_SUPPORTED - TOP_LEVEL_IGNORED)
    if unknown:
        errors.append(
            f"unknown top-level key(s) {unknown}; supported keys are "
            f"{sorted(TOP_LEVEL_SUPPORTED)} and documented-but-ignored keys "
            f"{sorted(TOP_LEVEL_IGNORED)}")
    for key in sorted(TOP_LEVEL_IGNORED & set(data)):
        warnings.append(f"{key}: documented in the prompt but not compiled "
                        f"(ignored on purpose)")

    module = data.get("module")
    if isinstance(module, dict):
        extra = sorted(set(module) - MODULE_KEYS)
        if extra:
            errors.append(f"module: unknown key(s) {extra}")
        port_errors, port_warnings = _check_items(module.get("ports"), PORT_KEYS,
                                                   "module.ports")
        errors += port_errors
        warnings += port_warnings

    for key, allowed in (("states", STATE_KEYS), ("transitions", TRANSITION_KEYS),
                         ("clock_domains", CLOCK_DOMAIN_KEYS),
                         ("derived_signals", DERIVED_KEYS)):
        item_errors, item_warnings = _check_items(data.get(key), allowed, key)
        errors += item_errors
        warnings += item_warnings
    return errors, warnings