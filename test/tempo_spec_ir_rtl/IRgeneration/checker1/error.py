from dataclasses import asdict, dataclass
from typing import Optional


@dataclass
class CheckIssue:
    error_type: str
    location: str
    field: Optional[str]
    message: str
    repair: str
    severity: str = "error"
    line: Optional[int] = None
    column: Optional[int] = None

    def to_dict(self):
        return {key: value for key, value in asdict(self).items()
                if value is not None}


class Checker1Error(Exception):
    """Raised when checker1 rejects an IR candidate."""

    def __init__(self, report_path, errors):
        self.report_path = str(report_path)
        self.errors = errors
        summary = errors[0]["message"] if errors else "unknown checker error"
        super().__init__(
            f"checker1 failed with {len(errors)} error(s): {summary}; "
            f"report: {self.report_path}")
