import json
from datetime import datetime, timezone
from pathlib import Path

from .error import Checker1Error
from .parser import parse_text
from .repair import auto_repair_text


def check_ir(ir_path, report_path=None, *, raise_on_error=True,
             auto_repair=True, backup_path=None):
    """Repair deterministic format issues, re-parse, and write a JSON report."""
    source = Path(ir_path).resolve()
    report_output = Path(report_path or source.with_name("checker1_report.json")).resolve()
    report_output.parent.mkdir(parents=True, exist_ok=True)
    backup_file = None
    initial_issues = []
    repairs_applied = False
    try:
        original = source.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        original = ""
        from .error import CheckIssue
        issues = [CheckIssue(
            "File Error", "$", None, str(exc),
            "Ensure the IR file exists and is valid UTF-8")]
        initial_issues = issues
        repaired_text, repairs = original, []
    else:
        _, initial_issues = parse_text(original)
        if auto_repair:
            repaired_text, repairs, issues = auto_repair_text(original)
        else:
            repaired_text, repairs, issues = original, [], initial_issues
        if repairs and not issues and repaired_text != original:
            backup = Path(backup_path or source.with_name(
                source.stem + ".checker1_input" + source.suffix)).resolve()
            backup.write_text(original, encoding="utf-8")
            source.write_text(repaired_text, encoding="utf-8")
            backup_file = str(backup)
            repairs_applied = True
            yaml_status = source.parent / "yaml_validation.txt"
            if yaml_status.exists():
                yaml_status.write_text(
                    "OK: IR was repaired and validated by checker1.\n",
                    encoding="utf-8")
    stages = {
        "initial_parse": "failed" if initial_issues else "passed",
        "auto_repair": ("repaired" if repairs_applied else
                        "failed" if issues else "not_needed"),
        "final_parse": "failed" if issues else "passed",
    }
    errors = [issue.to_dict() for issue in issues if issue.severity == "error"]
    warnings = [issue.to_dict() for issue in issues if issue.severity == "warning"]
    status = "failed" if errors else "repaired" if repairs_applied else "passed"
    report = {
        "checker": "checker1",
        "scope": "deterministically auto-repairable IR format issues only",
        "status": status,
        "valid": not errors,
        "checked_at": datetime.now(timezone.utc).isoformat(),
        "input_ir": str(source),
        "stages": stages,
        "auto_repair_enabled": auto_repair,
        "repair_count": len(repairs),
        "repairs_applied": repairs_applied,
        "repairs": repairs,
        "backup_ir": backup_file,
        "error_count": len(errors),
        "warning_count": len(warnings),
        "errors": errors,
        "warnings": warnings,
    }
    report_output.write_text(
        json.dumps(report, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8")
    if errors and raise_on_error:
        raise Checker1Error(report_output, errors)
    return report
