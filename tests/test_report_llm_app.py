import io
import json
import zipfile

from exitpilot.analysis import analyse_folder, safe_extract_zip, to_dot
from exitpilot.llm import MistralClient, _extract_json
from exitpilot.regions import describe_region


# ------------------------------------------------------------------ report
def test_report_without_ai_has_every_section(examples):
    r = analyse_folder(examples / "startup-serverless")
    md = r.report.markdown
    assert not r.report.ai_used
    for title in ["## 1. Executive summary", "## 2. Where the data is", "## 3. Biggest lock-in points",
                  "## 4. Full inventory", "## 5. What we could not fully analyse", "## 6. Next steps"]:
        assert title in md
    assert "13/100" in md
    assert "United States" in md and "Outside Europe" in md


def test_report_uses_ai_text_but_drops_invented_resources(examples, fake_llm):
    def answer(prompt):
        if "Types to classify" in prompt:
            return {"results": []}
        return {"executive_summary": "Your shop depends mostly on standard services.",
                "top_risks": [{"address": "aws_db_instance.db", "why": "Managed DB.", "recommendation": "Use PostgreSQL."},
                              {"address": "aws_quantum_computer.q", "why": "Invented!", "recommendation": "-"}],
                "next_steps": ["Talk to the team."]}

    r = analyse_folder(examples / "loja-simples", llm=fake_llm(answer))
    assert r.report.ai_used
    assert "Your shop depends mostly on standard services." in r.report.markdown
    assert [t["address"] for t in r.report.top_risks] == ["aws_db_instance.db"]
    assert "aws_quantum_computer" not in r.report.markdown


def test_ai_without_summary_falls_back_to_template(examples, fake_llm):
    r = analyse_folder(examples / "loja-simples", llm=fake_llm({"top_risks": []}))
    assert not r.report.ai_used
    assert "We analysed 4 main infrastructure components" in r.report.markdown


def test_regions():
    assert describe_region("eu-west-1")["zone"] == "EU"
    assert describe_region("eu-west-2")["zone"] == "Europe (non-EU)"
    assert describe_region("europe-west1-b")["country"] == "Belgium"       # GCP zone -> region
    assert describe_region(None)["zone"] == "Unknown"


# ------------------------------------------------------------------ llm
def test_llm_without_key_never_calls_the_api(tmp_path, monkeypatch):
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    llm = MistralClient(api_key="", cache_path=tmp_path / "c.json")
    assert not llm.online and not llm.available
    assert llm.ask_json("hello") is None


def test_llm_cache_works_offline(tmp_path):
    cache = tmp_path / "c.json"
    first = MistralClient(api_key="", cache_path=cache)
    key = first._key("Answer only with valid JSON.", "question")
    cache.write_text(json.dumps({key: {"ok": True}}))
    offline = MistralClient(api_key="", cache_path=cache, offline=True)
    assert offline.available
    assert offline.ask_json("question") == {"ok": True}


def test_extract_json_handles_code_fences():
    assert _extract_json('```json\n{"a": 1}\n```') == {"a": 1}
    assert _extract_json('Here you go: {"a": 2} hope it helps') == {"a": 2}
    assert _extract_json("nope") is None


# ------------------------------------------------------------------ helpers
def test_zip_slip_is_blocked(tmp_path):
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("ok/main.tf", 'resource "aws_instance" "a" {}')
        zf.writestr("../../evil.tf", "bad")
    dest = tmp_path / "dest"
    dest.mkdir()
    assert safe_extract_zip(buf.getvalue(), dest) == 1
    assert (dest / "ok" / "main.tf").exists()
    assert not (tmp_path.parent / "evil.tf").exists()


def test_dot_graph_has_nodes_and_edges(examples):
    r = analyse_folder(examples / "loja-simples")
    full = to_dot(r.analysis, include_support=True)
    assert full.startswith("digraph")
    assert '"aws_instance.web" -> "aws_security_group.web";' in full          # direct dependency
    compact = to_dot(r.analysis)
    assert '"aws_security_group.web"' not in compact                           # support hidden
    assert '"aws_instance.web" -> "aws_lb.web" [style=dashed' in compact       # linked via support


# ------------------------------------------------------------------ app (headless)
def test_streamlit_app_runs_end_to_end(monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("EXITPILOT_OFFLINE", "1")
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    at = AppTest.from_file("../app.py", default_timeout=60)
    at.run()
    assert not at.exception
    at.selectbox[0].select("loja-simples").run()
    at.button[0].click().run()
    assert not at.exception
    values = [m.value for m in at.metric]
    assert "67/100" in values
    assert any("Lock-in report: loja-simples" in t.value for t in at.title)


def test_streamlit_app_shows_services_of_mixed_example(monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("EXITPILOT_OFFLINE", "1")
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    at = AppTest.from_file("../app.py", default_timeout=60)
    at.run()
    at.selectbox[0].select("loja-mista").run()
    at.button[0].click().run()
    assert not at.exception
    values = [m.value for m in at.metric]
    assert "62/100" in values and "12/14" in values
    assert any("Third-party services" in s.value for s in at.subheader)

    at.button(key="migrate").click().run()                       # phase 2
    assert not at.exception
    values = [m.value for m in at.metric]
    assert "82/100" in values and "0/14" in values
    assert any("Wave 3" in s.value for s in at.subheader)


def test_llm_parses_sdk_response_and_caches_it(tmp_path):
    """Simulates the mistralai SDK response object (resp.choices[0].message.content)."""
    from types import SimpleNamespace

    calls = []

    class FakeChat:
        def complete(self, **kwargs):
            calls.append(kwargs)
            msg = SimpleNamespace(content='```json\n{"results": []}\n```')
            return SimpleNamespace(choices=[SimpleNamespace(message=msg)])

    llm = MistralClient(api_key="", cache_path=tmp_path / "c.json")
    llm._client = SimpleNamespace(chat=FakeChat())
    assert llm.ask_json("classify these") == {"results": []}
    assert llm.ask_json("classify these") == {"results": []}       # second time: from cache
    assert len(calls) == 1
    assert calls[0]["response_format"] == {"type": "json_object"}
    assert (tmp_path / "c.json").exists()
