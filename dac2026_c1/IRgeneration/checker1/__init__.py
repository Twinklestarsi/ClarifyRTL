"""Deterministic format auto-repair checker for HW-PN IR candidates."""

from .checker import check_ir
from .error import Checker1Error

__all__ = ["check_ir", "Checker1Error"]
