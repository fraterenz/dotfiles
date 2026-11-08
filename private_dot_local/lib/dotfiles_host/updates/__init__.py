"""Composable, staged host updates; importing this package performs no actions."""

from .operations import (
    Phase, PhaseResult, UpdateFunction, UpdateReport, UpdateTarget, run_updates,
)

__all__ = [
    "Phase", "PhaseResult", "UpdateFunction", "UpdateReport", "UpdateTarget",
    "run_updates",
]
