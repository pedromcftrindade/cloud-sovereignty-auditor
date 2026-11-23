"""Record the AI's answers for the example projects, so people without a model can see them.

    python -m exitpilot.record                     # every example, with the model of your .env
    python -m exitpilot.record loja-mista loja-lixo

Needs a reachable model (Ollama running, or a Mistral API key). For each example it runs exactly what
the apps run: the analysis and report (with the AI analysis of every dependency) and the market
study's AI recommendation with the default settings. The answers are written to demo/ai_answers.json,
which is committed. When someone runs ExitPilot with no model reachable, these answers are replayed
for those same prompts (see RecordedAnswers in llm.py), and the report names the model and the date.

Only the example projects are recorded: your own projects never end up in this file.
Record again after changing a prompt, the facts sent to the model or an example; the test
test_recorded_answers_match_the_current_prompts says when the recording is out of date.
"""

from __future__ import annotations

import argparse
import json
import sys
import tempfile
from datetime import date
from pathlib import Path
from typing import Any, Optional

from .analysis import analyse_folder
from .llm import RECORDED_ANSWERS, make_llm
from .market import ai_recommendation, study

EXAMPLES = Path(__file__).resolve().parent.parent / "examples"


def example_names() -> list[str]:
    return sorted(p.name for p in EXAMPLES.iterdir() if p.is_dir()) if EXAMPLES.exists() else []


def run_example(name: str, llm) -> dict[str, Any]:
    """What the apps ask the AI for this example, with their default settings."""
    result = analyse_folder(EXAMPLES / name, llm=llm, project_name=name)
    ms = study(result.analysis, city="Lisbon", skip_waste=bool(result.analysis.waste), include_basic=False)
    market_ai = ai_recommendation(ms, llm)
    return {"example": name, "report_ai": result.report.ai_used,
            "dependencies_analysed": (result.ai or {}).get("dependencies_analysed", 0), "market_ai": market_ai}


def record(llm, names: list[str]) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Run every example with `llm` (whose cache must start empty) and return (recording, summary)."""
    summary = [run_example(name, llm) for name in names]
    answers = json.loads(Path(llm.cache_path).read_text(encoding="utf-8")) if Path(llm.cache_path).exists() else {}
    recording = {
        "recorded_with": {"label": getattr(llm, "label", "AI"), "model": llm.model,
                          "recorded_on": date.today().isoformat(), "examples": names},
        "answers": dict(sorted(answers.items())),
    }
    return recording, summary


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="exitpilot.record", description=__doc__.split("\n\n")[0])
    parser.add_argument("examples", nargs="*", help="example names (default: all)")
    parser.add_argument("--output", default=str(RECORDED_ANSWERS), help="default demo/ai_answers.json")
    args = parser.parse_args(argv)

    try:
        from dotenv import load_dotenv
        load_dotenv(EXAMPLES.parent / ".env")
    except ImportError:
        pass

    names = args.examples or example_names()
    unknown = sorted(set(names) - set(example_names()))
    if unknown:
        print(f"Unknown example(s): {', '.join(unknown)}. Available: {', '.join(example_names())}", file=sys.stderr)
        return 2

    with tempfile.TemporaryDirectory() as tmp:
        # A fresh cache: only the answers given now go into the recording, never old or private ones.
        llm = make_llm(cache_path=Path(tmp) / "answers.json", offline=False, recorded="0")
        if not llm.online:
            print(f"No model reachable: {llm.status}", file=sys.stderr)
            return 1
        print(f"Recording with {llm.label} ({llm.model}). A local model can take a few minutes per example.")
        recording, summary = record(llm, names)

    for s in summary:
        print(f"  {s['example']:<20} report: {'AI' if s['report_ai'] else 'template (AI answer rejected)'}, "
              f"{s['dependencies_analysed']} dependencies analysed, "
              f"market: {'AI' if s['market_ai'] else 'computed (AI answer rejected)'}")
    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(json.dumps(recording, indent=1, ensure_ascii=False) + "\n", encoding="utf-8")
    print(f"{len(recording['answers'])} answers written to {out}. Commit this file.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
