"""LocalClient: a model server on this machine with an OpenAI-compatible API (LM Studio, Ollama...)."""

import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import pytest

from exitpilot.analysis import analyse_folder
from exitpilot.llm import LocalClient, MistralClient, OllamaClient, _strip_thinking, make_llm


class FakeServer:
    """A tiny OpenAI-compatible server. `reply(prompt)` returns the text the 'model' answers."""

    def __init__(self, reply=None, models=("fake-model",), status=200):
        self.reply = reply or (lambda prompt: '{"ok": true}')
        self.models = models
        self.status = status
        self.requests = []            # (method, path, body)
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _send(self, code, payload):
                raw = json.dumps(payload).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

            def do_GET(self):
                outer.requests.append(("GET", self.path, None))
                self._send(200, {"data": [{"id": m} for m in outer.models]})

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                outer.requests.append(("POST", self.path, body))
                if outer.status != 200:
                    return self._send(outer.status, {"error": "server says no"})
                text = outer.reply(body["messages"][-1]["content"])
                self._send(200, {"choices": [{"message": {"content": text}}]})

        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.url = f"http://127.0.0.1:{self.httpd.server_port}/v1"
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    @property
    def posts(self):
        return [r for r in self.requests if r[0] == "POST"]

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in ("LLM_PROVIDER", "LLM_BASE_URL", "LLM_MODEL", "LLM_API_KEY", "LLM_TIMEOUT", "LLM_JSON_MODE",
                 "EXITPILOT_OFFLINE", "MISTRAL_API_KEY"):
        monkeypatch.delenv(name, raising=False)


@pytest.fixture
def server():
    created = []

    def make(**kwargs):
        s = FakeServer(**kwargs)
        created.append(s)
        return s

    yield make
    for s in created:
        s.close()


def client(server, tmp_path, **kwargs):
    return LocalClient(base_url=server.url, cache_path=tmp_path / "c.json", **kwargs)


# ------------------------------------------------------------------ connecting
def test_connects_and_uses_the_first_model_listed(server, tmp_path):
    llm = client(server(models=("nemo-12b", "other")), tmp_path)
    assert llm.online and llm.available
    assert llm.model == "nemo-12b"
    assert "Using the first model listed: nemo-12b" in llm.status


def test_explicit_model_is_kept(server, tmp_path):
    llm = client(server(models=("nemo-12b",)), tmp_path, model="mistral-small-3")
    assert llm.model == "mistral-small-3"
    assert "not in its model list" in llm.status


def test_server_down_means_no_ai_but_no_crash(tmp_path):
    llm = LocalClient(base_url="http://127.0.0.1:9/v1", cache_path=tmp_path / "c.json")
    assert not llm.online and not llm.available
    assert "not reachable" in llm.status
    assert llm.ask_json("hello") is None


def test_cached_answers_work_without_the_server(server, tmp_path):
    srv = server()
    first = client(srv, tmp_path, model="m1")
    assert first.ask_json("question") == {"ok": True}
    down = LocalClient(base_url="http://127.0.0.1:9/v1", model="m1", cache_path=tmp_path / "c.json")
    assert down.available and down.ask_json("question") == {"ok": True}      # no server, cache hit
    other_model = LocalClient(base_url="http://127.0.0.1:9/v1", model="m2", cache_path=tmp_path / "c.json")
    assert other_model.ask_json("question") is None                           # cache is keyed by the model


def test_offline_never_calls_the_server(server, tmp_path):
    srv = server()
    llm = client(srv, tmp_path, offline=True)
    assert not llm.online and llm.ask_json("question") is None
    assert srv.requests == []
    assert "Set LLM_MODEL" in llm.status


# ------------------------------------------------------------------ asking
def test_request_shape_and_cache(server, tmp_path):
    srv = server(reply=lambda p: '```json\n{"answer": 42}\n```')
    llm = client(srv, tmp_path)
    assert llm.ask_json("what is it?") == {"answer": 42}                      # fences are removed
    assert llm.ask_json("what is it?") == {"answer": 42}                      # second time: cache
    assert len(srv.posts) == 1
    path, body = srv.posts[0][1], srv.posts[0][2]
    assert path == "/v1/chat/completions"
    assert body["model"] == "fake-model" and body["stream"] is False
    assert [m["role"] for m in body["messages"]] == ["system", "user"]
    assert "response_format" not in body                                      # LM Studio rejects json_object


def test_json_mode_is_opt_in(server, tmp_path):
    srv = server()
    client(srv, tmp_path, json_mode=True).ask_json("q")
    assert srv.posts[0][2]["response_format"] == {"type": "json_object"}


def test_reasoning_blocks_are_removed(server, tmp_path):
    srv = server(reply=lambda p: '<think>I should {answer} {"wrong": 1}</think>\n{"right": 2}')
    assert client(srv, tmp_path).ask_json("q") == {"right": 2}
    assert _strip_thinking("<thinking>x</thinking>y") == "y"


def test_invalid_json_is_retried_then_gives_up(server, tmp_path):
    srv = server(reply=lambda p: "I am sorry, I cannot do that.")
    llm = client(srv, tmp_path)
    assert llm.ask_json("q", retries=1) is None
    assert len(srv.posts) == 2
    assert llm.last_error == "The model did not return valid JSON."


def test_http_error_returns_none(server, tmp_path):
    llm = client(server(status=500), tmp_path)
    assert llm.ask_json("q") is None
    assert "HTTPError 500" in llm.last_error


def test_connection_lost_during_a_request_does_not_stack_timeouts(server, tmp_path):
    srv = server()
    llm = client(srv, tmp_path)
    srv.close()
    assert llm.ask_json("q") is None
    assert not llm.online                                                     # re-probed only after 15 s


# ------------------------------------------------------------------ the factory
def test_make_llm_picks_the_provider(monkeypatch, tmp_path):
    assert type(make_llm(cache_path=tmp_path / "c.json")) is MistralClient
    monkeypatch.setenv("LLM_PROVIDER", "ollama")                     # native API: honours num_ctx
    llm = make_llm(cache_path=tmp_path / "c.json", offline=True)
    assert type(llm) is OllamaClient and llm.base_url == "http://localhost:11434"
    monkeypatch.setenv("LLM_PROVIDER", "ollama-openai")              # OpenAI-compatible endpoint
    llm = make_llm(cache_path=tmp_path / "c.json", offline=True)
    assert type(llm) is LocalClient and llm.base_url == "http://localhost:11434/v1"
    monkeypatch.setenv("LLM_PROVIDER", "lmstudio")
    assert make_llm(cache_path=tmp_path / "c.json", offline=True).base_url == "http://localhost:1234/v1"
    monkeypatch.setenv("LLM_BASE_URL", "http://10.0.0.5:8080/v1/")
    assert make_llm(cache_path=tmp_path / "c.json", offline=True).base_url == "http://10.0.0.5:8080/v1"


def test_make_llm_rejects_an_unknown_provider():
    with pytest.raises(ValueError, match="Unknown LLM_PROVIDER"):
        make_llm("skynet")


# ------------------------------------------------------------------ end to end
def test_report_written_by_a_local_model(examples, server, tmp_path):
    def reply(prompt):
        if "Types to classify" in prompt:
            return '{"results": []}'
        return json.dumps({"executive_summary": "A local model wrote this summary.",
                           "top_risks": [{"address": "aws_db_instance.orders", "why": "Managed DB.",
                                          "recommendation": "Use PostgreSQL."},
                                         {"address": "aws_quantum_computer.q", "why": "Invented", "recommendation": "-"}],
                           "next_steps": ["Talk to the team."]})

    llm = client(server(reply=reply, models=("nemo-12b",)), tmp_path)
    result = analyse_folder(examples / "loja-mista", llm=llm)
    assert result.report.ai_used
    assert "AI text by local model (nemo-12b)" in result.report.markdown
    assert "A local model wrote this summary." in result.report.markdown
    assert "aws_quantum_computer" not in result.report.markdown               # invented resources are dropped
    assert result.score.value == 61.5                                          # numbers do not depend on the AI


def test_numbers_are_identical_with_and_without_the_model(examples, server, tmp_path):
    llm = client(server(reply=lambda p: "not json"), tmp_path)
    with_ai = analyse_folder(examples / "loja-mista", llm=llm)
    without = analyse_folder(examples / "loja-mista")
    assert not with_ai.report.ai_used
    assert (with_ai.score.value, with_ai.exposure.outside_eu) == (without.score.value, without.exposure.outside_eu)
