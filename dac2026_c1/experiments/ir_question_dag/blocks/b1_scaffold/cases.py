"""Case index built directly from the dataset manifest (no corpus copying)."""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path


@dataclass(frozen=True)
class Case:
    case_id: str          # e.g. "RTLLM/Control/Finite State Machine/fsm"
    dataset: str          # RTLLM | VerilogEval | Tempo26
    short: str            # e.g. "fsm"
    prompt: Path
    reference: Path
    generated: Path | None  # one-shot RTL; None when generation never produced RTL
    evidence: Path
    root: Path

    @property
    def key(self) -> str:
        """Filesystem-safe identifier used for run directories."""
        return f"{self.dataset}__{self.short}"

    def prompt_text(self) -> str:
        return self.prompt.read_text(encoding="utf-8")

    def reference_text(self) -> str:
        return self.reference.read_text(encoding="utf-8", errors="replace")

    def evidence_data(self) -> dict:
        return json.loads(self.evidence.read_text(encoding="utf-8"))


def manifest_path(case_root: Path | str) -> Path:
    return Path(case_root) / "manifest.json"


def load_cases(case_root: Path | str, *, dataset: str | None = None,
               names: list[str] | None = None) -> list[Case]:
    root = Path(case_root)
    manifest = json.loads(manifest_path(root).read_text(encoding="utf-8"))
    rows = manifest["cases"] if isinstance(manifest, dict) else manifest
    wanted = {name.lower() for name in names} if names else None
    cases: list[Case] = []
    for row in rows:
        if dataset and row["dataset"] != dataset:
            continue
        case = Case(
            case_id=row["case_id"], dataset=row["dataset"], short=row["short"],
            prompt=root / row["prompt"], reference=root / row["reference"],
            generated=(root / row["generated"]) if row.get("generated") else None,
            evidence=root / row["evidence"],
            root=root,
        )
        if wanted and case.short.lower() not in wanted:
            continue
        cases.append(case)
    return cases


def get_case(case_root: Path | str, name: str) -> Case:
    for case in load_cases(case_root):
        if name in (case.short, case.key, case.case_id):
            return case
    raise KeyError(f"case not found: {name}")