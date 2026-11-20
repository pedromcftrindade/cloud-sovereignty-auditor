"""Glue: folder -> read -> classify -> score -> report. Also builds the dependency graph.

Reading has two sources that end up in the same Analysis (model.py):
  2a. parse_folder()  Terraform files: the infrastructure that is declared
  2b. add_services()  config, code, dependencies, images, network snapshot:
                      the third-party services the application really uses
"""

from __future__ import annotations

import io
import zipfile
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from .ai_analysis import enrich
from .classifier import classify
from .model import Analysis, Resource
from .parser import parse_folder
from .report import Report, generate_report
from .scoring import Exposure, Score, compute_exposure, compute_score, score_by_provider
from .services import add_services

LEVEL_COLOURS = {0: "#2e7d32", 1: "#7cb342", 2: "#f9a825", 3: "#c62828"}
SUPPORT_COLOUR = "#9e9e9e"
UNKNOWN_COLOUR = "#5c6bc0"


@dataclass
class Result:
    analysis: Analysis
    unknown: list[Resource]
    score: Score
    by_provider: dict[str, Score]
    report: Report
    exposure: Optional[Exposure] = None
    ai: Optional[dict] = None          # {"provider", "model", "dependencies_analysed"} when a model was used

    @property
    def services(self) -> list[Resource]:
        return [r for r in self.analysis.resources if r.origin == "service"]


def analyse_folder(folder: str | Path, llm=None, project_name: Optional[str] = None,
                   include_services: bool = True, ai_analysis: bool = True) -> Result:
    analysis = parse_folder(folder)                 # 2a: declared infrastructure (Terraform)
    if include_services:
        add_services(analysis, folder)              # 2b: third-party services really used
    unknown = classify(analysis, llm=llm)           # catalogs first, then the AI for what they do not know
    analysed = enrich(analysis, llm) if ai_analysis else 0   # the AI analyses each dependency (JSON)
    report = generate_report(analysis, unknown, llm=llm, project_name=project_name or Path(folder).name)
    ai = None
    if llm is not None and (analysed or report.ai_used):
        ai = {"provider": getattr(llm, "label", "AI"), "model": getattr(llm, "model", ""),
              "dependencies_analysed": analysed}
    return Result(analysis=analysis, unknown=unknown, score=compute_score(analysis.resources),
                  by_provider=score_by_provider(analysis.resources), report=report,
                  exposure=compute_exposure(analysis.resources), ai=ai)


def to_dot(analysis: Analysis, include_support: bool = False) -> str:
    """Graphviz DOT text: one box per resource, coloured by lock-in level, arrows = dependencies."""
    shown = [r for r in analysis.resources if include_support or not r.support]
    shown_addresses = {r.address for r in shown}
    lines = ['digraph G {', '  rankdir=LR;', '  bgcolor="transparent";',
             '  node [shape=box, style="rounded,filled", fontname="Helvetica", fontsize=11, fontcolor="white"];']
    for r in shown:
        colour = SUPPORT_COLOUR if r.support else LEVEL_COLOURS.get(r.level, UNKNOWN_COLOUR)
        font = "black" if (not r.support and r.level in (1, 2)) else "white"   # readable on light fills
        level = "?" if r.level is None else r.level
        if r.origin == "service":
            label = f"{r.service or r.name}\\n{r.company or 'unknown owner'} (level {level})"
        else:
            label = f"{r.type}\\n{r.name} (level {level})"
        lines.append(f'  "{r.address}" [label="{label}", fillcolor="{colour}", fontcolor="{font}"];')
    direct = set()
    for r in shown:
        for dep in r.depends_on:
            if dep in shown_addresses:
                direct.add(frozenset((r.address, dep)))
                lines.append(f'  "{r.address}" -> "{dep}";')
    if not include_support:
        # Main resources are often linked only through support resources
        # (instance <- target group attachment -> target group <- listener -> load balancer).
        # Show those indirect links as dashed lines so the map stays readable.
        for a, b in sorted(_links_through_support(analysis)):
            if frozenset((a, b)) not in direct:
                lines.append(f'  "{a}" -> "{b}" [style=dashed, dir=none, color="#888888"];')
    lines.append("}")
    return "\n".join(lines)


def _links_through_support(analysis: Analysis) -> set[tuple[str, str]]:
    by_address = {r.address: r for r in analysis.resources}
    neighbours: dict[str, set[str]] = {a: set() for a in by_address}
    for r in analysis.resources:
        for dep in r.depends_on:
            if dep in by_address:
                neighbours[r.address].add(dep)
                neighbours[dep].add(r.address)
    links = set()
    for start, res in by_address.items():
        if res.support:
            continue
        seen, frontier = {start}, [start]
        while frontier:                      # walk only through support resources
            node = frontier.pop()
            for nxt in neighbours[node]:
                if nxt in seen:
                    continue
                seen.add(nxt)
                if by_address[nxt].support:
                    # shared network pieces (VPC, subnets) would connect everything to everything
                    if by_address[nxt].category != "network":
                        frontier.append(nxt)
                elif nxt != start:
                    links.add(tuple(sorted((start, nxt))))
    return links


def safe_extract_zip(data: bytes, dest: str | Path, max_files: int = 2000, max_bytes: int = 50_000_000) -> int:
    """Extract an uploaded .zip without letting paths escape `dest` (zip-slip) or filling the disk."""
    dest = Path(dest).resolve()
    count, total = 0, 0
    with zipfile.ZipFile(io.BytesIO(data)) as zf:
        for info in zf.infolist():
            if info.is_dir():
                continue
            target = (dest / info.filename).resolve()
            if dest not in target.parents:
                continue                      # "../../etc/passwd" and absolute paths are skipped
            count += 1
            total += info.file_size
            if count > max_files or total > max_bytes:
                raise ValueError("Archive too large for this demo.")
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_bytes(zf.read(info))
    return count
