class CompilerError(Exception):
    """Base class for deterministic compiler failures."""


class IRParseError(CompilerError):
    """The input is not valid YAML/JSON or has an invalid top-level shape."""


class IRValidationError(CompilerError):
    """The IR is syntactically valid but cannot deterministically become RTL."""

    def __init__(self, errors):
        self.errors = list(errors)
        super().__init__("IR validation failed:\n- " + "\n- ".join(self.errors))
