from pathlib import Path

import yaml

from .error import CheckIssue


class DuplicateKeyError(yaml.MarkedYAMLError):
    pass


class UniqueKeyLoader(yaml.SafeLoader):
    pass


def _construct_mapping(loader, node, deep=False):
    mapping = {}
    for key_node, value_node in node.value:
        key = loader.construct_object(key_node, deep=deep)
        if key in mapping:
            raise DuplicateKeyError(
                context="while constructing a mapping",
                context_mark=node.start_mark,
                problem=f"duplicate key {key!r}",
                problem_mark=key_node.start_mark)
        mapping[key] = loader.construct_object(value_node, deep=deep)
    return mapping


UniqueKeyLoader.add_constructor(
    yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping)


def _syntax_repair(message):
    lowered = message.lower()
    if "constructor for the tag" in lowered or "tag" in lowered:
        return "Wrap Verilog expressions beginning with '!' in quotes, for example guard: \"!rst_n\""
    if "flow mapping" in lowered or "expected ',' or '}'" in lowered:
        return "Wrap Verilog concatenation/expression text in quotes instead of leaving {...} as YAML syntax"
    if "duplicate key" in lowered:
        return "Remove or rename the duplicate mapping key"
    return "Correct YAML/JSON syntax at the reported line and column"


def parse_text(text):
    """Parse IR text while rejecting duplicate mapping keys."""
    try:
        data = yaml.load(text, Loader=UniqueKeyLoader)
    except yaml.MarkedYAMLError as exc:
        mark = exc.problem_mark or exc.context_mark
        message = exc.problem or str(exc).splitlines()[0]
        return None, [CheckIssue(
            "Invalid YAML/JSON Syntax", "$", None, message,
            _syntax_repair(message), line=mark.line + 1 if mark else None,
            column=mark.column + 1 if mark else None)]
    except yaml.YAMLError as exc:
        return None, [CheckIssue(
            "Invalid YAML/JSON Syntax", "$", None, str(exc),
            _syntax_repair(str(exc)))]
    except (TypeError, ValueError) as exc:
        return None, [CheckIssue(
            "Invalid YAML/JSON Syntax", "$", None, str(exc),
            "Replace complex/non-scalar mapping keys with normal string field names")]
    if not isinstance(data, dict):
        return None, [CheckIssue(
            "Invalid Data Structure", "$", None,
            "IR document root must be a mapping/object",
            "Replace the document root with a mapping containing module, states, and transitions")]
    return data, []


def parse_document(path):
    ir_path = Path(path)
    try:
        text = ir_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        return None, [CheckIssue(
            "File Error", "$", None, str(exc),
            "Ensure the IR file exists and is valid UTF-8")]
    return parse_text(text)
