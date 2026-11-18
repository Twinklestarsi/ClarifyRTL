"""Data model for questions and their IR sites."""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, field
from typing import Any

# S0 = a site exists; S1 = folded into an expression; S2 = structurally
# overridden (e.g. domain reset); S3 = the schema has no such concept.
S0, S1, S2, S3 = "S0", "S1", "S2", "S3"
STATUSES = (S0, S1, S2, S3)


@dataclass
class Option:
    value: str
    label: str = ""
    patch: list[dict[str, Any]] = field(default_factory=list)
    note: str = ""


@dataclass
class Question:
    """One askable decision, anchored on IR objects.

    ``anchors`` are JSON-pointer prefixes into the raw IR document: every
    difference between two candidate answers must fall under one of them.
    """

    id: str
    category: str                     # C1..C8
    template: str                     # priority_pick, reset_semantics, ...
    text: str
    anchors: list[str]
    options: list[Option]
    evidence: list[str] = field(default_factory=list)
    current: str = ""                 # option value the current IR implements
    required_construct: str = ""      # semantic carrier needed when no anchor exists yet
    gap_hypothesis: str = ""           # direct | G2 | G1 | unsupported (inventory hint only)
    depends_on: list[str] = field(default_factory=list)
    # Conditional prerequisites.  Each row is
    # {"question": "Q1", "values": ["A"]}: this question is applicable
    # only when Q1 has one of the listed answers.  ``depends_on`` remains the
    # backwards-compatible, unconditional form.
    active_when: list[dict[str, Any]] = field(default_factory=list)
    evidence_source: str = "visible_spec"
    generated_by: str = ""
    source_point: str = ""
    status: str = ""                  # detected site status
    reason: str = ""

    @property
    def fingerprint(self) -> str:
        payload = json.dumps({"category": self.category, "template": self.template,
                              "anchors": self.anchors,
                              "options": [o.value for o in self.options]},
                             sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()[:12]

    @property
    def inventory_fingerprint(self) -> str:
        """Identity that must survive representability-only restructuring.

        Anchors and patches are deliberately excluded: phase A may materialize
        them, but may not change the question or its option set.
        """
        payload = json.dumps({"id": self.id, "category": self.category,
                              "template": self.template, "text": self.text,
                              "options": [o.value for o in self.options]},
                             sort_keys=True, ensure_ascii=False)
        return hashlib.sha256(payload.encode()).hexdigest()[:16]

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "Question":
        options = [Option(**item) if isinstance(item, dict) else Option(item)
                   for item in data.get("options", [])]
        known = {f for f in cls.__dataclass_fields__ if f != "options"}
        return cls(options=options, **{k: v for k, v in data.items()
                                       if k in known})


@dataclass
class SiteVerdict:
    status: str
    anchors: list[str]
    diff_paths: list[str]
    compiles: bool
    local: bool
    detail: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status == S0

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)
