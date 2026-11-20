"""Sovereignty score.

    S = 100 * (1 - N / (3 * R))

    R = number of main resources that were classified (support resources and
        unclassified resources are left out)
    N = sum of their lock-in levels (0..3)
    3 * R = worst possible case (every resource at level 3)

S goes from 0 (everything proprietary) to 100 (no lock-in at all).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Optional

from .model import Resource


@dataclass
class Score:
    value: Optional[float]                 # None when there is nothing to score
    counted: int                           # R
    level_sum: int                         # N
    by_level: dict[int, int] = field(default_factory=dict)   # level -> number of resources

    @property
    def rounded(self) -> Optional[int]:
        return None if self.value is None else int(round(self.value))

    @property
    def label(self) -> str:
        if self.value is None:
            return "n/a"
        if self.value >= 70:
            return "Low lock-in"
        if self.value >= 40:
            return "Moderate lock-in"
        return "High lock-in"


def counted_resources(resources: list[Resource]) -> list[Resource]:
    """Resources that enter the formula: classified and not support."""
    return [r for r in resources if r.level is not None and not r.support]


def compute_score(resources: list[Resource]) -> Score:
    counted = counted_resources(resources)
    r = len(counted)
    n = sum(res.level for res in counted)
    by_level = {lvl: sum(1 for res in counted if res.level == lvl) for lvl in range(4)}
    if r == 0:
        return Score(value=None, counted=0, level_sum=0, by_level=by_level)
    value = 100 * (1 - n / (3 * r))
    return Score(value=round(value, 1), counted=r, level_sum=n, by_level=by_level)


def score_by_provider(resources: list[Resource]) -> dict[str, Score]:
    providers = sorted({r.provider for r in resources})
    return {p: compute_score([r for r in resources if r.provider == p]) for p in providers}
