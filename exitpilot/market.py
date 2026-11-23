"""Market study: which European provider fits this infrastructure best, for this company.

From the analysis it works out what has to be hosted (machines and their sizes, managed services)
and compares the providers in providers.json on three facts, all computed in Python:

  * price      the monthly cost of the machines, cheapest plan that fits each one
               (list prices, EUR excl. VAT; storage, traffic and IPv4 not included)
  * proximity  distance from the company's city to the provider's nearest EU data centre,
               plus the theoretical minimum round-trip time over fibre (about 1 ms per 100 km)
  * coverage   how many of the managed services the infrastructure uses the provider offers
               (what it does not offer must be self-hosted: more operations work)

    score = 100 x (wp x P + wd x D + wc x C + wa x A) / (wp + wd + wc + wa)

    P = cheapest cost / this provider's cost     (1 for the cheapest)
    D = (nearest + 50) / (this distance + 50)    (1 for the closest; +50 km avoids dividing by ~0)
    C = managed services covered / needed         (1 when nothing managed is needed)
    A = 1 if ExitPilot already generates Terraform for the provider (phase 2), else 0
    wp, wd, wc, wa = the weights chosen by the user (DEFAULT_WEIGHTS: 40, 30, 30, 30)

Why automation has a weight by default: a provider for which the migration is already generated and
checked (Terraform, cloud-init, docker-compose: see migrate.py) is cheaper and less risky to move to, so
it counts as much as proximity or managed services. It is a visible weight, not a hidden bonus: set it
to 0 in the app or with weights={"automation": 0} for a study that ignores ExitPilot's own automation.

Providers whose plans cannot be ordered today are listed, but never recommended. The AI (optional)
only writes the recommendation text from these facts; its pick must be one of the providers.
"""

from __future__ import annotations

import json
import math
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from .model import Analysis

PROVIDERS_PATH = Path(__file__).with_name("providers.json")
DEFAULT_SIZE = (2, 4.0)                       # vCPU, GB RAM when the size is not known
SERVICES_HOST = (2, 8.0)                      # one machine for the self-hosted services (see migrate.py)
# Default weights of the score (see the module docstring for why automation is not 0)
DEFAULT_WEIGHTS = {"price": 40.0, "proximity": 30.0, "coverage": 30.0, "automation": 30.0}

# Where the company is (for proximity). Approximate city centres.
CITIES: dict[str, tuple[float, float]] = {
    "Lisbon": (38.72, -9.14), "Porto": (41.15, -8.61), "Madrid": (40.42, -3.70), "Barcelona": (41.39, 2.17),
    "Paris": (48.86, 2.35), "Brussels": (50.85, 4.35), "Amsterdam": (52.37, 4.90), "Frankfurt": (50.11, 8.68),
    "Berlin": (52.52, 13.40), "Vienna": (48.21, 16.37), "Milan": (45.46, 9.19), "Rome": (41.90, 12.50),
    "Dublin": (53.35, -6.26), "Copenhagen": (55.68, 12.57), "Stockholm": (59.33, 18.07),
    "Warsaw": (52.23, 21.01), "Athens": (37.98, 23.73),
}
# Where today's hyperscaler regions are (for the map). Approximate.
REGION_COORDS: dict[str, tuple[str, float, float]] = {
    "eu-west-1": ("Dublin", 53.35, -6.26), "eu-west-2": ("London", 51.51, -0.13), "eu-west-3": ("Paris", 48.86, 2.35),
    "eu-central-1": ("Frankfurt", 50.11, 8.68), "eu-north-1": ("Stockholm", 59.33, 18.07),
    "eu-south-1": ("Milan", 45.46, 9.19), "eu-south-2": ("Aragón", 41.65, -0.88),
    "us-east-1": ("N. Virginia", 39.04, -77.49), "us-east-2": ("Ohio", 40.00, -83.00),
    "us-west-1": ("N. California", 37.35, -121.96), "us-west-2": ("Oregon", 45.60, -121.20),
    "westeurope": ("Netherlands", 52.37, 4.90), "northeurope": ("Dublin", 53.35, -6.26),
    "francecentral": ("Paris", 48.86, 2.35), "germanywestcentral": ("Frankfurt", 50.11, 8.68),
    "swedencentral": ("Gävle", 60.67, 17.14), "italynorth": ("Milan", 45.46, 9.19),
    "spaincentral": ("Madrid", 40.42, -3.70), "polandcentral": ("Warsaw", 52.23, 21.01),
    "uksouth": ("London", 51.51, -0.13), "eastus": ("Virginia", 37.37, -79.83),
    "europe-west1": ("Belgium", 50.47, 3.82), "europe-west3": ("Frankfurt", 50.11, 8.68),
    "europe-west4": ("Netherlands", 53.44, 6.83), "europe-west9": ("Paris", 48.86, 2.35),
    "europe-west8": ("Milan", 45.46, 9.19), "europe-southwest1": ("Madrid", 40.42, -3.70),
    "europe-north1": ("Finland", 60.57, 27.19), "europe-central2": ("Warsaw", 52.23, 21.01),
    "europe-west2": ("London", 51.51, -0.13), "us-central1": ("Iowa", 41.26, -95.86),
}
# Known machine sizes (vCPU, GB RAM) of the hyperscalers' common types
SIZES: dict[str, tuple[int, float]] = {
    "t3.nano": (2, 0.5), "t3.micro": (2, 1), "t3.small": (2, 2), "t3.medium": (2, 4), "t3.large": (2, 8),
    "t3.xlarge": (4, 16), "t3.2xlarge": (8, 32), "t2.micro": (1, 1), "t2.small": (1, 2), "t2.medium": (2, 4),
    "t4g.small": (2, 2), "t4g.medium": (2, 4), "m5.large": (2, 8), "m5.xlarge": (4, 16), "m5.2xlarge": (8, 32),
    "m6i.large": (2, 8), "m6i.xlarge": (4, 16), "c5.large": (2, 4), "c5.xlarge": (4, 8), "r5.large": (2, 16),
    "standard_b1s": (1, 1), "standard_b1ms": (1, 2), "standard_b2s": (2, 4), "standard_b2ms": (2, 8),
    "standard_d2s_v5": (2, 8), "standard_d4s_v5": (4, 16), "standard_d2s_v3": (2, 8), "standard_d4s_v3": (4, 16),
    "e2-micro": (2, 1), "e2-small": (2, 2), "e2-medium": (2, 4), "e2-standard-2": (2, 8), "e2-standard-4": (4, 16),
    "n2-standard-2": (2, 8), "n2-standard-4": (4, 16),
}
COMPUTE_CATEGORIES = {"compute", "web_app_platform"}
# Category of the analysis -> managed capability a provider may offer
CAPABILITY_OF = {
    "object_storage": "object_storage", "database": "managed_database", "proprietary_database": "managed_database",
    "kubernetes": "managed_kubernetes", "containers": "managed_kubernetes", "load_balancer": "load_balancer",
    "dns": "dns", "cdn": "cdn", "messaging": "managed_messaging", "serverless": "serverless",
}
# Categories that phase 2 self-hosts on one extra machine (see migrate.py)
SELF_HOSTED = {"database", "proprietary_database", "cache", "messaging", "identity", "observability"}


@lru_cache(maxsize=1)
def load_providers(path: str = str(PROVIDERS_PATH)) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        return json.load(f)


# ------------------------------------------------------------------ what has to be hosted
@dataclass
class Machine:
    source: str                     # address in the analysis, or "self-hosted services"
    vcpu: int
    ram_gb: float
    size_known: bool


def _size_of(config: dict[str, Any]) -> Optional[tuple[int, float]]:
    for key in ("instance_type", "size", "machine_type", "instance_class"):
        value = config.get(key)
        if isinstance(value, str) and "${" not in value:
            name = value.lower().removeprefix("db.")
            if name in SIZES:
                return SIZES[name]
    return None


def _count_of(config: dict[str, Any]) -> int:
    value = config.get("count")
    return value if isinstance(value, int) and not isinstance(value, bool) and value > 0 else 1


def machines_needed(analysis: Analysis, skip_waste: bool = True) -> list[Machine]:
    """Machines to host: every compute resource (sizes from the type), plus one for self-hosted services."""
    waste = {w.address for w in analysis.waste if w.exclude_from_migration or w.kind == "disabled_resource"} \
        if skip_waste else {w.address for w in analysis.waste if w.kind == "disabled_resource"}
    machines: list[Machine] = []
    self_hosted = False
    for r in analysis.resources:
        if r.address in waste or r.level is None:
            continue
        if r.category in COMPUTE_CATEGORIES and not r.support and r.origin != "service":
            size = _size_of(r.config)
            vcpu, ram = size or DEFAULT_SIZE
            for _ in range(_count_of(r.config)):
                machines.append(Machine(r.address, vcpu, ram, size is not None))
        if r.category in SELF_HOSTED and r.origin != "service":
            self_hosted = True
    if self_hosted:
        machines.append(Machine("self-hosted services (database, cache, queues...)", *SERVICES_HOST, True))
    return machines


def capabilities_needed(analysis: Analysis, skip_waste: bool = True) -> list[str]:
    waste = {w.address for w in analysis.waste if w.exclude_from_migration} if skip_waste else set()
    needed = {CAPABILITY_OF[r.category] for r in analysis.resources
              if r.category in CAPABILITY_OF and r.origin != "service" and not r.support
              and r.address not in waste and r.level is not None}
    return sorted(needed)


# ------------------------------------------------------------------ comparing providers
def distance_km(a: tuple[float, float], b: tuple[float, float]) -> float:
    """Great-circle distance (haversine)."""
    lat1, lon1, lat2, lon2 = map(math.radians, (a[0], a[1], b[0], b[1]))
    h = math.sin((lat2 - lat1) / 2) ** 2 + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2
    return 6371.0 * 2 * math.asin(math.sqrt(h))


@dataclass
class Offer:
    provider: str                   # id in providers.json
    name: str
    hq: str
    monthly_eur: Optional[float]    # None when no plan fits
    lines: list[dict[str, Any]]     # one per machine: {"source", "plan", "vcpu", "ram_gb", "eur_month"}
    orderable: bool                 # False when a chosen plan is marked unavailable
    nearest_dc: str
    distance_km: float
    rtt_ms: float                   # theoretical minimum round trip over fibre
    covered: list[str]
    missing: list[str]
    terraform: str
    phase2_automated: bool
    note: str
    source: str
    score: float = 0.0
    parts: dict[str, float] = field(default_factory=dict)   # P, D, C


@dataclass
class MarketStudy:
    city: str
    weights: dict[str, float]
    machines: list[Machine]
    needed: list[str]
    offers: list[Offer]             # best first; offers that cannot be ordered come last
    checked: str
    include_basic: bool
    recommendation: str = ""
    recommendation_source: str = "template"     # "template" | "ai"

    @property
    def best(self) -> Optional[Offer]:
        return next((o for o in self.offers if o.orderable and o.monthly_eur is not None), None)

    def best_by(self, key: str) -> Optional[Offer]:
        candidates = [o for o in self.offers if o.orderable and o.monthly_eur is not None]
        if not candidates:
            return None
        if key == "price":
            return min(candidates, key=lambda o: o.monthly_eur)
        if key == "proximity":
            return min(candidates, key=lambda o: o.distance_km)
        return max(candidates, key=lambda o: (len(o.covered), -o.monthly_eur))


def _cheapest_plan(plans: list[dict[str, Any]], vcpu: int, ram: float, include_basic: bool) -> Optional[dict]:
    fitting = [p for p in plans if p["vcpu"] >= vcpu and p["ram_gb"] >= ram and (include_basic or not p.get("basic"))]
    if not fitting:
        return None
    # prefer plans that can be ordered; among them the cheapest
    return min(fitting, key=lambda p: (p.get("available", True) is False, p["eur_month"]))


def study(analysis: Analysis, city: str = "Lisbon", weights: Optional[dict[str, float]] = None,
          skip_waste: bool = True, include_basic: bool = False, eu_only: bool = True) -> MarketStudy:
    data = load_providers()
    where = CITIES.get(city, CITIES["Lisbon"])
    # floats: the Streamlit sliders give 40, the web app 40.0. The same study must give the same AI prompt
    # (and so the same cached answer) in both interfaces.
    weights = {k: float(v) for k, v in (DEFAULT_WEIGHTS | (weights or {})).items()}
    machines = machines_needed(analysis, skip_waste)
    needed = capabilities_needed(analysis, skip_waste)
    offers: list[Offer] = []

    for pid, p in data["providers"].items():
        lines, total, orderable = [], 0.0, True
        for m in machines:
            plan = _cheapest_plan(p["plans"], m.vcpu, m.ram_gb, include_basic)
            if plan is None:
                total = None
                break
            orderable = orderable and plan.get("available", True) is not False
            total += plan["eur_month"]
            lines.append({"source": m.source, "plan": plan["name"], "vcpu": plan["vcpu"],
                          "ram_gb": plan["ram_gb"], "eur_month": plan["eur_month"]})
        dcs = [d for d in p["datacentres"] if d["zone"] == "EU" or not eu_only]
        nearest = min(dcs, key=lambda d: distance_km(where, (d["lat"], d["lon"])))
        km = distance_km(where, (nearest["lat"], nearest["lon"]))
        covered = [c for c in needed if c in p["capabilities"]]
        offers.append(Offer(
            provider=pid, name=p["name"], hq=p["hq"], monthly_eur=None if total is None else round(total, 2),
            lines=lines, orderable=orderable, nearest_dc=f"{nearest['city']} ({nearest['country']})",
            distance_km=round(km), rtt_ms=round(km / 100, 1), covered=covered,
            missing=[c for c in needed if c not in covered], terraform=p.get("terraform", ""),
            phase2_automated=bool(p.get("phase2_automated")), note=p.get("note", ""), source=p.get("source", "")))

    priced = [o for o in offers if o.monthly_eur is not None]
    cheapest = min((o.monthly_eur for o in priced), default=None)
    nearest_km = min(o.distance_km for o in offers)
    total_w = sum(weights.values()) or 1.0
    for o in offers:
        price = (cheapest / o.monthly_eur) if (o.monthly_eur and cheapest is not None) else (1.0 if not machines else 0.0)
        prox = (nearest_km + 50) / (o.distance_km + 50)
        cov = len(o.covered) / len(needed) if needed else 1.0
        auto = 1.0 if o.phase2_automated else 0.0
        o.parts = {"P": round(price, 3), "D": round(prox, 3), "C": round(cov, 3), "A": auto}
        o.score = round(100 * (weights["price"] * price + weights["proximity"] * prox
                               + weights["coverage"] * cov + weights["automation"] * auto) / total_w, 1)
    offers.sort(key=lambda o: (not o.orderable or o.monthly_eur is None, -o.score, o.name))
    result = MarketStudy(city=city, weights=weights, machines=machines, needed=needed, offers=offers,
                         checked=data.get("checked", ""), include_basic=include_basic)
    result.recommendation = template_recommendation(result)
    return result


# ------------------------------------------------------------------ recommendation text
def _label(cap: str) -> str:
    return load_providers()["capability_labels"].get(cap, cap)


def labels(caps: list[str]) -> str:
    """'Managed SQL database, CDN' (for tables)."""
    return ", ".join(_label(c) for c in caps)


def template_recommendation(s: MarketStudy) -> str:
    best = s.best
    if best is None:
        return "No provider in the catalogue can host these machines with the plans that can be ordered today."
    parts = [f"For a company in {s.city}, {best.name} ({best.hq}) gives the best balance with these weights: "
             f"about EUR {best.monthly_eur:.2f}/month for {len(s.machines)} machine(s), nearest EU data centre in "
             f"{best.nearest_dc} ({best.distance_km} km)"]
    if s.needed:
        parts.append(f", and {len(best.covered)} of {len(s.needed)} managed services available")
    text = "".join(parts) + "."
    if best.missing:
        text += " To self-host on it: " + ", ".join(_label(c) for c in best.missing) + "."
    cheap, close = s.best_by("price"), s.best_by("proximity")
    if cheap and cheap.provider != best.provider:
        text += f" Cheapest: {cheap.name} (EUR {cheap.monthly_eur:.2f}/month)."
    if close and close.provider != best.provider:
        text += f" Closest: {close.name} ({close.distance_km} km, {close.nearest_dc})."
    blocked = [o.name for o in s.offers if not o.orderable]
    if blocked:
        text += f" Not recommended because their plans cannot be ordered today: {', '.join(blocked)}."
    return text


def facts_for_ai(s: MarketStudy) -> dict[str, Any]:
    return {
        "company_city": s.city, "weights": s.weights, "machines": len(s.machines),
        "managed_services_needed": [_label(c) for c in s.needed],
        "providers": [{"id": o.provider, "name": o.name, "headquarters": o.hq, "score": o.score,
                       "monthly_eur": o.monthly_eur, "orderable_today": o.orderable,
                       "nearest_eu_datacentre": o.nearest_dc, "distance_km": o.distance_km,
                       "missing_managed_services": [_label(c) for c in o.missing],
                       "terraform_provider": o.terraform, "exitpilot_generates_terraform": o.phase2_automated,
                       "note": o.note}
                      for o in s.offers],
    }


def ai_recommendation(s: MarketStudy, llm) -> bool:
    """Ask the AI to write the recommendation from the facts. Validated: the pick must be an orderable
    provider of the list and the text must name it. Returns True if the AI text was used."""
    if llm is None or not getattr(llm, "available", False) or s.best is None:
        return False
    facts = facts_for_ai(s)
    prompt = (
        "You advise a European company that wants to leave US cloud providers. Choose ONE provider from "
        "the list for its infrastructure and explain the choice in 3-4 sentences for a manager. Use ONLY "
        "these facts; do not invent prices, features or locations. A provider with orderable_today=false "
        "must not be chosen. Mention the trade-offs (price, distance, managed services to self-host).\n"
        f"FACTS:\n{json.dumps(facts, ensure_ascii=False, separators=(',', ':'))}\n"
        'Answer ONLY with JSON: {"pick": "<provider id>", "recommendation": "3-4 sentences"}'
    )
    answer = llm.ask_json(prompt)
    if not isinstance(answer, dict):
        return False
    pick = str(answer.get("pick", "")).strip().strip("`'\" ").lower()
    text = " ".join(str(answer.get("recommendation", "")).split())[:900]
    offer = next((o for o in s.offers if o.provider == pick), None)
    if offer is None or not offer.orderable or not text or offer.name.lower() not in text.lower():
        return False
    # Numbers in the text must be numbers we gave it (prices, km, scores, counts)
    allowed = {f"{v:g}" for o in s.offers for v in (o.monthly_eur or 0, o.distance_km, o.score, o.rtt_ms)}
    allowed |= {str(len(s.machines)), str(len(s.needed))} | {str(x) for x in range(0, 11)}
    numbers = set(re.findall(r"\d+(?:\.\d+)?", text.replace(",", "")))
    if any(n not in allowed and n.rstrip("0").rstrip(".") not in allowed for n in numbers):
        return False
    s.recommendation, s.recommendation_source = text, "ai"
    return True


# ------------------------------------------------------------------ map
def map_points(analysis: Analysis, s: MarketStudy) -> list[dict[str, Any]]:
    """Points for st.map: the company, today's regions, the recommended provider and the others."""
    points = []
    lat, lon = CITIES.get(s.city, CITIES["Lisbon"])
    points.append({"lat": lat, "lon": lon, "label": f"Your company ({s.city})", "kind": "company",
                   "color": "#1e88e5", "size": 30000})
    for region in sorted({r.region for r in analysis.resources if r.region}):
        if region.lower() in REGION_COORDS:
            name, rlat, rlon = REGION_COORDS[region.lower()]
            points.append({"lat": rlat, "lon": rlon, "label": f"Today: {region} ({name})", "kind": "today",
                           "color": "#c62828", "size": 40000})
    data = load_providers()["providers"]
    best = s.best.provider if s.best else None
    for o in s.offers:
        for d in data[o.provider]["datacentres"]:
            if d["zone"] != "EU":
                continue
            recommended = o.provider == best
            points.append({"lat": d["lat"], "lon": d["lon"], "label": f"{o.name}: {d['city']}",
                           "kind": "recommended" if recommended else "alternative",
                           "color": "#2e7d32" if recommended else "#9e9e9e",
                           "size": 35000 if recommended else 15000})
    return points


def market_markdown(s: MarketStudy) -> str:
    lines = [f"## Market study: where to go ({s.city})\n",
             f"_Indicative list prices checked on {s.checked}, EUR excl. VAT, compute only (storage, traffic and "
             "IPv4 not included). Weights: " + ", ".join(f"{k} {v:g}" for k, v in s.weights.items()) + "._\n",
             s.recommendation + ("  \n_(written by the AI from these facts)_" if s.recommendation_source == "ai" else ""),
             "", "| # | Provider | Score | EUR/month | Nearest EU data centre | km | Managed services missing | Orderable |",
             "|---|---|---|---|---|---|---|---|"]
    for i, o in enumerate(s.offers, 1):
        cost = "n/a" if o.monthly_eur is None else f"{o.monthly_eur:.2f}"
        missing = ", ".join(_label(c) for c in o.missing) or "-"
        lines.append(f"| {i} | {o.name} ({o.hq}) | {o.score:g} | {cost} | {o.nearest_dc} | {o.distance_km} | "
                     f"{missing} | {'yes' if o.orderable else 'no'} |")
    lines.append("\nSources: " + "; ".join(f"{o.name}: {o.source}" for o in s.offers))
    return "\n".join(lines) + "\n"
