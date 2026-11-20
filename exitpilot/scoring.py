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

from .model import PROVIDER_NAMES, Resource
from .regions import EU_PROVIDERS, US_PROVIDERS


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


# ---------------------------------------------------------------- jurisdiction exposure
# The score above measures how hard it is to leave (technical lock-in). It does not look at
# who controls each dependency. Exposure answers that second question: of the dependencies
# that count, how many are controlled by a company headquartered outside the EU?

@dataclass
class Exposure:
    total: int                                     # dependencies looked at (counted resources)
    outside_eu: int                                # controlled by a company headquartered outside the EU
    in_eu: int
    unknown: int                                   # company or jurisdiction not known
    companies: dict[str, int] = field(default_factory=dict)   # company -> dependencies outside the EU

    @property
    def label(self) -> str:
        if self.total == 0:
            return "n/a"
        return f"{self.outside_eu} of {self.total} controlled outside the EU"


def company_of(res: Resource) -> tuple[Optional[str], Optional[str]]:
    """(company, zone of its headquarters) for any resource, Terraform or service.

    An explicit company (services, or the projection made by migrate.py) wins over the provider."""
    if res.origin == "service" or res.company_zone:
        return res.company, res.company_zone
    if res.provider in US_PROVIDERS:
        return PROVIDER_NAMES.get(res.provider, res.provider), "Outside Europe"
    if res.provider in EU_PROVIDERS:
        return EU_PROVIDERS[res.provider]
    return None, None


def compute_exposure(resources: list[Resource]) -> Exposure:
    counted = counted_resources(resources)
    outside, inside, unknown = 0, 0, 0
    companies: dict[str, int] = {}
    for res in counted:
        company, zone = company_of(res)
        if zone == "EU":
            inside += 1
        elif zone in ("Outside Europe", "Europe (non-EU)"):
            outside += 1
            companies[company or "?"] = companies.get(company or "?", 0) + 1
        else:
            unknown += 1
    unknown += sum(1 for r in resources if r.origin == "service" and r.level is None)  # unknown hosts
    total = len(counted) + sum(1 for r in resources if r.origin == "service" and r.level is None)
    companies = dict(sorted(companies.items(), key=lambda kv: (-kv[1], kv[0])))
    return Exposure(total=total, outside_eu=outside, in_eu=inside, unknown=unknown, companies=companies)
