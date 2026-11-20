"""Give every resource a category and a lock-in level (0..3).

Order of decisions:
  1. exact type in the catalog           -> confidence "high"
  2. longest matching prefix              -> confidence "high"   (e.g. aws_iam_*)
  3. remote module, by its short name     -> confidence "medium" (we cannot see inside)
  4. third-party service in services.json -> confidence "high"   (step 2b, see services.py)
  5. the LLM, for whatever is left        -> confidence "medium" (validated, see below)
  6. otherwise: unclassified              -> excluded from the score, listed in the report

Unknown third-party hosts are never sent to the LLM: who owns a service and
where that company is based are facts, and facts come from curated files.
"""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from .model import Analysis, Resource
from .services import load_services

CATALOG_PATH = Path(__file__).with_name("catalog.json")
VALID_LEVELS = {0, 1, 2, 3}


@lru_cache(maxsize=1)
def load_catalog(path: str = str(CATALOG_PATH)) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        catalog = json.load(f)
    # longest prefixes first, so "aws_route53_" wins over "aws_route"
    catalog["prefixes"] = sorted(catalog["prefixes"], key=lambda p: len(p["prefix"]), reverse=True)
    return catalog


def _apply(res: Resource, entry: dict[str, Any], catalog: dict[str, Any], by: str, confidence: str) -> None:
    category = entry["category"]
    res.category = category
    res.level = int(entry["level"])
    res.support = bool(catalog["categories"].get(category, {}).get("support", False))
    res.classified_by = by
    res.confidence = confidence
    res.note = entry.get("note", res.note)


def classify_resource(res: Resource, catalog: Optional[dict[str, Any]] = None) -> bool:
    """Classify one resource from the catalog. Returns False if it is unknown."""
    catalog = catalog or load_catalog()

    if res.origin == "service":
        entry = load_services()["services"].get(res.name)
        if entry is None:
            return False                                # unknown host: listed, never guessed
        _apply(res, entry, catalog, by="services", confidence="high")
        res.service, res.company = entry["name"], entry["company"]
        res.company_country, res.company_zone = entry["country"], entry["zone"]
        return True

    if res.origin == "module":
        slug = res.type.split(":", 1)[1]
        modules = catalog["modules"]
        key = slug if slug in modules else next(
            (k for k in sorted(modules, key=len, reverse=True) if k in slug), None
        )
        if key:
            _apply(res, modules[key], catalog, by="module", confidence="medium")
            res.note = res.note or "Remote module: estimated from its name, its internals were not analysed."
            return True
        return False

    entry = catalog["types"].get(res.type)
    if entry:
        _apply(res, entry, catalog, by="catalog", confidence="high")
        return True

    for p in catalog["prefixes"]:
        if res.type.startswith(p["prefix"]):
            _apply(res, p, catalog, by="prefix", confidence="high")
            return True
    return False


def classify(analysis: Analysis, llm=None) -> list[Resource]:
    """Classify all resources. Returns the list of resources that stayed unclassified."""
    catalog = load_catalog()
    unknown = [r for r in analysis.resources if not classify_resource(r, catalog)]

    infrastructure_unknown = [r for r in unknown if r.origin != "service"]
    if infrastructure_unknown and llm is not None and llm.available:
        _classify_with_llm(infrastructure_unknown, catalog, llm)

    still_unknown = [r for r in analysis.resources if r.level is None]
    for r in still_unknown:
        r.confidence = "none"
        r.classified_by = "none"
        if r.origin == "service":
            r.note = r.note or ("External host not in the services catalog: owner and jurisdiction "
                                "not assessed, excluded from the score.")
        else:
            r.note = r.note or "Not in the catalog: excluded from the score."
    return still_unknown


def _classify_with_llm(unknown: list[Resource], catalog: dict[str, Any], llm) -> None:
    """One single LLM request for all unknown types. Every answer is validated."""
    types = sorted({r.type for r in unknown})
    categories = sorted(catalog["categories"])
    prompt = (
        "You classify Terraform resource types by cloud vendor lock-in.\n"
        "Levels: 0 = standard, exists on every cloud; 1 = compatible with a common standard; "
        "2 = managed service with an open-source equivalent; 3 = proprietary, no direct equivalent.\n"
        f"Allowed categories (use exactly one of these strings): {json.dumps(categories)}\n"
        f"Types to classify: {json.dumps(types)}\n"
        'Answer ONLY with JSON: {"results": [{"type": "...", "category": "...", "level": 0, '
        '"reason": "max 15 words"}]}. If you do not know a type, leave it out.'
    )
    answer = llm.ask_json(prompt)
    if not isinstance(answer, dict):
        return

    valid: dict[str, dict[str, Any]] = {}
    for item in answer.get("results", []) or []:
        if not isinstance(item, dict):
            continue
        rtype, category, level = item.get("type"), item.get("category"), item.get("level")
        if isinstance(level, str) and level.strip().isdigit():
            level = int(level.strip())
        # Never trust the model blindly: unknown type, invented category or level -> discarded
        if (rtype in types and category in catalog["categories"]
                and isinstance(level, int) and not isinstance(level, bool) and level in VALID_LEVELS):
            valid[rtype] = {"category": category, "level": level,
                            "note": f"Classified by AI: {str(item.get('reason', '')).strip()[:120]}"}

    for res in unknown:
        if res.type in valid:
            _apply(res, valid[res.type], catalog, by="llm", confidence="medium")
