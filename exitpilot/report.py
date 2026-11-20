"""Build the lock-in report.

Golden rule: Python computes, the LLM writes.
  * Every number, level and table comes from our code.
  * The LLM only writes the executive summary, explains the top risks and
    suggests next steps, using ONLY the facts we send it.
  * Any risk that mentions a resource that does not exist is thrown away.
  * Without an LLM, a template text is used: the report is always produced.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date
from typing import Any, Optional

from .ai_analysis import ai_markdown
from .classifier import load_catalog
from .model import PROVIDER_NAMES, Analysis, Resource
from .regions import describe_region, jurisdiction_note
from .scoring import Exposure, Score, compute_exposure, compute_score, counted_resources, score_by_provider
from .services import evidence_summary

EFFORT_ORDER = {"high": 0, "medium": 1, "low": 2}


@dataclass
class Report:
    markdown: str
    facts: dict[str, Any]
    ai_used: bool
    project_name: str = "infrastructure"
    top_risks: list[dict[str, str]] = field(default_factory=list)
    executive_summary: str = ""
    next_steps: list[str] = field(default_factory=list)
    changes: list[dict[str, str]] = field(default_factory=list)   # {"item", "change", "why", "source"}


def _esc(value: Any) -> str:
    return str(value).replace("|", "\\|").replace("\n", " ")


def _category_info(category: Optional[str]) -> dict[str, Any]:
    return load_catalog()["categories"].get(category or "", {})


def ranked_main_resources(resources: list[Resource]) -> list[Resource]:
    """Main resources, worst lock-in first (level desc, then effort high->low)."""
    main = counted_resources(resources)
    return sorted(main, key=lambda r: (-(r.level or 0),
                                       EFFORT_ORDER.get(_category_info(r.category).get("effort", "low"), 2),
                                       r.address))


def service_rows(analysis: Analysis) -> list[dict[str, Any]]:
    """Third-party services (step 2b), known ones first, worst lock-in first."""
    services = [r for r in analysis.resources if r.origin == "service"]
    rows = []
    for r in sorted(services, key=lambda x: (x.level is None, -(x.level or 0), x.service or x.name)):
        rows.append({
            "address": r.address, "service": r.service or r.name, "company": r.company or "unknown",
            "country": r.company_country or "unknown", "zone": r.company_zone or "Unknown",
            "category": _category_info(r.category).get("label", r.category or "unclassified"),
            "level": r.level, "european_alternative": _category_info(r.category).get("leaseweb", ""),
            "evidence": evidence_summary(r), "verified": r.level is not None,
        })
    return rows


def region_rows(analysis: Analysis) -> list[dict[str, str]]:
    rows, seen = [], set()
    for r in analysis.resources:
        if r.origin == "service":          # services have a company, not a region: see service_rows()
            continue
        key = (r.provider, r.region)
        if key in seen:
            continue
        seen.add(key)
        info = describe_region(r.region, r.provider)
        rows.append({"provider": r.provider, "region": info["region"], "country": info["country"],
                     "zone": info["zone"], "note": jurisdiction_note(r.provider, info["zone"])})
    return sorted(rows, key=lambda x: (x["provider"], x["region"]))


def build_facts(analysis: Analysis, score: Score, unknown: list[Resource],
                exposure: Optional[Exposure] = None) -> dict[str, Any]:
    """The only information the LLM is allowed to use."""
    ranked = ranked_main_resources(analysis.resources)
    exposure = exposure or compute_exposure(analysis.resources)
    services = service_rows(analysis)
    return {
        "jurisdiction_exposure": {"dependencies": exposure.total, "controlled_outside_eu": exposure.outside_eu,
                                  "controlled_in_eu": exposure.in_eu, "unknown": exposure.unknown,
                                  "companies_outside_eu": exposure.companies},
        "third_party_services": [{k: s[k] for k in ("address", "service", "company", "country", "zone", "category", "level")}
                                 for s in services if s["verified"]],
        "unknown_external_hosts": [s["service"] for s in services if not s["verified"]],
        "sovereignty_score": score.rounded,
        "score_label": score.label,
        "main_resources_counted": score.counted,
        "resources_by_level": score.by_level,
        "providers": [PROVIDER_NAMES.get(p, p) for p in analysis.providers],
        "regions": region_rows(analysis),
        "highest_lock_in_resources": [
            {"address": r.address, "category": _category_info(r.category).get("label", r.category),
             "level": r.level, "what_changes": _category_info(r.category).get("what_changes", ""),
             "european_alternative": _category_info(r.category).get("leaseweb", ""), "note": r.note}
            for r in ranked[:8]
        ],
        "estimated_modules": [r.address for r in analysis.resources if r.origin == "module"],
        "unclassified_types": sorted({r.type for r in unknown if r.origin != "service"}),
        "files_with_errors": [e.file for e in analysis.errors],
    } | ({"suspected_waste": [{"item": w.address, "what": w.title, "why": w.reason, "confidence": w.confidence}
                              for w in analysis.waste]} if analysis.waste else {})


def _ask_llm_text(facts: dict[str, Any], llm) -> Optional[dict[str, Any]]:
    if llm is None or not llm.available:
        return None
    prompt = (
        "You are a cloud architect writing a vendor lock-in report for a non-technical manager "
        "of a European company. Use ONLY the facts below. Do not invent resources, numbers, prices "
        "or legal conclusions; say 'may be subject to', never 'illegal'.\n"
        f"FACTS:\n{json.dumps(facts, ensure_ascii=False, separators=(',', ':'))}\n\n"
        "If third_party_services is not empty, mention the jurisdiction exposure (how many dependencies "
        "are controlled by companies headquartered outside the EU) in the summary.\n"
        "Answer ONLY with JSON in this shape:\n"
        '{"executive_summary": "3-4 sentences", '
        '"top_risks": [{"address": "<exactly one address from highest_lock_in_resources>", '
        '"why": "1-2 sentences", "recommendation": "1 sentence"}], '
        '"next_steps": ["short sentence", "..."], '
        '"changes": [{"item": "<exactly one address from highest_lock_in_resources, third_party_services '
        'or suspected_waste>", "change": "what to change, concretely, in 1 sentence", "why": "1 sentence"}]}\n'
        "Give at most 3 top_risks, at most 4 next_steps and at most 6 changes. changes are the concrete "
        "modifications you suggest (replace a service with its European option, change a configuration, "
        "delete something unused), most important first."
        + ("\nsuspected_waste lists things that look unused: suggest reviewing and deleting them instead of "
           "migrating them, and never say they are certainly unused." if facts.get("suspected_waste") else "")
    )
    answer = llm.ask_json(prompt)
    return answer if isinstance(answer, dict) else None


def _validated_ai_text(answer: Optional[dict[str, Any]], facts: dict[str, Any]) -> Optional[dict[str, Any]]:
    if not answer:
        return None
    valid_addresses = {r["address"] for r in facts["highest_lock_in_resources"]}
    summary = answer.get("executive_summary")
    risks = [
        {"address": r["address"], "why": str(r.get("why", "")).strip(),
         "recommendation": str(r.get("recommendation", "")).strip()}
        for r in (answer.get("top_risks") or [])
        if isinstance(r, dict) and r.get("address") in valid_addresses   # invented resources are dropped
    ][:3]
    steps = [str(s).strip() for s in (answer.get("next_steps") or []) if str(s).strip()][:4]
    if not isinstance(summary, str) or not summary.strip():
        return None
    return {"executive_summary": summary.strip(), "top_risks": risks, "next_steps": steps,
            "changes": _validated_changes(answer.get("changes"), facts)}


def change_items(facts: dict[str, Any]) -> set[str]:
    """Addresses the AI may suggest changes for: the facts it was given, nothing else."""
    return ({r["address"] for r in facts["highest_lock_in_resources"]}
            | {s["address"] for s in facts.get("third_party_services", [])}
            | {w["item"] for w in facts.get("suspected_waste", [])})


def _validated_changes(raw: Any, facts: dict[str, Any], limit: int = 6) -> list[dict[str, str]]:
    allowed, seen, out = change_items(facts), set(), []
    for c in raw if isinstance(raw, list) else []:
        if not isinstance(c, dict):
            continue
        item = str(c.get("item", "")).strip().strip("`'\" ")      # small models add `backticks` or quotes
        change = " ".join(str(c.get("change", "")).split())[:240]
        why = " ".join(str(c.get("why", "")).split())[:240]
        if item in allowed and item not in seen and change:      # invented items are dropped
            seen.add(item)
            out.append({"item": item, "change": change, "why": why, "source": "ai"})
        if len(out) == limit:
            break
    return out


def _template_changes(analysis: Analysis, facts: dict[str, Any], limit: int = 6) -> list[dict[str, str]]:
    """Without AI: the same list, written from the catalogs and the waste findings."""
    out = [{"item": w.address, "change": "Review it with the team, then delete it instead of migrating it.",
            "why": w.reason, "source": "catalog"} for w in analysis.waste if w.exclude_from_migration]
    for r in facts["highest_lock_in_resources"]:
        if r["level"] >= 2 and r["european_alternative"]:
            out.append({"item": r["address"], "change": f"Move it to: {r['european_alternative']}.",
                        "why": r["what_changes"], "source": "catalog"})
    return out[:limit]


def _effort_of(analysis: Analysis, address: str) -> str:
    res = next((r for r in analysis.resources if r.address == address), None)
    return _category_info(res.category if res else None).get("effort", "unknown")


def _template_text(analysis: Analysis, score: Score, facts: dict[str, Any]) -> dict[str, Any]:
    infra = [p for p in facts["providers"] if p != PROVIDER_NAMES["saas"]]
    providers = ", ".join(infra) or "no known cloud provider"
    level3 = score.by_level.get(3, 0)
    if level3 == 0:
        effort = ("No component depends on a proprietary service without a European equivalent; "
                  "the migration effort is mainly in re-hosting managed services.")
    else:
        noun = "component depends" if level3 == 1 else "components depend"
        effort = (f"{level3} {noun} on proprietary services with no direct European equivalent, "
                  "which is where most of the migration effort would go.")
    services = facts.get("third_party_services", [])
    if services:
        what = (f"We analysed {score.counted} main components: {score.counted - len(services)} infrastructure "
                f"resources on {providers} and {len(services)} third-party services the application uses. ")
    else:
        what = f"We analysed {score.counted} main infrastructure components running on {providers}. "
    exp = facts.get("jurisdiction_exposure", {})
    exposure = ""
    if services and exp.get("dependencies"):
        exposure = (f" {exp['controlled_outside_eu']} of {exp['dependencies']} dependencies are controlled by "
                    "companies headquartered outside the EU, even when their servers are in Europe.")
    summary = (
        what
        + f"The sovereignty score is {score.rounded if score.rounded is not None else 'n/a'}/100 "
        f"({score.label.lower()}). {effort}{exposure}"
    )
    risks = [
        {"address": r["address"],
         "why": f"{r['category']} (level {r['level']}). {r['what_changes']}",
         "recommendation": (f"Address this early in the migration plan (estimated effort: "
                            f"{_effort_of(analysis, r['address'])}).")}
        for r in facts["highest_lock_in_resources"] if r["level"] >= 2
    ][:3]
    steps = ["Review the highest lock-in components with the application team.",
             "Confirm data residency requirements for each region listed.",
             "Generate a migration plan to Leaseweb (phase 2)."]
    return {"executive_summary": summary, "top_risks": risks, "next_steps": steps,
            "changes": _template_changes(analysis, facts)}


def generate_report(analysis: Analysis, unknown: list[Resource], llm=None,
                    project_name: str = "infrastructure") -> Report:
    score = compute_score(analysis.resources)
    by_provider = score_by_provider(analysis.resources)
    exposure = compute_exposure(analysis.resources)
    facts = build_facts(analysis, score, unknown, exposure)

    ai_text = _validated_ai_text(_ask_llm_text(facts, llm), facts)
    ai_used = ai_text is not None
    text = ai_text or _template_text(analysis, score, facts)
    if ai_used and not text["top_risks"]:          # the AI gave no valid risks: use ours
        text["top_risks"] = _template_text(analysis, score, facts)["top_risks"]

    lines: list[str] = []
    add = lines.append
    model_info = (f"AI text by {getattr(llm, 'label', 'AI')} ({llm.model})" if ai_used
                  else "AI not used (template text)")
    add(f"# Lock-in report: {project_name}")
    add(f"_Generated by ExitPilot on {date.today().isoformat()}. {model_info}. "
        "This report is technical information, not legal advice._\n")

    add("## 1. Executive summary\n")
    if score.value is None:
        add("**Sovereignty score: n/a** (no classified main resources were found).\n")
    else:
        add(f"**Sovereignty score: {score.rounded}/100 ({score.label})**\n")
    add(text["executive_summary"] + "\n")
    if len(by_provider) > 1:
        add("| Provider | Score | Main resources |\n|---|---|---|")
        for p, s in by_provider.items():
            add(f"| {PROVIDER_NAMES.get(p, p)} | {s.rounded if s.rounded is not None else 'n/a'} | {s.counted} |")
        add("")

    add("## 2. Where the data is\n")
    if facts["regions"]:
        add("| Provider | Region | Country | Zone | Note |\n|---|---|---|---|---|")
        for row in facts["regions"]:
            add(f"| {_esc(PROVIDER_NAMES.get(row['provider'], row['provider']))} | {_esc(row['region'])} | "
                f"{_esc(row['country'])} | {_esc(row['zone'])} | {_esc(row['note'])} |")
    else:
        add("No cloud infrastructure was declared (no Terraform resources).")
    add("")

    rows = service_rows(analysis)
    if rows:
        add("### Third-party services the application uses\n")
        add("Found in configuration, code, dependencies, container images and network traffic. "
            "The zone is where the **company** is headquartered: a European data centre does not change "
            "which laws the company is subject to.\n")
        if exposure.total:
            add(f"**Jurisdiction exposure: {exposure.outside_eu} of {exposure.total} dependencies are controlled "
                f"by companies headquartered outside the EU** ({exposure.in_eu} in the EU, {exposure.unknown} unknown).\n")
        add("| Service | Company | Headquarters | Zone | Category | Level | Evidence |\n|---|---|---|---|---|---|---|")
        for s in rows:
            level = "n/a" if s["level"] is None else s["level"]
            name = s["service"] if s["verified"] else f"{s['service']} (not verified)"
            add(f"| {_esc(name)} | {_esc(s['company'])} | {_esc(s['country'])} | {_esc(s['zone'])} | "
                f"{_esc(s['category'])} | {level} | {_esc(s['evidence'])} |")
        add("")

    add("## 3. Biggest lock-in points\n")
    if not text["top_risks"]:
        add("No main resource above level 1 was found.\n")
    by_address = {r.address: r for r in analysis.resources}
    for i, risk in enumerate(text["top_risks"], 1):
        res = by_address.get(risk["address"])
        info = _category_info(res.category if res else None)
        add(f"**{i}. `{risk['address']}`** - {info.get('label', '?')}, level {res.level if res else '?'}")
        add(f"- Why: {risk['why']}")
        add(f"- Recommendation: {risk['recommendation']}")
        add(f"- Leaseweb option: {info.get('leaseweb', '-')}\n")

    changes = text.get("changes") or (_template_changes(analysis, facts) if ai_used else [])
    if changes:
        by_source = "suggested by the AI from the facts above, not verified" if changes[0]["source"] == "ai" \
            else "from the catalogs"
        add(f"### What to change ({by_source})\n")
        add("| # | Item | Change | Why |\n|---|---|---|---|")
        for i, c in enumerate(changes, 1):
            add(f"| {i} | `{_esc(c['item'])}` | {_esc(c['change'])} | {_esc(c['why'])} |")
        add("")

    lines.extend(ai_markdown(analysis))

    add("## 4. Full inventory\n")
    add("| Resource | Category | Level | Leaseweb option | Effort | Confidence |\n|---|---|---|---|---|---|")
    for r in ranked_main_resources(analysis.resources):
        info = _category_info(r.category)
        add(f"| `{_esc(r.address)}` | {_esc(info.get('label', r.category))} | {r.level} | "
            f"{_esc(info.get('leaseweb', '-'))} | {_esc(info.get('effort', '-'))} | {r.confidence} |")
    support = [r for r in analysis.resources if r.support]
    if support:
        noun = "resource" if len(support) == 1 else "resources"
        add(f"\n**Associated configuration (not counted in the score): {len(support)} {noun}**\n")
        add("| Resource | Category | File |\n|---|---|---|")
        for r in sorted(support, key=lambda x: x.address):
            add(f"| `{_esc(r.address)}` | {_esc(_category_info(r.category).get('label', r.category))} | {_esc(r.file)} |")
    add("")

    if analysis.waste:
        add(f"### Suspected waste: {len(analysis.waste)} item(s) to review before migrating\n")
        add("These look unused in the analysed files (found by ExitPilot, not by the AI). Nothing was deleted: "
            "review each one. Items marked *left out* are not translated in the migration plan (phase 2) "
            "unless you include them.\n")
        add("| Item | What | Why | Confidence | Migration |\n|---|---|---|---|---|")
        for w in analysis.waste:
            where = "; ".join(f"{e['file']}" + (f":{e['line']}" if e.get("line") else "") for e in w.evidence[:3])
            add(f"| `{_esc(w.address)}` | {_esc(w.title)} | {_esc(w.reason)} ({_esc(where)}) | {w.confidence} | "
                f"{'left out' if w.exclude_from_migration else 'kept (clean up only)'} |")
        add("")

    add("## 5. What we could not fully analyse\n")
    limits = []
    for e in analysis.errors:
        limits.append(f"- File `{e.file}` could not be read ({_esc(e.message)}).")
    for r in analysis.resources:
        if r.origin == "module":
            limits.append(f"- Remote module `{r.name}` (`{r.config.get('source', '')}`): estimated from its name.")
        if r.classified_by == "llm":
            limits.append(f"- `{r.address}` was classified by AI (medium confidence).")
    for t in facts["unclassified_types"]:
        limits.append(f"- Type `{t}` is not in the catalog and was excluded from the score.")
    guesses = {r.service or r.name: r.ai["owner_guess"] for r in analysis.resources
               if r.origin == "service" and r.ai and r.ai.get("owner_guess")}
    for host in facts["unknown_external_hosts"]:
        suggestion = ""
        if host in guesses:
            g = guesses[host]
            suggestion = (f" The AI suggests it may be operated by {g['company']}"
                          + (f" ({g['country']})" if g.get("country") else "")
                          + ": unverified, confirm it before adding it to services.json.")
        limits.append(f"- External host `{host}` is not in the services catalog: owner and jurisdiction "
                      f"not assessed, excluded from the score.{suggestion}")
    add("\n".join(limits) if limits else "Everything was analysed.")
    add("")

    add("## 6. Next steps\n")
    for s in text["next_steps"]:
        add(f"- {s}")
    add("\n---\n")
    add("_How the score works: S = 100 x (1 - N / (3 x R)), where R is the number of main resources "
        f"(here {score.counted}) and N the sum of their lock-in levels (here {score.level_sum}). "
        "Levels: 0 standard, 1 compatible with a standard, 2 managed service with an open-source "
        "equivalent, 3 proprietary._")

    return Report(markdown="\n".join(lines), facts=facts, ai_used=ai_used, project_name=project_name,
                  top_risks=text["top_risks"], executive_summary=text["executive_summary"],
                  next_steps=text["next_steps"], changes=changes)
