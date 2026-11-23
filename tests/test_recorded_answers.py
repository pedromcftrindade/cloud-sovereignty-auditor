"""Recorded AI answers (demo/ai_answers.json): replayed for the examples when no model is reachable."""

import json

import pytest

from exitpilot import record as rec
from exitpilot.analysis import analyse_folder
from exitpilot.llm import RECORDED_ANSWERS, MistralClient, RecordedAnswers, cache_key, load_recording, make_llm
from exitpilot.market import ai_recommendation, study

SUMMARY = "The infrastructure depends on Amazon Web Services for most of its resources."


def model_answer(prompt):
    """What a well-behaved model would answer to each of ExitPilot's prompts."""
    if "Types to classify" in prompt:
        return {"results": []}
    if "DEPENDENCIES:" in prompt:
        import re
        batch = prompt.split("DEPENDENCIES:", 1)[1]
        return {"items": [{"address": a, "risk": "medium", "why_it_matters": "It is a dependency.",
                           "migration_notes": ["Move it"], "code_change_hint": "", "confidence": "medium"}
                          for a in dict.fromkeys(re.findall(r'"address":\s*"([^"]+)"', batch))]}
    if "Choose ONE provider" in prompt:
        return {"pick": "leaseweb", "recommendation": "Leaseweb is the choice: it is orderable today and "
                                                      "ExitPilot generates its Terraform."}
    return {"executive_summary": SUMMARY, "top_risks": [], "next_steps": ["Review the report"], "changes": []}


class LiveModel(MistralClient):
    """A reachable model (no network): answers like model_answer and caches like the real clients."""

    label = "Ollama"

    def __init__(self, cache_path):
        super().__init__(api_key="", model="mistral", cache_path=cache_path, offline=False)
        self.calls = 0

    @property
    def online(self):
        return True

    def ask_json(self, prompt, system="Answer only with valid JSON.", retries=2):
        key = self._key(system, prompt)
        if key in self._cache:
            return self._cache[key]
        self.calls += 1
        self._cache[key] = model_answer(prompt)
        self._save_cache()
        return self._cache[key]


@pytest.fixture
def recording(tmp_path):
    """A recording of two examples, made the way `python -m exitpilot.record` makes it."""
    data, summary = rec.record(LiveModel(tmp_path / "rec-cache.json"), ["loja-mista", "loja-lixo"])
    path = tmp_path / "ai_answers.json"
    path.write_text(json.dumps(data))
    return path, data, summary


def offline_client(tmp_path, recorded):
    """Someone who cloned the repo: no API key, no Ollama, an empty cache."""
    return make_llm("mistral", api_key="", cache_path=tmp_path / "own-cache.json", recorded=recorded)


def test_recording_holds_only_the_example_answers_and_who_made_them(recording):
    _, data, summary = recording
    meta = data["recorded_with"]
    assert meta["label"] == "Ollama" and meta["model"] == "mistral" and meta["examples"] == ["loja-mista", "loja-lixo"]
    assert data["answers"] and all(isinstance(k, str) and len(k) == 64 for k in data["answers"])
    assert all(s["report_ai"] and s["market_ai"] and s["dependencies_analysed"] for s in summary)


def test_without_a_model_the_examples_show_the_recorded_ai_text(tmp_path, recording):
    path, _, _ = recording
    llm = offline_client(tmp_path, path)
    assert isinstance(llm, RecordedAnswers) and not llm.online and llm.available
    result = analyse_folder(rec.EXAMPLES / "loja-mista", llm=llm, project_name="loja-mista")
    assert result.report.ai_used and SUMMARY in result.report.markdown
    assert result.ai["provider"] == "Ollama" and result.ai["model"].startswith("mistral, recorded on ")
    assert "AI text by Ollama (mistral, recorded on" in result.report.markdown    # never credits Mistral's API
    assert result.ai["dependencies_analysed"] > 0
    ms = study(result.analysis, city="Lisbon", skip_waste=bool(result.analysis.waste))
    assert ai_recommendation(ms, llm) and ms.recommendation_source == "ai"
    assert "recorded with Ollama (mistral)" in llm.status


def test_a_project_that_was_not_recorded_gets_the_template(tmp_path, recording):
    path, _, _ = recording
    result = analyse_folder(rec.EXAMPLES / "loja-simples", llm=offline_client(tmp_path, path))
    assert not result.report.ai_used and "AI not used (template text)" in result.report.markdown


def test_another_city_is_not_in_the_recording(tmp_path, recording):
    path, _, _ = recording
    llm = offline_client(tmp_path, path)
    result = analyse_folder(rec.EXAMPLES / "loja-lixo", llm=llm, project_name="loja-lixo")
    assert not ai_recommendation(study(result.analysis, city="Berlin", skip_waste=True), llm)


def test_a_reachable_model_is_always_preferred(tmp_path, recording):
    path, _, _ = recording
    live = LiveModel(tmp_path / "live-cache.json")
    llm = RecordedAnswers(live, load_recording(path))
    assert not llm.replaying and llm.label == "Ollama" and llm.model == "mistral"
    analyse_folder(rec.EXAMPLES / "loja-mista", llm=llm, project_name="loja-mista")
    assert live.calls > 0                                            # asked the model, not the recording


def test_the_users_own_cache_wins_and_recorded_answers_are_not_copied_into_it(tmp_path, recording):
    path, data, _ = recording
    key, _ = next(iter(data["answers"].items()))
    own = tmp_path / "own-cache.json"
    own.write_text(json.dumps({key: {"executive_summary": "mine"}}))
    llm = offline_client(tmp_path, path)
    assert llm.inner._cache[key] == {"executive_summary": "mine"}
    analyse_folder(rec.EXAMPLES / "loja-mista", llm=llm, project_name="loja-mista")
    assert json.loads(own.read_text()) == {key: {"executive_summary": "mine"}}


@pytest.mark.parametrize("content", ["", "not json", "[]", '{"answers": {}}', '{"answers": {"k": 1}}',
                                     '{"recorded_with": {"label": "x"}, "answers": {"k": 1}}'])
def test_a_missing_or_broken_recording_is_ignored(tmp_path, content):
    path = tmp_path / "ai_answers.json"
    path.write_text(content)
    assert load_recording(path) is None
    assert type(offline_client(tmp_path, path)) is MistralClient
    assert type(offline_client(tmp_path, tmp_path / "nothing.json")) is MistralClient


def test_recording_can_be_switched_off(tmp_path, recording, monkeypatch):
    path, _, _ = recording
    monkeypatch.setenv("EXITPILOT_RECORDED", str(path))
    assert isinstance(make_llm("mistral", api_key="", cache_path=tmp_path / "c.json"), RecordedAnswers)
    monkeypatch.setenv("EXITPILOT_RECORDED", "0")
    assert type(make_llm("mistral", api_key="", cache_path=tmp_path / "c.json")) is MistralClient


def test_record_command_needs_a_reachable_model(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv("LLM_PROVIDER", "mistral")
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    monkeypatch.setattr("dotenv.load_dotenv", lambda *a, **k: None)          # ignore a developer's .env
    assert rec.main(["--output", str(tmp_path / "out.json")]) == 1
    assert "No model reachable" in capsys.readouterr().err and not (tmp_path / "out.json").exists()
    assert rec.main(["atlantis"]) == 2


def test_cache_key_is_the_one_the_clients_use(tmp_path):
    client = MistralClient(api_key="", model="mistral", cache_path=tmp_path / "c.json")
    assert client._key("sys", "prompt") == cache_key("mistral", "sys", "prompt")


def test_recorded_answers_match_the_current_prompts():
    """If this fails, a prompt, the facts or an example changed since the recording:
    run `python -m exitpilot.record` with Ollama running and commit demo/ai_answers.json."""
    recording = load_recording(RECORDED_ANSWERS)
    if recording is None:
        pytest.skip("no demo/ai_answers.json yet: run `python -m exitpilot.record` with a model")
    meta = recording["recorded_with"]
    answers = recording["answers"]
    for name in meta["examples"]:
        prompts = []

        class Spy:
            available, model, label = True, meta["model"], meta["label"]

            def ask_json(self, prompt, system="Answer only with valid JSON.", retries=2):
                prompts.append((system, prompt))
                return answers.get(cache_key(meta["model"], system, prompt))

        rec.run_example(name, Spy())
        missing = [p for s, p in prompts if cache_key(meta["model"], s, p) not in answers]
        assert not missing, f"{name}: {len(missing)} prompt(s) not in the recording; record again"
