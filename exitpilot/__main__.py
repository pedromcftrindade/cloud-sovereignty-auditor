"""Command line interface.

    python -m exitpilot examples/loja-mista            # Markdown report, no AI
    python -m exitpilot examples/loja-mista --json     # JSON summary (handy for tests and fixtures)
    python -m exitpilot examples/loja-mista --ai --out analysis.json   # everything, in one structured file
    python -m exitpilot my-project --ai                # AI text (Mistral by default, see LLM_PROVIDER in llm.py)
    LLM_PROVIDER=lmstudio LLM_MODEL=<model> python -m exitpilot my-project --ai   # model on this machine
    python -m exitpilot examples/loja-mista --migrate out/loja-mista   # phase 2: plan + generated files
    python -m exitpilot my-project --migrate out --tofu                # also run tofu validate (optional)
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any

from .analysis import Result, analyse_folder


def summary(result: Result) -> dict[str, Any]:
    """Compact, stable JSON view of a result (numbers only, no AI text)."""
    exp = result.exposure
    return {
        "score": result.score.value,
        "score_label": result.score.label,
        "main_resources": result.score.counted,
        "jurisdiction_exposure": None if exp is None else {
            "dependencies": exp.total, "controlled_outside_eu": exp.outside_eu,
            "controlled_in_eu": exp.in_eu, "unknown": exp.unknown, "companies_outside_eu": exp.companies,
        },
        "services": [
            {"id": r.name, "service": r.service, "company": r.company, "headquarters": r.company_country,
             "zone": r.company_zone, "category": r.category, "level": r.level,
             "evidence": r.evidence}
            for r in result.services if r.level is not None
        ],
        "unknown_external_hosts": [r.name for r in result.services if r.level is None],
        "unclassified_infrastructure": sorted(r.address for r in result.unknown if r.origin != "service"),
        "files_with_errors": [e.file for e in result.analysis.errors],
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="exitpilot", description="Cloud lock-in and sovereignty report.")
    parser.add_argument("folder", help="folder with Terraform files and/or the application project")
    parser.add_argument("--json", action="store_true", help="print a JSON summary instead of the report")
    parser.add_argument("--ai", action="store_true",
                        help="write the report text with an LLM (Mistral API, or a local server: see LLM_PROVIDER)")
    parser.add_argument("--no-services", action="store_true", help="only read Terraform (skip step 2b)")
    parser.add_argument("--out", metavar="FILE", help="write the complete analysis (facts, AI analysis, report, "
                                                      "migration plan) to this JSON file")
    parser.add_argument("--no-ai-analysis", action="store_true",
                        help="with --ai: only write the report text, skip the AI analysis of each dependency")
    parser.add_argument("--migrate", metavar="OUT_DIR", help="phase 2: write the migration plan and files here")
    parser.add_argument("--overwrite", action="store_true", help="allow --migrate to write into a non-empty folder")
    parser.add_argument("--tofu", action="store_true", help="with --migrate: run tofu init + validate (needs OpenTofu)")
    args = parser.parse_args(argv)

    folder = Path(args.folder)
    if not folder.is_dir():
        print(f"Not a folder: {folder}", file=sys.stderr)
        return 2

    llm = None
    if args.ai:
        from dotenv import load_dotenv

        from .llm import make_llm

        load_dotenv()
        try:
            llm = make_llm()
        except ValueError as exc:
            print(exc, file=sys.stderr)
            return 2
        print(llm.status, file=sys.stderr)

    result = analyse_folder(folder, llm=llm, include_services=not args.no_services,
                            ai_analysis=not args.no_ai_analysis)

    if args.out:
        from .export import build_export
        from .migrate import plan_migration

        data = build_export(result, plan_migration(result))
        Path(args.out).parent.mkdir(parents=True, exist_ok=True)
        Path(args.out).write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
        ai = data["ai"]
        print(f"Analysis written to {args.out}: {len(data['resources'])} dependencies, "
              + (f"{ai['dependencies_analysed']} analysed by {ai['provider']} ({ai['model']})" if ai
                 else "no AI used"))
        if not args.migrate:
            return 0

    if args.migrate:
        from .migrate import plan_migration, write_migration

        migration = plan_migration(result)
        try:
            out = write_migration(migration, args.migrate, overwrite=args.overwrite, run_tofu=args.tofu)
        except FileExistsError as exc:
            print(exc, file=sys.stderr)
            return 2
        b, a = migration.before, migration.after
        eb, ea = migration.exposure_before, migration.exposure_after
        print(f"Migration plan written to {out}")
        print(f"  Sovereignty score:          {b.rounded}/100 -> {a.rounded}/100 (projected)")
        print(f"  Controlled outside the EU:  {eb.outside_eu} of {eb.total} -> {ea.outside_eu} of {ea.total} (projected)")
        for check in migration.checks:
            print(f"  [{check.status}] {check.name}: {check.detail}")
        for path in sorted(migration.files):
            print(f"  - {path}")
        return 0

    if args.json:
        print(json.dumps(summary(result), indent=2, ensure_ascii=False))
    else:
        print(result.report.markdown)
    return 0


if __name__ == "__main__":
    sys.exit(main())
