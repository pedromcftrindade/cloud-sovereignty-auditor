"""analysis.json: everything ExitPilot learned, in one structured file to keep working with.

    {
      "version": 1, "project": "...", "generated": "2026-11-20",
      "ai": {"provider", "model", "dependencies_analysed"} | null,
      "score": {...}, "exposure": {...},
      "resources": [ one entry per dependency: facts + "ai" block (source "ai") or null ],
      "report":   {"executive_summary", "top_risks", "next_steps", "ai_text", "markdown"},
      "analysis": {"files_read", "errors"},
      "migration": {"before", "after", "steps", "checks", "files"} | null
    }

Provenance is explicit: every field of a resource is a fact (from the parsers and the curated
catalogs, see "classified_by") except what is inside its "ai" block, which is AI output that was
validated but not verified.
"""

from __future__ import annotations

from dataclasses import asdict
from datetime import date
from typing import Any, Optional

from .analysis import Result
from .migrate import Migration
from .scoring import Exposure, Score

EXPORT_VERSION = 1


def _score(s: Score) -> dict[str, Any]:
    return {"value": s.value, "label": s.label, "counted": s.counted, "level_sum": s.level_sum,
            "by_level": {str(k): v for k, v in s.by_level.items()}}


def _exposure(e: Exposure) -> dict[str, Any]:
    return {"dependencies": e.total, "controlled_outside_eu": e.outside_eu, "controlled_in_eu": e.in_eu,
            "unknown": e.unknown, "companies_outside_eu": e.companies}


def _migration(m: Migration) -> dict[str, Any]:
    return {
        "before": {"score": _score(m.before), "exposure": _exposure(m.exposure_before)},
        "after": {"score": _score(m.after), "exposure": _exposure(m.exposure_after)},
        "projected": True,
        "steps": [asdict(s) for s in m.steps],
        "checks": [asdict(c) for c in m.checks],
        "files": sorted(m.files),
    }


def build_export(result: Result, migration: Optional[Migration] = None) -> dict[str, Any]:
    """Plain dict, ready for json.dumps."""
    report = result.report
    return {
        "version": EXPORT_VERSION,
        "project": report.project_name,
        "generated": date.today().isoformat(),
        "ai": result.ai,
        "score": _score(result.score),
        "exposure": _exposure(result.exposure) if result.exposure else None,
        "resources": [r.to_dict() for r in result.analysis.resources],
        "report": {
            "executive_summary": report.executive_summary,
            "top_risks": report.top_risks,
            "next_steps": report.next_steps,
            "changes": report.changes,
            "ai_text": report.ai_used,
            "markdown": report.markdown,
        },
        "analysis": {"files_read": result.analysis.files_read,
                     "errors": [asdict(e) for e in result.analysis.errors]},
        "suspected_waste": [asdict(w) for w in result.analysis.waste],
        "migration": _migration(migration) if migration else None,
    }
