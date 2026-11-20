"""ExitPilot - web interface (Streamlit).

Run with:  streamlit run app.py
"""

from __future__ import annotations

import json
import tempfile
from pathlib import Path

import streamlit as st
from dotenv import load_dotenv

from exitpilot.analysis import (LEVEL_COLOURS, SUPPORT_COLOUR, UNKNOWN_COLOUR, analyse_folder,
                                safe_extract_zip, to_dot)
from exitpilot.export import build_export
from exitpilot.llm import make_llm
from exitpilot.market import CITIES, ai_recommendation, labels, map_points, market_markdown
from exitpilot.market import study as market_study
from exitpilot.migrate import migration_zip, plan_migration
from exitpilot.model import PROVIDER_NAMES
from exitpilot.report import ranked_main_resources, service_rows

load_dotenv()  # reads MISTRAL_API_KEY from a local .env file (never committed)

ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples"

st.set_page_config(page_title="ExitPilot - cloud lock-in report", page_icon="🧭", layout="wide")


@st.cache_resource
def get_llm():
    # Created once per server: keeps the disk cache in memory and avoids re-creating the client.
    # LLM_PROVIDER chooses Mistral's API (default) or a model server on this machine (see llm.py).
    return make_llm()


def run_analysis(source_dir: Path, project_name: str, use_ai: bool):
    llm = get_llm() if use_ai else None
    with st.spinner("Reading the infrastructure and writing the report..."):
        return analyse_folder(source_dir, llm=llm, project_name=project_name)


# ------------------------------------------------------------------ sidebar
st.sidebar.title("🧭 ExitPilot")
st.sidebar.caption("How locked-in is your cloud? A sovereignty report for your infrastructure code "
                   "and the third-party services your application really uses.")

mode = st.sidebar.radio("Infrastructure to analyse", ["Example", "Upload"], horizontal=True)
examples = sorted(p.name for p in EXAMPLES.iterdir() if p.is_dir()) if EXAMPLES.exists() else []
example = st.sidebar.selectbox("Example", examples, disabled=(mode != "Example"))
uploads = st.sidebar.file_uploader(
    "A .zip of the project (best), or single files: Terraform, docker-compose, .env, requirements, code",
    type=["zip", "tf", "yml", "yaml", "env", "json", "txt", "toml", "py", "js", "ts", "html"],
    accept_multiple_files=True, disabled=(mode != "Upload"),
)

use_ai = st.sidebar.toggle(f"Use AI ({get_llm().label})", value=True)
llm_status = get_llm().status
st.sidebar.caption(llm_status if use_ai else "AI disabled: template text only.")

if st.sidebar.button("Analyse", type="primary", width="stretch"):
    st.session_state.pop("migration", None)          # a new analysis invalidates the old plan
    if mode == "Example" and example:
        st.session_state["result"] = run_analysis(EXAMPLES / example, example, use_ai)
    elif mode == "Upload" and uploads:
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            try:
                for up in uploads:
                    if up.name.lower().endswith(".zip"):
                        safe_extract_zip(up.getvalue(), tmp_path)
                    else:
                        (tmp_path / Path(up.name).name).write_bytes(up.getvalue())
                st.session_state["result"] = run_analysis(tmp_path, "uploaded project", use_ai)
            except ValueError as exc:
                st.sidebar.error(str(exc))
    else:
        st.sidebar.warning("Choose an example or upload files first.")

# ------------------------------------------------------------------ main area
result = st.session_state.get("result")
if result is None:
    st.title("Cloud lock-in report")
    st.write(
        "ExitPilot reads your infrastructure code (Terraform for AWS, Azure or Google Cloud) **and** the "
        "third-party services your application really uses (configuration, code, dependencies, container "
        "images, network traffic). It measures how locked-in you are, who controls each dependency and "
        "shows the European alternatives. Choose an example on the left and press **Analyse**."
    )
    st.stop()

analysis, score, report = result.analysis, result.score, result.report
st.title(f"Lock-in report: {report.project_name}")

exposure = result.exposure
c1, c2, c3, c4, c5 = st.columns(5)
c1.metric("Sovereignty score", "n/a" if score.value is None else f"{score.rounded}/100",
          help="S = 100 x (1 - N / (3 x R)). 100 = no lock-in.", border=True)
c2.metric("Lock-in", score.label.split()[0] if score.value is not None else "n/a", border=True)
c3.metric("Controlled outside the EU",
          "n/a" if not exposure or exposure.total == 0 else f"{exposure.outside_eu}/{exposure.total}",
          help="Dependencies controlled by a company headquartered outside the EU, wherever its servers are.",
          border=True)
c4.metric("Main resources", score.counted,
          help="Resources and services that enter the score (support configuration is left out).", border=True)
c5.metric("Not fully analysed", len(result.unknown) + len(analysis.errors)
          + sum(1 for r in analysis.resources if r.origin == "module"), border=True)

if len(result.by_provider) > 1:
    cols = st.columns(len(result.by_provider))
    for col, (prov, s) in zip(cols, result.by_provider.items()):
        col.metric(PROVIDER_NAMES.get(prov, prov), "n/a" if s.value is None else f"{s.rounded}/100")

if analysis.waste:
    left_out = sum(1 for w in analysis.waste if w.exclude_from_migration)
    st.info(f"🧹 **{len(analysis.waste)} suspected waste item(s)** look unused ({left_out} can be left out of the "
            "migration). See *Suspected waste* in the report or the Inventory tab. Nothing is deleted automatically.")

st.caption(f"🤖 Report text written by AI ({get_llm().label}), numbers computed by ExitPilot."
           if report.ai_used else "Report text from template (AI not used). All numbers computed by ExitPilot.")

tab_report, tab_market, tab_map, tab_inventory, tab_json = st.tabs(
    ["Report", "Market study", "Dependency map", "Inventory", "Common JSON"])

with tab_report:
    body = report.markdown.split("\n", 1)[1] if report.markdown.startswith("# ") else report.markdown
    st.markdown(body)   # the H1 title is already shown at the top of the page
    st.download_button("Download report (.md)", report.markdown, file_name="lock-in-report.md",
                       mime="text/markdown")

with tab_map:
    show_support = st.checkbox("Show support configuration (network, permissions...)", value=False)
    st.graphviz_chart(to_dot(analysis, include_support=show_support))
    legend = [(LEVEL_COLOURS[0], "level 0 standard"), (LEVEL_COLOURS[1], "level 1 compatible"),
              (LEVEL_COLOURS[2], "level 2 managed, open-source equivalent"),
              (LEVEL_COLOURS[3], "level 3 proprietary"), (SUPPORT_COLOUR, "support"),
              (UNKNOWN_COLOUR, "unknown")]
    st.markdown(" &nbsp; ".join(
        f'<span style="color:{c}; font-size:1.2em">■</span> {text}' for c, text in legend),
        unsafe_allow_html=True)
    st.caption("Arrows: direct dependency. Dashed lines: linked through support configuration.")

with tab_inventory:
    rows = [
        {"resource": r.address, "provider": r.provider, "category": r.category, "level": r.level,
         "confidence": r.confidence, "classified by": r.classified_by, "region": r.region, "file": r.file}
        for r in ranked_main_resources(analysis.resources)
    ]
    st.subheader("Main resources")
    st.dataframe(rows, width="stretch", hide_index=True)
    services = service_rows(analysis)
    if services:
        st.subheader("Third-party services the application uses")
        st.caption("Zone = where the company is headquartered. Evidence = where we found it "
                   "(config, code, dependency, image or observed traffic). Secret values are never stored.")
        st.dataframe([{"service": s["service"], "company": s["company"], "headquarters": s["country"],
                       "zone": s["zone"], "category": s["category"], "level": s["level"],
                       "verified": s["verified"], "evidence": s["evidence"]} for s in services],
                     width="stretch", hide_index=True)
    if analysis.waste:
        st.subheader("Suspected waste (review before deleting)")
        st.dataframe([{"item": w.address, "what": w.title, "why": w.reason, "confidence": w.confidence,
                       "migration": "left out" if w.exclude_from_migration else "clean up only",
                       "where": "; ".join(e["file"] + (f":{e['line']}" if e.get("line") else "")
                                          for e in w.evidence[:3])}
                      for w in analysis.waste], width="stretch", hide_index=True)
    st.subheader("Support configuration and unclassified")
    other = [
        {"resource": r.address, "category": r.category, "level": r.level, "note": r.note, "file": r.file}
        for r in analysis.resources if r.support or r.level is None
    ]
    st.dataframe(other, width="stretch", hide_index=True)

with tab_market:
    st.subheader("Where to go: European providers for this infrastructure")
    st.caption("Compares European providers on price (cheapest plan that fits each machine), distance from your "
               "company to their nearest EU data centre, and the managed services your infrastructure uses. "
               "Indicative public list prices, EUR excl. VAT, compute only.")
    q1, q2 = st.columns([1, 2])
    with q1:
        city = st.selectbox("Your company is in", list(CITIES), index=list(CITIES).index("Lisbon"), key="mk_city")
        include_basic = st.checkbox("Include test/dev plans (no fixed SLA)", value=False, key="mk_basic")
        skip_waste_mk = st.checkbox("Leave suspected waste out", value=True, key="mk_waste",
                                    disabled=not analysis.waste)
    with q2:
        w1, w2, w3, w4 = st.columns(4)
        w_price = w1.slider("Price", 0, 100, 40, key="mk_wp")
        w_prox = w2.slider("Proximity", 0, 100, 30, key="mk_wd")
        w_cov = w3.slider("Managed services", 0, 100, 30, key="mk_wc")
        w_auto = w4.slider("Automated migration", 0, 100, 0, key="mk_wa",
                           help="Weight for providers ExitPilot can already generate Terraform for (phase 2).")
    weights = {"price": w_price, "proximity": w_prox, "coverage": w_cov, "automation": w_auto}
    ms = market_study(analysis, city=city, weights=weights, skip_waste=skip_waste_mk and bool(analysis.waste),
                      include_basic=include_basic)

    ai_key = json.dumps([report.project_name, city, weights, include_basic, skip_waste_mk], sort_keys=True)
    saved_ai = st.session_state.get("mk_ai", {}).get(ai_key)
    if saved_ai:
        ms.recommendation, ms.recommendation_source = saved_ai, "ai"
    if use_ai and st.button("Ask the AI to explain the choice", key="mk_ask"):
        with st.spinner("The AI is writing the recommendation..."):
            if ai_recommendation(ms, get_llm()):
                st.session_state.setdefault("mk_ai", {})[ai_key] = ms.recommendation
            else:
                st.warning("The AI answer did not pass validation (wrong provider or numbers that are not in "
                           "the facts). Showing the recommendation computed by ExitPilot.")
    (st.success if ms.best else st.warning)(
        ("🤖 " if ms.recommendation_source == "ai" else "") + ms.recommendation)

    best, cheap, close = ms.best, ms.best_by("price"), ms.best_by("proximity")
    k1, k2, k3 = st.columns(3)
    k1.metric("Best overall" + (f" (score {best.score:g})" if best else ""), best.name if best else "n/a",
              border=True)
    k2.metric("Cheapest" + (f" (EUR {cheap.monthly_eur:.2f}/month)" if cheap else ""),
              cheap.name if cheap else "n/a", border=True)
    k3.metric("Closest" + (f" ({close.distance_km} km, {close.nearest_dc})" if close else ""),
              close.name if close else "n/a", border=True)

    st.dataframe([{"provider": f"{o.name} ({o.hq})", "score": o.score,
                   "EUR/month": o.monthly_eur, "nearest EU data centre": o.nearest_dc, "km": o.distance_km,
                   "min. round trip (ms)": o.rtt_ms, "managed services missing": labels(o.missing) or "-",
                   "orderable today": "yes" if o.orderable else "no", "Terraform provider": o.terraform,
                   "ExitPilot generates Terraform": "yes" if o.phase2_automated else "no"}
                  for o in ms.offers], width="stretch", hide_index=True)
    st.caption("score = 100 x (wp x P + wd x D + wc x C + wa x A) / (wp + wd + wc + wa), where P = cheapest cost / "
               "this cost, D = (nearest km + 50) / (this km + 50), C = managed services covered / needed, "
               "A = 1 if ExitPilot already generates its Terraform, else 0. Providers whose plans cannot be "
               "ordered today are listed but never recommended. Round trip: theoretical minimum over fibre "
               "(about 1 ms per 100 km).")
    with st.expander(f"Machines to host ({len(ms.machines)}) and the plan chosen at each provider"):
        st.dataframe([{"machine": m.source, "vCPU": m.vcpu, "RAM (GB)": m.ram_gb,
                       "size known": "yes" if m.size_known else "no (assumed 2 vCPU / 4 GB)"} for m in ms.machines],
                     width="stretch", hide_index=True)
        st.dataframe([{"provider": o.name, "machine": ln["source"], "plan": ln["plan"],
                       "vCPU": ln["vcpu"], "RAM (GB)": ln["ram_gb"], "EUR/month": ln["eur_month"]}
                      for o in ms.offers for ln in o.lines], width="stretch", hide_index=True)
    st.markdown("**Today vs. the European options** (red: where your infrastructure runs today, blue: your "
                "company, green: recommended provider, grey: other providers)")
    try:
        import pandas as pd

        st.map(pd.DataFrame(map_points(analysis, ms)), latitude="lat", longitude="lon",
               color="color", size="size")
    except Exception as exc:  # noqa: BLE001 - a map problem must not break the page
        st.caption(f"Map not available ({type(exc).__name__}).")
    with st.expander("Notes and sources (checked " + ms.checked + ")"):
        for o in ms.offers:
            st.markdown(f"- **{o.name}**: {o.note} [Price list]({o.source})")
    st.download_button("Download the market study (.md)", market_markdown(ms),
                       file_name=f"{report.project_name}-market-study.md", mime="text/markdown")

with tab_json:
    ir = json.dumps(analysis.to_dict(), indent=2, ensure_ascii=False)
    st.caption("The common representation every input format is converted into (input for phase 2).")
    st.code(ir, language="json")
    st.download_button("Download JSON", ir, file_name="infrastructure.json", mime="application/json")
    st.caption("analysis.json is the complete file: facts, the AI's analysis of each dependency, the report "
               "and (if you generated it below) the migration plan.")
    saved = st.session_state.get("migration")
    full = build_export(result, saved if saved is not None and saved.project == report.project_name else None)
    st.download_button("Download analysis.json (complete)", json.dumps(full, indent=2, ensure_ascii=False),
                       file_name="analysis.json", mime="application/json")

# ------------------------------------------------------------------ phase 2: migration
st.divider()
st.header("Phase 2: migration plan to Leaseweb")
st.caption("Chooses a European target for every dependency, orders the work in waves, generates the "
           "Terraform, cloud-init, docker-compose and environment files, and projects the result. "
           "Nothing is applied: it is a proposal to review.")
exclude_waste = st.checkbox("Leave suspected waste out of the migration", value=True, key="exclude_waste",
                            disabled=not analysis.waste,
                            help="Unused disks, IPs, SDKs... are listed for clean-up instead of being moved.")
if st.button("Generate migration plan", key="migrate", type="primary"):
    st.session_state["migration"] = plan_migration(result, exclude_waste=exclude_waste and bool(analysis.waste))

migration = st.session_state.get("migration")
if migration is not None and migration.project == report.project_name:
    b, a = migration.before, migration.after
    eb, ea = migration.exposure_before, migration.exposure_after
    m1, m2, m3 = st.columns(3)
    m1.metric("Sovereignty score after the plan", "n/a" if a.rounded is None else f"{a.rounded}/100",
              delta=None if a.rounded is None or b.rounded is None else a.rounded - b.rounded, border=True)
    m2.metric("Controlled outside the EU after the plan", f"{ea.outside_eu}/{ea.total}",
              delta=ea.outside_eu - eb.outside_eu, delta_color="inverse", border=True)
    m3.metric("Steps", len([s for s in migration.steps if s.kind not in ("keep", "drop")]),
              help="Steps that need work (already European dependencies are kept).", border=True)
    st.caption("Projected values: they assume every step is done. Self-hosting lowers lock-in but adds "
               "operations work (updates, backups, monitoring).")

    def _lvl(value):
        return "?" if value is None else value

    def _rows(steps):
        return [{"today": s.today, "target": s.target, "level": f"{_lvl(s.level_before)} -> {_lvl(s.level_after)}",
                 "generated": ", ".join(s.generated) or "-", "manual work": " ".join(s.actions) or "-"}
                for s in steps]

    decisions = [s for s in migration.steps if s.kind == "decision"]
    if decisions:
        st.subheader("Decisions needed first")
        st.dataframe(_rows(decisions), width="stretch", hide_index=True)
    for wave, title in [(1, "Wave 1: quick wins"), (2, "Wave 2: managed services"), (3, "Wave 3: deep changes")]:
        steps = [s for s in migration.steps_in_wave(wave) if s.kind != "decision"]
        if steps:
            st.subheader(title)
            st.dataframe(_rows(steps), width="stretch", hide_index=True)
    dropped = [s for s in migration.steps if s.kind == "drop"]
    if dropped:
        st.subheader("Not migrated: suspected waste")
        st.dataframe([{"item": s.today, "why": " ".join(s.actions)} for s in dropped],
                     width="stretch", hide_index=True)
    kept = [s.today for s in migration.steps if s.kind == "keep"]
    if kept:
        st.caption("Already European (kept): " + "; ".join(kept))

    st.subheader("Checks")
    for check in migration.checks:
        icon = {"passed": "✅", "failed": "❌"}.get(check.status, "⏸️")
        st.markdown(f"{icon} **{check.name}** ({check.status}): {check.detail}")

    st.subheader("Generated files")
    for path, text in sorted(migration.files.items()):
        with st.expander(path):
            language = {"tf": "hcl", "yaml": "yaml", "yml": "yaml", "md": "markdown"}.get(path.rsplit(".", 1)[-1], "bash")
            st.code(text, language=language)
    st.download_button("Download the migration (.zip)", migration_zip(migration),
                       file_name=f"{migration.project}-migration.zip", mime="application/zip")
