import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
EXAMPLES = ROOT / "examples"


class FakeLLM:
    """Stands in for MistralClient in tests: returns a fixed answer, never uses the network."""

    model = "fake-model"

    def __init__(self, answer):
        self.answer = answer
        self.available = True
        self.prompts = []

    def ask_json(self, prompt, system="", retries=0):
        self.prompts.append(prompt)
        return self.answer(prompt) if callable(self.answer) else self.answer


@pytest.fixture
def examples():
    return EXAMPLES


@pytest.fixture
def fake_llm():
    return FakeLLM


@pytest.fixture(autouse=True)
def no_recorded_answers(monkeypatch):
    """The answers recorded in demo/ai_answers.json would turn template text into AI text in the app
    tests. Tests that need them pass the file explicitly (test_recorded_answers.py)."""
    monkeypatch.setenv("EXITPILOT_RECORDED", "0")
