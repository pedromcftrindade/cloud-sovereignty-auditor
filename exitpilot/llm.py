"""The only module that talks to the LLM.

Interchangeable clients, chosen with make_llm() and LLM_PROVIDER:

  mistral (default)  Mistral's API (a European provider)
  ollama             Ollama on this machine, through its native API (/api/chat). This is the
                     server behind "ollama run mistral". The native API lets us set the context
                     window (num_ctx) and JSON mode, which its OpenAI-compatible endpoint cannot:
                     with the default context, long prompts would be cut without any error.
  local              any server with an OpenAI-compatible API running on your own
                     machine: LM Studio, llama.cpp, vLLM... ("lmstudio" only changes the
                     default address; "ollama-openai" uses Ollama's OpenAI-compatible endpoint).
  Nothing leaves the machine with a local model.

* Answers are cached on disk (.cache/llm_cache.json). The same question is
  never sent twice, and a cached answer is reused even with no internet, no
  API key and no model server ("demo mode"). The cache key includes the model
  name: record the cache and run the demo with the same model.
* Every failure (no key, no network, rate limit, invalid JSON, server down)
  returns None. The caller then falls back to a version without AI: the app
  never crashes because of the LLM.
* Recorded answers (demo/ai_answers.json, made with `python -m exitpilot.record`):
  the answers a real model gave for the example projects. They are replayed only
  when no model is reachable, so someone who clones the repository without Ollama
  or an API key still sees what the AI writes. The report then says which model
  wrote them and when ("mistral, recorded on ..."). Other projects get the
  template text, as without AI.

Environment variables:
  LLM_PROVIDER       "mistral" (default), "local", "ollama" or "lmstudio"
  EXITPILOT_OFFLINE  "1" = never call a model, only use the cache (and the recorded answers)
  EXITPILOT_RECORDED path of the recorded answers, or "0" to ignore them

  Mistral:
    MISTRAL_API_KEY  API key (the free "Experiment" plan is enough for the demo)
    MISTRAL_MODEL    default: mistral-small-latest

  Ollama (LLM_PROVIDER=ollama):
    LLM_MODEL        default: mistral (the model of "ollama run mistral")
    LLM_BASE_URL     default: http://localhost:11434 (a trailing /v1 is ignored)
    LLM_NUM_CTX      context window in tokens, default 8192
    LLM_JSON_MODE    default "1" for Ollama (format=json); "0" turns it off
    LLM_TIMEOUT      seconds per request, default 180

  Local server:
    LLM_BASE_URL     default: http://localhost:1234/v1 (LM Studio), http://localhost:11434/v1 (Ollama)
    LLM_MODEL        model name as the server lists it. Set it: the cache is keyed by it
    LLM_API_KEY      only if your server asks for one (LM Studio and Ollama do not)
    LLM_TIMEOUT      seconds per request, default 180 (CPU-only machines are slow)
    LLM_MAX_TOKENS   default 2048
    LLM_JSON_MODE    "1" = send response_format json_object (Ollama accepts it;
                     LM Studio only accepts json_schema, so leave it off there)
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Optional

DEFAULT_MODEL = "mistral-small-latest"
LMSTUDIO_URL = "http://localhost:1234/v1"
OLLAMA_URL = "http://localhost:11434/v1"            # OpenAI-compatible endpoint ("ollama-openai")
OLLAMA_NATIVE_URL = "http://localhost:11434"         # native API ("ollama", recommended)
OLLAMA_DEFAULT_MODEL = "mistral"
DEFAULT_CACHE = Path(__file__).resolve().parent.parent / ".cache" / "llm_cache.json"
RECORDED_ANSWERS = Path(__file__).resolve().parent.parent / "demo" / "ai_answers.json"


def cache_key(model: str, system: str, prompt: str) -> str:
    """Same model, same instructions, same facts: same key. Shared by the cache and the recorded answers."""
    return hashlib.sha256(f"{model}\n{system}\n{prompt}".encode()).hexdigest()


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
    label = "Mistral"                  # shown in the report: "AI text by Mistral (model)"

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
        return cache_key(self.model, system, prompt)

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


# ---------------------------------------------------------------- local model server
def _strip_thinking(text: str) -> str:
    """Reasoning models may put their chain of thought in <think> blocks before the answer."""
    return re.sub(r"<think(?:ing)?>.*?</think(?:ing)?>", "", text or "", flags=re.DOTALL).strip()


class LocalClient(MistralClient):
    """Client for a local server with an OpenAI-compatible API (LM Studio, Ollama...).

    Same interface as MistralClient (ask_json, available, online, status, last_error),
    same disk cache. Uses only the standard library: no extra dependency, and requests
    never go through a system proxy, so they stay on your machine.
    """

    label = "local model"

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None,
                 api_key: Optional[str] = None, cache_path: Path | str = DEFAULT_CACHE,
                 offline: Optional[bool] = None, timeout: Optional[float] = None,
                 json_mode: Optional[bool] = None, default_url: str = LMSTUDIO_URL):
        self.base_url = (base_url or os.getenv("LLM_BASE_URL") or default_url).rstrip("/")
        self.local_api_key = api_key if api_key is not None else os.getenv("LLM_API_KEY", "")
        self.timeout = float(timeout if timeout is not None else os.getenv("LLM_TIMEOUT", "180"))
        self.max_tokens = int(os.getenv("LLM_MAX_TOKENS", "2048"))
        self.json_mode = json_mode if json_mode is not None else os.getenv("LLM_JSON_MODE") == "1"
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
        self._reachable = False
        self._probed_at = 0.0
        super().__init__(api_key="", model="(unset)", cache_path=cache_path, offline=offline)
        self.model = model or os.getenv("LLM_MODEL", "")
        self.explicit_model = bool(self.model)
        if self.offline:
            self.status = ("Offline/demo mode: only cached AI answers are used."
                           + ("" if self.model else " Set LLM_MODEL to the model the cache was recorded with."))
        else:
            self._probe()

    # ---------------------------------------------------------------- server
    def _request(self, path: str, body: Optional[dict] = None, timeout: Optional[float] = None) -> Any:
        headers = {"Content-Type": "application/json"}
        if self.local_api_key:
            headers["Authorization"] = f"Bearer {self.local_api_key}"
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(f"{self.base_url}{path}", data=data, headers=headers,
                                     method="POST" if body is not None else "GET")
        with self._opener.open(req, timeout=timeout or self.timeout) as resp:
            return json.loads(resp.read().decode("utf-8"))

    def _probe(self) -> None:
        """Quick check that the server answers, and (when LLM_MODEL is not set) pick its first model."""
        self._probed_at = time.monotonic()
        try:
            listing = self._request("/models", timeout=3)
            ids = [m.get("id") for m in (listing.get("data") or []) if isinstance(m, dict) and m.get("id")]
        except Exception as exc:  # noqa: BLE001 - any failure means "not reachable"
            self._reachable = False
            self.status = (f"Local model server not reachable at {self.base_url} ({type(exc).__name__}): "
                           "running without AI (cached answers still used). Start the server and reload.")
            return
        self._reachable = True
        if not self.explicit_model and ids:
            self.model = ids[0]
            self.status = (f"Connected to {self.base_url}. Using the first model listed: {self.model}. "
                           "Set LLM_MODEL to choose one: the cache is keyed by the model name.")
        elif self.model and ids and self.model not in ids:
            self.status = (f"Connected to {self.base_url}, but {self.model!r} is not in its model list "
                           f"({', '.join(ids[:5])}). Requests may fail.")
        else:
            self.status = f"Connected to {self.base_url} ({self.model or 'no model listed'})."

    @property
    def online(self) -> bool:
        if self.offline:
            return False
        if not self._reachable and time.monotonic() - self._probed_at > 15:
            self._probe()                      # the server may have been started after the app
        return self._reachable

    # ---------------------------------------------------------------- public
    def _chat(self, system: str, prompt: str) -> str:
        body: dict[str, Any] = {
            "model": self.model, "stream": False, "temperature": 0.2, "max_tokens": self.max_tokens,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
        }
        if self.json_mode:
            body["response_format"] = {"type": "json_object"}
        reply = self._request("/chat/completions", body)
        content = reply["choices"][0]["message"]["content"]
        if isinstance(content, list):          # some servers return content chunks
            content = "".join(c.get("text", "") if isinstance(c, dict) else str(c) for c in content)
        return content or ""

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
                content = self._chat(system, prompt)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", "replace")[:150] if hasattr(exc, "read") else ""
                self.last_error = f"HTTPError {exc.code}: {detail}".strip()
                if exc.code in (429, 503) and attempt < retries:    # busy: wait and retry
                    time.sleep(2 * (attempt + 1))
                    continue
                break
            except Exception as exc:  # noqa: BLE001 - connection lost, timeout, unexpected reply...
                self.last_error = f"{type(exc).__name__}: {str(exc)[:200]}"
                self._reachable = False        # do not stack timeouts: probe again in a few seconds
                self._probed_at = time.monotonic()
                break
            data = _extract_json(_strip_thinking(content))
            if data is None:
                self.last_error = "The model did not return valid JSON."
                continue
            with self._lock:
                self._cache[key] = data
                self._save_cache()
            self.last_error = None
            return data
        return None


class OllamaClient(LocalClient):
    """Ollama through its native API: POST /api/chat, GET /api/tags.

    Same interface and cache as the other clients. Differences from the OpenAI-compatible endpoint:
      * options.num_ctx sets the context window (default 8192). Ollama's default is smaller, and a
        prompt longer than the window is cut silently: the model would then miss the instructions.
      * format="json" makes the model answer with valid JSON.
      * "mistral" matches the model Ollama lists as "mistral:latest".
    """

    label = "Ollama"

    def __init__(self, base_url: Optional[str] = None, model: Optional[str] = None,
                 json_mode: Optional[bool] = None, num_ctx: Optional[int] = None, **kwargs: Any):
        url = (base_url or os.getenv("LLM_BASE_URL") or OLLAMA_NATIVE_URL).strip().rstrip("/")
        if url.endswith("/v1"):
            url = url[:-3]                     # the native API lives at the root, not under /v1
        if not url.startswith(("http://", "https://")):
            url = "http://" + url
        self.num_ctx = int(num_ctx if num_ctx is not None else os.getenv("LLM_NUM_CTX", "8192"))
        if json_mode is None:
            json_mode = os.getenv("LLM_JSON_MODE", "1") != "0"
        kwargs.pop("default_url", None)
        super().__init__(base_url=url, model=model or os.getenv("LLM_MODEL") or OLLAMA_DEFAULT_MODEL,
                         json_mode=json_mode, default_url=url, **kwargs)

    def _matches(self, names: list[str]) -> bool:
        wanted = self.model.lower()
        return any(n.lower() in (wanted, f"{wanted}:latest") for n in names)

    def _probe(self) -> None:
        self._probed_at = time.monotonic()
        try:
            listing = self._request("/api/tags", timeout=3)
            names = [m.get("name") for m in (listing.get("models") or []) if isinstance(m, dict) and m.get("name")]
        except Exception as exc:  # noqa: BLE001 - any failure means "not reachable"
            self._reachable = False
            self.status = (f"Ollama not reachable at {self.base_url} ({type(exc).__name__}): running without AI "
                           "(cached answers still used). Start Ollama (e.g. `ollama serve`) and reload.")
            return
        if names and not self._matches(names):
            self._reachable = False
            self.status = (f"Ollama is running, but model {self.model!r} is not installed "
                           f"(installed: {', '.join(names[:5])}). Run `ollama pull {self.model}` or set LLM_MODEL.")
            return
        self._reachable = True
        self.status = f"Connected to Ollama at {self.base_url} ({self.model}, context {self.num_ctx} tokens)."

    def _chat(self, system: str, prompt: str) -> str:
        body: dict[str, Any] = {
            "model": self.model, "stream": False,
            "messages": [{"role": "system", "content": system}, {"role": "user", "content": prompt}],
            "options": {"temperature": 0.2, "num_ctx": self.num_ctx, "num_predict": self.max_tokens},
        }
        if self.json_mode:
            body["format"] = "json"
        reply = self._request("/api/chat", body)
        return ((reply or {}).get("message") or {}).get("content", "") or ""


# ---------------------------------------------------------------- recorded answers
def load_recording(path: Path | str | None) -> Optional[dict[str, Any]]:
    """The recorded answers file, or None if there is none (or it is not a valid recording)."""
    if not path:
        return None
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(data, dict) or not isinstance(data.get("answers"), dict) or not data["answers"]:
        return None
    if not isinstance(data.get("recorded_with"), dict) or not data["recorded_with"].get("model"):
        return None
    return data


class RecordedAnswers:
    """Wraps the configured client. While its model is reachable, everything goes to it (and its cache).
    While it is not, answers recorded with a real model are replayed for the exact prompts that were
    recorded (the example projects); anything else gets no answer, so the program uses its template.

    label and model then name the model that really wrote the recorded answers, so the report says
    "AI text by Ollama (mistral, recorded on 2026-11-24)" and never credits the configured model."""

    def __init__(self, inner: "MistralClient", recording: dict[str, Any]):
        self.inner = inner
        meta = recording["recorded_with"]
        self.recorded_label = str(meta.get("label") or "AI")
        self.recorded_model = str(meta["model"])
        self.recorded_on = str(meta.get("recorded_on") or "")
        self.answers: dict[str, Any] = recording["answers"]

    def __getattr__(self, name: str) -> Any:          # cache_path, last_error, offline, base_url...
        if name == "inner":
            raise AttributeError(name)
        return getattr(self.inner, name)

    @property
    def replaying(self) -> bool:
        return not self.inner.online

    @property
    def online(self) -> bool:
        return self.inner.online

    @property
    def available(self) -> bool:
        return True

    @property
    def label(self) -> str:
        return self.recorded_label if self.replaying else self.inner.label

    @property
    def model(self) -> str:
        if not self.replaying:
            return self.inner.model
        return self.recorded_model + (f", recorded on {self.recorded_on}" if self.recorded_on else ", recorded")

    @property
    def status(self) -> str:
        if not self.replaying:
            return self.inner.status
        return (f"{self.inner.status} The answers recorded with {self.recorded_label} ({self.recorded_model}) "
                f"for the example projects are shown; other projects get the template text.")

    def ask_json(self, prompt: str, system: str = "Answer only with valid JSON.", retries: int = 2) -> Optional[Any]:
        answer = self.inner.ask_json(prompt, system=system, retries=retries)    # live model or its cache
        if answer is not None or self.inner.online:
            return answer
        return self.answers.get(cache_key(self.recorded_model, system, prompt))


LOCAL_PROVIDERS = {"local": LMSTUDIO_URL, "lmstudio": LMSTUDIO_URL, "lm-studio": LMSTUDIO_URL,
                   "ollama-openai": OLLAMA_URL, "openai_compat": LMSTUDIO_URL}


def make_llm(provider: Optional[str] = None, recorded: Path | str | None = None,
             **kwargs: Any) -> "MistralClient | RecordedAnswers":
    """The client chosen by LLM_PROVIDER (default: Mistral). kwargs go to the client.

    recorded: the recorded answers to replay when no model is reachable (default: EXITPILOT_RECORDED,
    else demo/ai_answers.json if it exists; "0" or "" = none)."""
    name = (provider or os.getenv("LLM_PROVIDER", "mistral")).strip().lower()
    if name == "mistral":
        client = MistralClient(**kwargs)
    elif name == "ollama":
        client = OllamaClient(**kwargs)
    elif name in LOCAL_PROVIDERS:
        client = LocalClient(default_url=LOCAL_PROVIDERS[name], **kwargs)
    else:
        raise ValueError(f"Unknown LLM_PROVIDER {name!r}: use mistral, ollama, lmstudio, local or ollama-openai.")
    if recorded is None:
        recorded = os.getenv("EXITPILOT_RECORDED", str(RECORDED_ANSWERS))
    recording = load_recording(None if str(recorded) in ("", "0") else recorded)
    return RecordedAnswers(client, recording) if recording else client
