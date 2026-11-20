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
from exitpilot.llm import MistralClient
from exitpilot.model import PROVIDER_NAMES
from exitpilot.report import ranked_main_resources

load_dotenv()  # reads MISTRAL_API_KEY from a local .env file (never committed)

ROOT = Path(__file__).resolve().parent
EXAMPLES = ROOT / "examples"

st.set_page_config(page_title="ExitPilot - cloud lock-in report", page_icon="🧭", layout="wide")


@st.cache_resource
def get_llm() -> MistralClient:
    # Created once per server: keeps the disk cache in memory and avoids re-creating the client.
    return MistralClient()


def run_analysis(source_dir: Path, project_name: str, use_ai: bool):
    llm = get_llm() if use_ai else None
    with st.spinner("Reading the infrastructure and writing the report..."):
        return analyse_folder(source_dir, llm=llm, project_name=project_name)


# ------------------------------------------------------------------ sidebar
st.sidebar.title("🧭 ExitPilot")
st.sidebar.caption("How locked-in is your cloud? A sovereignty report for your infrastructure code.")

mode = st.sidebar.radio("Infrastructure to analyse", ["Example", "Upload"], horizontal=True)
examples = sorted(p.name for p in EXAMPLES.iterdir() if p.is_dir()) if EXAMPLES.exists() else []
example = st.sidebar.selectbox("Example", examples, disabled=(mode != "Example"))
uploads = st.sidebar.file_uploader(
    "Terraform files (.tf) or a .zip of the project", type=["tf", "zip"],
    accept_multiple_files=True, disabled=(mode != "Upload"),
)

use_ai = st.sidebar.toggle("Use AI (Mistral)", value=True)
llm_status = get_llm().status
st.sidebar.caption(llm_status if use_ai else "AI disabled: template text only.")

if st.sidebar.button("Analyse", type="primary", width="stretch"):
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
        "ExitPilot reads your infrastructure code (Terraform for AWS, Azure or Google Cloud), "
        "measures how dependent you are on each provider and shows the European alternatives. "
        "Choose an example on the left and press **Analyse**."
    )
    st.stop()

analysis, score, report = result.analysis, result.score, result.report
st.title(f"Lock-in report: {report.project_name}")

c1, c2, c3, c4 = st.columns(4)
c1.metric("Sovereignty score", "n/a" if score.value is None else f"{score.rounded}/100",
          help="S = 100 x (1 - N / (3 x R)). 100 = no lock-in.", border=True)
c2.metric("Lock-in", score.label.split()[0] if score.value is not None else "n/a", border=True)
c3.metric("Main resources", score.counted,
          help="Resources that enter the score (support configuration is left out).", border=True)
c4.metric("Not fully analysed", len(result.unknown) + len(analysis.errors)
          + sum(1 for r in analysis.resources if r.origin == "module"), border=True)

if len(result.by_provider) > 1:
    cols = st.columns(len(result.by_provider))
    for col, (prov, s) in zip(cols, result.by_provider.items()):
        col.metric(PROVIDER_NAMES.get(prov, prov), "n/a" if s.value is None else f"{s.rounded}/100")

st.caption("🤖 Report text written by AI (Mistral), numbers computed by ExitPilot."
           if report.ai_used else "Report text from template (AI not used). All numbers computed by ExitPilot.")

tab_report, tab_map, tab_inventory, tab_json = st.tabs(["Report", "Dependency map", "Inventory", "Common JSON"])

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
    st.subheader("Support configuration and unclassified")
    other = [
        {"resource": r.address, "category": r.category, "level": r.level, "note": r.note, "file": r.file}
        for r in analysis.resources if r.support or r.level is None
    ]
    st.dataframe(other, width="stretch", hide_index=True)

with tab_json:
    ir = json.dumps(analysis.to_dict(), indent=2, ensure_ascii=False)
    st.caption("The common representation every input format is converted into (input for phase 2).")
    st.code(ir, language="json")
    st.download_button("Download JSON", ir, file_name="infrastructure.json", mime="application/json")

st.divider()
st.button("Generate migration plan to Leaseweb (phase 2)", disabled=True,
          help="Phase 2: translate this infrastructure to Leaseweb, validated with OpenTofu.")
