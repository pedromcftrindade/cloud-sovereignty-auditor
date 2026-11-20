"""Quick check of the LLM you configured (Mistral API or a local model server).
Run from the repo root, with the server running:

    python scripts/check_llm.py                                   # uses .env
    LLM_PROVIDER=lmstudio LLM_MODEL=mistral-nemo-instruct-2407 python scripts/check_llm.py

It sends one tiny request and prints the answer and how long it took. If three
short sentences take more than about 15 seconds, a whole report will be slow:
record the cache first and run the demo with EXITPILOT_OFFLINE=1.
"""

import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from exitpilot.llm import make_llm  # noqa: E402

load_dotenv()
try:
    llm = make_llm(offline=False)
except ValueError as exc:
    sys.exit(str(exc))

print("Provider:", llm.label)
print("Model:   ", llm.model or "(none)")
print("Status:  ", llm.status)
if not llm.online:
    sys.exit(1)

started = time.monotonic()
answer = llm.ask_json('Reply with JSON {"ok": true, "company": "<the company that made you>"}', retries=0)
seconds = time.monotonic() - started
if answer is None:
    print("FAILED:", llm.last_error)
    sys.exit(1)
print(f"OK in {seconds:.1f}s, answer: {answer}")
print("(Cached in .cache/llm_cache.json: delete that file to force a new call.)")
