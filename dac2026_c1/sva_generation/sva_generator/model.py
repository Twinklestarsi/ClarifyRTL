"""Small backend AST used by the SVA lowering and emitter."""

from dataclasses import dataclass, field
from typing import List, Optional, Tuple, Union


@dataclass(frozen=True)
class ClockEvent:
    edge: str
    signal: str


@dataclass(frozen=True)
class SequenceAST:
    name: str
    body: str
    source_property: str


@dataclass(frozen=True)
class DirectBody:
    expression: str


@dataclass(frozen=True)
class ImplicationBody:
    antecedent: str
    consequent: str
    delay_min: int = 0
    delay_max: int = 0


@dataclass(frozen=True)
class RawBody:
    """Escape hatch for sequence disjunctions not represented by primitives."""

    text: str


PropertyBody = Union[DirectBody, ImplicationBody, RawBody]


@dataclass(frozen=True)
class GenerateLoop:
    """Elaboration-time loop used to check every element of a memory."""

    index: str
    stop: str
    label: str


@dataclass(frozen=True)
class PropertyAST:
    name: str
    clock: ClockEvent
    property_expression: PropertyBody
    source_property: str
    role: str = "primary"
    category: str = "unknown"
    disable: Optional[str] = None
    emit_assert: bool = True
    emit_cover: bool = False
    diagnostic_trigger: Optional[str] = None
    diagnostic_actual: Optional[str] = None
    diagnostic_expected: Optional[str] = None
    requirement_id: str = ""
    spec_source: str = ""
    generate_loop: Optional[GenerateLoop] = None

    @property
    def diagnostics(self) -> Tuple[Tuple[str, str], ...]:
        """Return only procedural expressions that may be sampled safely."""
        return tuple(
            (label, value) for label, value in (
                ("trigger", self.diagnostic_trigger),
                ("actual", self.diagnostic_actual),
                ("expected", self.diagnostic_expected),
            ) if value is not None)


@dataclass
class SVAProgram:
    module_name: str
    sequences: List[SequenceAST] = field(default_factory=list)
    properties: List[PropertyAST] = field(default_factory=list)
    warnings: List[str] = field(default_factory=list)
