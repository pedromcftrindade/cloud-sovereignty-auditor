"""Ollama (native API) and the AI's "What to change" suggestions in the report.

A tiny HTTP server stands in for Ollama, so these tests need neither Ollama nor a model.
"""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from exitpilot.analysis import analyse_folder
from exitpilot.llm import OllamaClient, make_llm


class FakeOllama:
    """Answers GET /api/tags and POST /api/chat like Ollama does, and records the requests."""

    def __init__(self, models=("mistral:latest",), answer='{"ok": true}'):
        self.models, self.answer, self.requests = list(models), answer, []
        fake = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, data):
                body = json.dumps(data).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if self.path == "/api/tags":
                    self._send({"models": [{"name": n} for n in fake.models]})
                else:
                    self.send_error(404)

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                fake.requests.append((self.path, body))
                prompt = body["messages"][-1]["content"]
                content = fake.answer(prompt) if callable(fake.answer) else fake.answer
                self._send({"model": body["model"], "message": {"role": "assistant", "content": content},
                            "done": True})

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.server.server_address[1]}"
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()


@pytest.fixture
def ollama():
    servers = []

    def start(**kwargs):
        servers.append(FakeOllama(**kwargs))
        return servers[-1]

    yield start
    for s in servers:
        s.close()


# ------------------------------------------------------------------ transport
def test_ollama_native_request(ollama, tmp_path):
    server = ollama()
    llm = OllamaClient(base_url=server.url, cache_path=tmp_path / "c.json")
    assert llm.online and "Connected to Ollama" in llm.status
    assert llm.model == "mistral"                                   # "ollama run mistral"
    assert llm.ask_json("say ok") == {"ok": True}
    path, body = server.requests[0]
    assert path == "/api/chat" and body["format"] == "json" and body["stream"] is False
    assert body["options"]["num_ctx"] == 8192                       # long prompts are not cut
    assert llm.ask_json("say ok") == {"ok": True} and len(server.requests) == 1   # second time: cache


def test_ollama_url_with_v1_and_env(ollama, tmp_path, monkeypatch):
    server = ollama()
    monkeypatch.setenv("LLM_PROVIDER", "ollama")
    monkeypatch.setenv("LLM_BASE_URL", server.url + "/v1")         # the old OpenAI-style address still works
    monkeypatch.setenv("LLM_NUM_CTX", "4096")
    llm = make_llm(cache_path=tmp_path / "c.json")
    assert isinstance(llm, OllamaClient) and llm.base_url == server.url
    llm.ask_json("x")
    assert server.requests[0][1]["options"]["num_ctx"] == 4096


def test_ollama_model_not_installed(ollama, tmp_path):
    server = ollama(models=["llama3.2:latest"])
    llm = OllamaClient(base_url=server.url, cache_path=tmp_path / "c.json")
    assert not llm.online and "ollama pull mistral" in llm.status
    assert llm.ask_json("x") is None


def test_ollama_not_running(tmp_path):
    llm = OllamaClient(base_url="http://127.0.0.1:9", cache_path=tmp_path / "c.json")
    assert not llm.online and "not reachable" in llm.status
    assert llm.ask_json("x") is None


def test_full_analysis_through_ollama(ollama, tmp_path, examples):
    def answer(prompt):
        if "DEPENDENCIES:" in prompt:              # ai_analysis.enrich: one batch of dependencies
            return json.dumps({"items": [{"address": "aws_db_instance.db", "risk": "medium",
                                          "why_it_matters": "Holds the orders.", "migration_notes": ["Dump it"],
                                          "confidence": "high"}]})
        return json.dumps({"executive_summary": "The shop runs on AWS.", "top_risks": [], "next_steps": [],
                           "changes": [{"item": "service.twilio", "change": "Delete the Twilio SDK.",
                                        "why": "Never imported."}]})

    server = ollama(answer=answer)
    llm = OllamaClient(base_url=server.url, cache_path=tmp_path / "c.json")
    result = analyse_folder(examples / "loja-lixo", llm=llm)
    assert result.report.ai_used and "AI text by Ollama (mistral)" in result.report.markdown
    assert result.report.changes[0] == {"item": "service.twilio", "change": "Delete the Twilio SDK.",
                                        "why": "Never imported.", "source": "ai"}
    assert all(path == "/api/chat" for path, _ in server.requests)


# ------------------------------------------------------------------ "What to change"
def test_ai_changes_are_validated(examples, fake_llm):
    answer = {"executive_summary": "Summary.", "top_risks": [], "next_steps": [],
              "changes": [{"item": "aws_ebs_volume.old_backup", "change": "Delete the old 500 GB disk.", "why": "Unused."},
                          {"item": "aws_quantum.q", "change": "Invented!", "why": "-"},        # not in the facts
                          {"item": "service.stripe", "change": "", "why": "empty change"},      # nothing to say
                          {"item": "aws_ebs_volume.old_backup", "change": "Duplicate", "why": "-"},
                          {"item": "`service.twilio`", "change": "Remove the SDK.", "why": "Unused."}]}
    r = analyse_folder(examples / "loja-lixo", llm=fake_llm(lambda p: answer if "FACTS" in p else {}))
    assert [c["item"] for c in r.report.changes] == ["aws_ebs_volume.old_backup", "service.twilio"]
    assert "### What to change (suggested by the AI" in r.report.markdown
    assert "aws_quantum" not in r.report.markdown


def test_changes_without_ai_come_from_the_catalogs(examples):
    r = analyse_folder(examples / "loja-lixo")
    assert "### What to change (from the catalogs)" in r.report.markdown
    items = [c["item"] for c in r.report.changes]
    assert "aws_ebs_volume.old_backup" in items and all(c["source"] == "catalog" for c in r.report.changes)


def test_numbers_are_identical_with_and_without_ai(ollama, tmp_path, examples):
    server = ollama(answer='{"items": [], "results": [], "executive_summary": "x", "changes": []}')
    llm = OllamaClient(base_url=server.url, cache_path=tmp_path / "c.json")
    with_ai, without = analyse_folder(examples / "loja-lixo", llm=llm), analyse_folder(examples / "loja-lixo")
    assert with_ai.score.value == without.score.value
    assert [w.address for w in with_ai.analysis.waste] == [w.address for w in without.analysis.waste]
