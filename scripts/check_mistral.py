"""Quick check that your Mistral key works. Run from the repo root:

    python scripts/check_mistral.py
"""

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from dotenv import load_dotenv  # noqa: E402

from exitpilot.llm import MistralClient  # noqa: E402

load_dotenv()
llm = MistralClient(offline=False)
print("Status:", llm.status)
if not llm.online:
    sys.exit(1)

answer = llm.ask_json('Reply with JSON {"ok": true, "provider": "<your company name>"}', retries=0)
if answer is None:
    print("FAILED:", llm.last_error)
    sys.exit(1)
print("OK, answer:", answer)
print("(The answer is now cached in .cache/llm_cache.json; delete that file to force a new call.)")
