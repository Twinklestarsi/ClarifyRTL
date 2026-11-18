import json
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

import yaml

from Compiler.rtl_compiler.errors import IRParseError, IRValidationError
from Compiler.rtl_compiler.parser import parse_ir
from Compiler.rtl_compiler.validator import validate_ir

from .error import Checker2Error
from .normalizer import normalize_ir, records


def _issue(error_type, location, field, message, repair):
    return {
        "error_type": error_type,
        "location": location,
        "field": field,
        "message": message,
        "repair": repair,
        "severity": "error",
    }


def _raw_reset_checks(data):
    errors = []
    for domain, location in records(data.get("clock_domains", {}), "clock_domains"):
        reset = domain.get("reset")
        if not reset:
            continue
        if not domain.get("reset_type"):
            errors.append(_issue(
                "Missing Reset Semantics", location, "reset_type",
                "Clock domain has reset but no reset_type",
                "Set reset_type to synchronous or asynchronous from the specification"))
        if not domain.get("reset_polarity"):
            errors.append(_issue(
                "Missing Reset Semantics", location, "reset_polarity",
                "Clock domain has reset but no reset_polarity",
                "Set reset_polarity to active_high or active_low from the specification"))
    return errors


def _resource_connections(data):
    connected = set()

    def walk(value):
        if isinstance(value, dict):
            for child in value.values():
                walk(child)
        elif isinstance(value, list):
            for child in value:
                walk(child)
        elif isinstance(value, str) and value.isidentifier():
            connected.add(value)

    for resource, _ in records(data.get("resources", {}), "resources"):
        walk(resource.get("ports", {}))
    return connected


def _output_driver_checks(model, data):
    drivers = defaultdict(list)
    ports = model.port_map()
    for state in model.states:
        if state.name in ports and ports[state.name].direction == "output":
            drivers[state.name].append(f"state:{state.name}")
    for signal in model.derived_signals:
        name = str(signal.get("name", ""))
        if name in ports:
            drivers[name].append(f"derived_signal:{name}")
    for transition in model.transitions:
        if transition.transition_type != "combinational":
            continue
        for action in transition.actions:
            if action.base_target in ports:
                drivers[action.base_target].append(
                    f"transition:{transition.name}")
    resource_connections = _resource_connections(data)
    for output in resource_connections:
        if output in ports and ports[output].direction == "output" and not drivers[output]:
            drivers[output].append(f"resource_connection:{output}")

    errors = []
    for port in model.ports:
        if port.direction != "output":
            continue
        sources = list(dict.fromkeys(drivers[port.name]))
        if not sources:
            errors.append(_issue(
                "Undriven Output", f"$.module.ports.{port.name}", "driver",
                f"Output {port.name!r} has no state, derived signal, transition, or resource driver",
                "Map the output to a state or add a derived/combinational assignment"))
        elif len(sources) > 1:
            errors.append(_issue(
                "Multiple Output Drivers", f"$.module.ports.{port.name}", "driver",
                f"Output {port.name!r} has multiple drivers: {sources}",
                "Keep exactly one output driver"))
    return errors, dict(drivers)


def check_ir(ir_path, report_path=None, *, raise_on_error=True,
             backup_path=None):
    """Normalize and validate hardware semantics before RTL compilation."""
    source = Path(ir_path).resolve()
    report_output = Path(report_path or source.with_name("checker2_report.json")).resolve()
    report_output.parent.mkdir(parents=True, exist_ok=True)
    errors = []
    warnings = []
    normalizations = []
    backup_file = None
    output_drivers = {}
    try:
        original = source.read_text(encoding="utf-8")
        data = yaml.safe_load(original)
        if not isinstance(data, dict):
            raise ValueError("IR root must be a mapping")
        errors.extend(_raw_reset_checks(data))
        normalizations, normalization_warnings = normalize_ir(data)
        warnings.extend(normalization_warnings)
        normalized = yaml.safe_dump(
            data, allow_unicode=True, sort_keys=False, default_flow_style=False)
        if normalizations and normalized != original:
            backup = Path(backup_path or source.with_name(
                source.stem + ".checker2_input" + source.suffix)).resolve()
            backup.write_text(original, encoding="utf-8")
            source.write_text(normalized, encoding="utf-8")
            backup_file = str(backup)
        model = parse_ir(source)
        try:
            compiler_warnings = validate_ir(model)
            warnings.extend({"location": "$", "message": warning}
                            for warning in compiler_warnings)
        except IRValidationError as exc:
            errors.extend(_issue(
                "Compiler Semantic Error", "$", None, message,
                "Correct the referenced IR hardware semantic field")
                for message in exc.errors)
        driver_errors, output_drivers = _output_driver_checks(model, data)
        errors.extend(driver_errors)
    except (OSError, UnicodeError, yaml.YAMLError, ValueError, IRParseError) as exc:
        errors.append(_issue(
            "Checker2 Input Error", "$", None, str(exc),
            "Run checker1 first and provide compiler-readable YAML/JSON IR"))

    status = "failed" if errors else "normalized" if normalizations else "passed"
    report = {
        "checker": "checker2",
        "scope": "hardware semantic consistency and deterministic normalization",
        "status": status,
        "valid": not errors,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "input_ir": str(source),
        "normalization_count": len(normalizations),
        "normalizations": normalizations,
        "normalizations_applied": bool(backup_file),
        "backup_ir": backup_file,
        "output_drivers": output_drivers,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
    }
    report_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    if errors and raise_on_error:
        raise Checker2Error(report_output, errors)
    return report
