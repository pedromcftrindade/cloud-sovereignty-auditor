"""The AI analyses every dependency and returns structured data (JSON) that the rest of the
program uses: the report, the migration plan and the exported analysis.json.

For each dependency (a cloud resource, a remote module or a third-party service) the model is
asked, in small batches, for:

    risk              low | medium | high   how hard and risky it is to leave
    why_it_matters    1-2 sentences for the people who decide
    migration_notes   up to 4 short steps, specific to this dependency
    code_change_hint  one sentence on what changes in the application code
    confidence        low | medium | high
    owner_guess       only for an unknown host: {company, country}, and only if the model is sure

What the AI is trusted with, and what it is not:

  * It sees only facts ExitPilot already extracted: type, category, level, region, company, where
    the dependency was found. Never the contents of configuration files: no passwords, keys or values.
  * Every answer is validated. An address that was not asked, a risk outside the list, a text that is
    too long or a field of the wrong type is dropped or trimmed. Nothing is accepted blindly.
  * Everything it says is stored in Resource.ai with source "ai", the provider and the model, so the
    reader can always tell an AI statement from a fact.
  * Facts stay with curated files: levels, companies and jurisdictions come from catalog.json and
    services.json. An owner_guess is a suggestion for a person to confirm. It does not change the score
    or the jurisdiction exposure: the host stays "unknown" until it is added to services.json.
"""

from __future__ import annotations

import json
from typing import Any, Optional

from .classifier import load_catalog
from .model import Analysis, Resource
from .services import evidence_summary

BATCH_SIZE = 8
RISKS = ("low", "medium", "high")
CONFIDENCE = ("low", "medium", "high")
MAX_WHY = 280
MAX_NOTE = 200
MAX_NOTES = 4
MAX_HINT = 240
MAX_NAME = 60

# Settings that describe the size or flavour of a resource and can never hold a secret.
SAFE_SETTINGS = ("engine", "engine_version", "instance_class", "instance_type", "size", "machine_type",
                 "runtime", "billing_mode", "sku_name", "tier", "node_type", "memory_size")


def _category_info(category: Optional[str]) -> dict[str, Any]:
    return load_catalog()["categories"].get(category or "", {})


def dependencies_to_analyse(analysis: Analysis) -> list[Resource]:
    """Main resources and services (support configuration is not analysed), plus unknown hosts."""
    return [r for r in analysis.resources
            if not r.support and (r.level is not None or r.origin == "service")]


def facts_for(res: Resource) -> dict[str, Any]:
    """The only facts about a dependency that are sent to the model."""
    info = _category_info(res.category)
    kind = {"service": "third-party service", "module": "remote Terraform module"}.get(res.origin, "cloud resource")
    item: dict[str, Any] = {
        "address": res.address, "kind": kind,
        "category": info.get("label", res.category or "unclassified"),
        "lock_in_level": res.level, "region": res.region,
    }
    if res.origin == "service":
        item.update({"service": res.service or res.name, "company": res.company,
                     "company_headquarters": res.company_country, "found_in": evidence_summary(res, 4)})
        if res.level is None:
            item["owner_unknown"] = True
    else:
        item.update({"provider": res.provider, "type": res.type, "file": res.file})
        settings = {k: res.config[k] for k in SAFE_SETTINGS
                    if isinstance(res.config.get(k), (str, int, float)) and not isinstance(res.config.get(k), bool)
                    and "${" not in str(res.config.get(k))}
        if settings:
            item["settings"] = settings
    item["european_option"] = info.get("leaseweb")
    return {k: v for k, v in item.items() if v not in (None, "")}


def _prompt(batch: list[Resource]) -> str:
    has_unknown = any(r.origin == "service" and r.level is None for r in batch)
    owner_rule = (
        'For a dependency marked "owner_unknown", you MAY add "owner_guess": {"company": "...", "country": "..."} '
        "but ONLY if you are sure who operates it; otherwise leave owner_guess out. Never guess.\n"
    ) if has_unknown else ""
    return (
        "You are a cloud migration engineer helping a European company reduce its dependence on "
        "providers headquartered outside the EU.\n"
        "For EACH dependency below, use ONLY the facts given. Do not invent versions, prices, features, "
        "company details or legal conclusions.\n"
        f"{owner_rule}"
        "Answer ONLY with JSON in this shape, one item per dependency, with the address copied exactly:\n"
        '{"items": [{"address": "<exactly as given>", "risk": "low|medium|high", '
        '"why_it_matters": "1-2 sentences", "migration_notes": ["up to 4 short, specific steps"], '
        '"code_change_hint": "one sentence about the application code, or empty", '
        '"confidence": "low|medium|high"}]}\n'
        'risk = how hard and risky it is to move away from this dependency.\n'
        f"DEPENDENCIES:\n{json.dumps([facts_for(r) for r in batch], ensure_ascii=False, separators=(',', ':'))}"
    )


def _text(value: Any, limit: int) -> str:
    return " ".join(str(value).split())[:limit].strip() if isinstance(value, (str, int, float)) else ""


def clean_item(raw: Any, res: Resource) -> Optional[dict[str, Any]]:
    """Validate one item the model returned for `res`. Returns the block to store, or None."""
    if not isinstance(raw, dict):
        return None
    why = _text(raw.get("why_it_matters"), MAX_WHY)
    notes = [_text(n, MAX_NOTE) for n in (raw.get("migration_notes") if isinstance(raw.get("migration_notes"), list) else [])]
    notes = [n for n in notes if n][:MAX_NOTES]
    if not why and not notes:
        return None                                              # nothing useful: discard
    risk = _text(raw.get("risk"), 10).lower()
    confidence = _text(raw.get("confidence"), 10).lower()
    block: dict[str, Any] = {
        "source": "ai",
        "risk": risk if risk in RISKS else None,
        "why_it_matters": why,
        "migration_notes": notes,
        "code_change_hint": _text(raw.get("code_change_hint"), MAX_HINT),
        "confidence": confidence if confidence in CONFIDENCE else "low",
    }
    guess = raw.get("owner_guess")
    if res.origin == "service" and res.level is None and isinstance(guess, dict):
        company, country = _text(guess.get("company"), MAX_NAME), _text(guess.get("country"), MAX_NAME)
        if company:
            block["owner_guess"] = {"company": company, "country": country, "verified": False}
    return block


def items_of(answer: Any) -> Optional[list]:
    """The list of items in the model's answer. Small models vary the wrapper, so accept
    {"items": [...]}, [{"items": [...]}] and a bare list of {"address": ...} objects."""
    if isinstance(answer, dict):
        items = answer.get("items")
        return items if isinstance(items, list) else None
    if isinstance(answer, list):
        if len(answer) == 1 and isinstance(answer[0], dict) and isinstance(answer[0].get("items"), list):
            return answer[0]["items"]
        if answer and all(isinstance(x, dict) for x in answer) and any("address" in x for x in answer):
            return answer
    return None


def enrich(analysis: Analysis, llm) -> int:
    """Ask the model to analyse every dependency. Stores the validated answers in Resource.ai.
    Returns how many dependencies were analysed. Does nothing (returns 0) without a usable model."""
    if llm is None or not getattr(llm, "available", False):
        return 0
    targets = dependencies_to_analyse(analysis)
    done = 0
    for start in range(0, len(targets), BATCH_SIZE):
        batch = targets[start:start + BATCH_SIZE]
        items = items_of(llm.ask_json(_prompt(batch)))
        if items is None:
            continue
        by_address = {r.address: r for r in batch}               # only addresses we asked about
        for raw in items:
            res = by_address.get(raw.get("address")) if isinstance(raw, dict) else None
            if res is None or res.ai is not None:
                continue
            block = clean_item(raw, res)
            if block:
                block["provider"] = getattr(llm, "label", "AI")
                block["model"] = getattr(llm, "model", "")
                res.ai = block
                done += 1
    return done


RISK_ORDER = {"high": 0, "medium": 1, "low": 2, None: 3}


def ai_markdown(analysis: Analysis, limit: int = 12) -> list[str]:
    """Report lines with the AI analysis (empty when there is none)."""
    analysed = [r for r in analysis.resources if r.ai]
    if not analysed:
        return []
    first = analysed[0].ai
    lines = ["### AI analysis of the main dependencies\n",
             f"_Written by {first.get('provider', 'AI')} ({first.get('model', '')}) from the facts above and "
             "not verified by ExitPilot. Levels, companies and jurisdictions come from the catalogs, "
             "not from the AI._\n"]
    ranked = sorted(analysed, key=lambda r: (RISK_ORDER.get(r.ai["risk"], 3), r.address))
    for r in ranked[:limit]:
        ai = r.ai
        name = r.service or r.address if r.origin == "service" else r.address
        lines.append(f"**`{r.address}`** ({name if name != r.address else r.category or 'dependency'}) - "
                     f"risk: {ai['risk'] or 'n/a'}, confidence: {ai['confidence']}")
        if ai["why_it_matters"]:
            lines.append(f"- Why it matters: {ai['why_it_matters']}")
        if ai["migration_notes"]:
            lines.append("- Steps: " + "; ".join(ai["migration_notes"]))
        if ai["code_change_hint"]:
            lines.append(f"- Code: {ai['code_change_hint']}")
        lines.append("")
    if len(ranked) > limit:
        lines.append(f"_{len(ranked) - limit} more dependencies are in the exported analysis.json._\n")
    return lines
