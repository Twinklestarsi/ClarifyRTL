class Checker2Error(Exception):
    """Raised when normalized IR still violates hardware semantic rules."""

    def __init__(self, report_path, errors):
        self.report_path = str(report_path)
        self.errors = list(errors)
        first = self.errors[0]["message"] if self.errors else "unknown semantic error"
        super().__init__(
            f"checker2 failed with {len(self.errors)} error(s): {first}; "
            f"report: {self.report_path}")
