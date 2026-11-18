from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class Parameter:
    name: str
    value: str


@dataclass
class Port:
    name: str
    direction: str
    width: Any = 1
    datatype: str = "wire"
    signed: bool = False


@dataclass
class ClockDomain:
    name: str
    clock: str
    edge: str = "posedge"
    reset: Optional[str] = None
    reset_type: str = "synchronous"
    reset_polarity: str = "active_high"
    states: List[str] = field(default_factory=list)


@dataclass
class State:
    name: str
    type: str
    width: Any
    clock_domain: str
    reset_value: str = "0"
    datatype: str = "unsigned"
    depth: Any = None
    initial_value: Optional[str] = None
    initial_enable: Optional[str] = None
    write_mask: Optional[str] = None


@dataclass
class Assignment:
    target: str
    expression: str

    @property
    def base_target(self):
        return self.target.split("[", 1)[0].strip()


@dataclass
class Transition:
    name: str
    transition_type: str
    clock_domain: Optional[str]
    guard: str
    actions: List[Assignment]
    else_actions: List[Assignment]
    latency: Any = 0
    update_semantics: str = ""
    trigger: str = ""


@dataclass
class SequentialProcess:
    name: str
    clock_domain: str
    transitions: List[str]


@dataclass
class IRModel:
    module_name: str
    parameters: List[Parameter]
    ports: List[Port]
    clock_domains: List[ClockDomain]
    states: List[State]
    derived_signals: List[Dict[str, Any]]
    transitions: List[Transition]
    processes: List[SequentialProcess]
    priorities: List[tuple]
    resources: List[Dict[str, Any]]
    cdc: List[Dict[str, Any]]
    raw: Dict[str, Any]
    warnings: List[str] = field(default_factory=list)

    def port_map(self):
        return {item.name: item for item in self.ports}

    def domain_map(self):
        return {item.name: item for item in self.clock_domains}

    def state_map(self):
        return {item.name: item for item in self.states}

    def transition_map(self):
        return {item.name: item for item in self.transitions}
