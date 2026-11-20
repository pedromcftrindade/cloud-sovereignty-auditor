"""The only module that talks to the LLM (Mistral, a European provider).

* Answers are cached on disk (.cache/llm_cache.json). The same question is
  never sent twice, and a cached answer is reused even with no internet or no
  API key ("demo mode").
* Every failure (no key, no network, rate limit, invalid JSON) returns None.
  The caller then falls back to a version without AI: the app never crashes
  because of the LLM.

Environment variables:
  MISTRAL_API_KEY    API key (free "Experiment" plan is enough for the demo)
  MISTRAL_MODEL      default: mistral-small-latest
  EXITPILOT_OFFLINE  "1" = never call the API, only use the cache
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Optional

DEFAULT_MODEL = "mistral-small-latest"
DEFAULT_CACHE = Path(__file__).resolve().parent.parent / ".cache" / "llm_cache.json"


def _import_mistral():
    """mistralai v3 moved the client to mistralai.client; older versions export it at the top."""
    try:
        from mistralai.client import Mistral  # mistralai >= 3
        return Mistral
    except ImportError:
        try:
            from mistralai import Mistral  # mistralai 1.x / 2.x
            return Mistral
        except ImportError:
            return None


def _extract_json(text: str) -> Optional[Any]:
    """Parse JSON even if the model wrapped it in ```json fences or added text around it."""
    if not text:
        return None
    cleaned = re.sub(r"^```(?:json)?\s*|\s*```$", "", text.strip())
    try:
        return json.loads(cleaned)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", cleaned, re.DOTALL)
        if match:
            try:
                return json.loads(match.group(0))
            except json.JSONDecodeError:
                return None
    return None


class MistralClient:
    def __init__(self, api_key: Optional[str] = None, model: Optional[str] = None,
                 cache_path: Path | str = DEFAULT_CACHE, offline: Optional[bool] = None):
        self.model = model or os.getenv("MISTRAL_MODEL", DEFAULT_MODEL)
        self.cache_path = Path(cache_path)
        self.offline = offline if offline is not None else os.getenv("EXITPILOT_OFFLINE") == "1"
        self.last_error: Optional[str] = None
        self._lock = threading.Lock()
        self._cache: dict[str, Any] = self._load_cache()
        self._client = None

        api_key = api_key if api_key is not None else os.getenv("MISTRAL_API_KEY", "")
        mistral_cls = _import_mistral()
        if self.offline:
            self.status = "Offline/demo mode: only cached AI answers are used."
        elif not api_key:
            self.status = "No MISTRAL_API_KEY found: running without AI (cached answers still used)."
        elif mistral_cls is None:
            self.status = "Package 'mistralai' is not installed: running without AI."
        else:
            self._client = mistral_cls(api_key=api_key)
            self.status = f"Connected to Mistral ({self.model})."

    # ---------------------------------------------------------------- cache
    def _load_cache(self) -> dict[str, Any]:
        try:
            return json.loads(self.cache_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            return {}

    def _save_cache(self) -> None:
        try:
            self.cache_path.parent.mkdir(parents=True, exist_ok=True)
            self.cache_path.write_text(json.dumps(self._cache, indent=1, ensure_ascii=False), encoding="utf-8")
        except OSError:
            pass

    def _key(self, system: str, prompt: str) -> str:
        return hashlib.sha256(f"{self.model}\n{system}\n{prompt}".encode()).hexdigest()

    # ---------------------------------------------------------------- public
    @property
    def online(self) -> bool:
        return self._client is not None

    @property
    def available(self) -> bool:
        """True if we can get answers: from the API or from the cache."""
        return self.online or bool(self._cache)

    def ask_json(self, prompt: str, system: str = "Answer only with valid JSON.",
                 retries: int = 2) -> Optional[Any]:
        key = self._key(system, prompt)
        with self._lock:
            if key in self._cache:
                return self._cache[key]
        if not self.online:
            return None

        for attempt in range(retries + 1):
            try:
                resp = self._client.chat.complete(
                    model=self.model,
                    messages=[{"role": "system", "content": system},
                              {"role": "user", "content": prompt}],
                    response_format={"type": "json_object"},
                    temperature=0.2,
                    timeout_ms=45_000,
                )
                content = resp.choices[0].message.content
                if isinstance(content, list):  # some SDK versions return content chunks
                    content = "".join(getattr(c, "text", "") or "" for c in content)
                data = _extract_json(content or "")
                if data is None:
                    self.last_error = "The model did not return valid JSON."
                    continue
                with self._lock:
                    self._cache[key] = data
                    self._save_cache()
                self.last_error = None
                return data
            except Exception as exc:  # noqa: BLE001 - network, auth, rate limit...
                self.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                if "429" in str(exc) and attempt < retries:   # free plan rate limit: wait and retry
                    time.sleep(2 * (attempt + 1))
                    continue
                break   # auth / network errors: retrying will not help
        return None
