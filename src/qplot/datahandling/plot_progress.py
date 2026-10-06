"""Immutable, bounded progress observations; no callbacks or Qt dependencies."""

from dataclasses import dataclass


@dataclass(frozen=True)
class PlotProgress:
    phase: str
    completed: int = 0
    total: int | None = None
    unit: str = "scan"
    stage: int = 1

    @property
    def title(self) -> str:
        return f"Stage {self.stage}/2: {self.phase}"

