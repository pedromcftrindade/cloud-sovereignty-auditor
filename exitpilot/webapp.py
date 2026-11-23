"""ExitPilot web app: a small HTTP server (Python standard library only) with a JSON API, and a
single-page interface in exitpilot/static/ (HTML, CSS and JavaScript, no build step).

    python -m exitpilot.webapp                       # http://127.0.0.1:8000
    python -m exitpilot.webapp --port 8080
    python -m exitpilot.webapp --host 0.0.0.0        # reachable from other machines: trusted networks only

It uses the same engine as the Streamlit app (app.py), which keeps working.

API (JSON):
    GET  /api/status                        examples, AI model status, cities, default market weights
    POST /api/analyse?example=NAME&ai=1     analyse an example
    POST /api/analyse?name=FILE.zip&ai=1    analyse an uploaded .zip (request body = the zip)
    POST /api/market                        {"id", "city", "weights", "include_basic", "skip_waste"}
    POST /api/market/ai                     same, and the AI writes the recommendation
    POST /api/migrate                       {"id", "exclude_waste"}
    GET  /api/download?id=ID&what=analysis.json | report.md | migration.zip | market.md

Safety: binds to 127.0.0.1 by default; uploads are limited in size and extracted with
safe_extract_zip (no path escapes); example names are checked against the folder list; static
files are served only from exitpilot/static/. Nothing the app reads is executed.
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import tempfile
import threading
import traceback
import uuid
from collections import OrderedDict
from dataclasses import asdict
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Optional
from urllib.parse import parse_qs, quote, urlparse

from .analysis import (LEVEL_COLOURS, SUPPORT_COLOUR, UNKNOWN_COLOUR, Result, analyse_folder,
                       safe_extract_zip, to_dot)
from .export import _migration, build_export
from .llm import make_llm
from .market import (CITIES, DEFAULT_WEIGHTS, MarketStudy, ai_recommendation, load_providers, map_points,
                     market_markdown, study)
from .migrate import Migration, migration_zip, plan_migration
from .model import PROVIDER_NAMES
from .report import ranked_main_resources, service_rows

ROOT = Path(__file__).resolve().parent.parent
EXAMPLES = ROOT / "examples"
STATIC = Path(__file__).with_name("static")
MAX_UPLOAD = 50_000_000
MAX_SESSIONS = 20

EXAMPLE_INFO = {
    "loja-mista": "Online shop on AWS Ireland plus its application: compose, .env, code, network traffic",
    "loja-lixo": "Shop on AWS Ireland with waste and exposed ports: unused disk, IP and SDKs, public database",
    "loja-simples": "Small shop on AWS Ireland: VM, S3, PostgreSQL, load balancer",
    "startup-serverless": "Serverless start-up on AWS USA: Lambda, DynamoDB, SQS, Cognito",
    "azure-loja": "Shop on Azure Netherlands: VM, Blob Storage, PostgreSQL, Cosmos DB",
    "casos-dificeis": "Hard cases: remote modules, a broken file, an unknown type",
}
CONTENT_TYPES = {".html": "text/html; charset=utf-8", ".css": "text/css; charset=utf-8",
                 ".js": "text/javascript; charset=utf-8", ".svg": "image/svg+xml", ".png": "image/png",
                 ".ico": "image/x-icon"}


class State:
    """In-memory sessions (one per analysis). Enough for a local, single-team app."""

    def __init__(self) -> None:
        self.lock = threading.Lock()
        self.sessions: OrderedDict[str, dict[str, Any]] = OrderedDict()
        self.llm = None
        self.llm_error = ""

    def load_llm(self) -> None:
        try:
            from dotenv import load_dotenv
            load_dotenv(ROOT / ".env")
        except ImportError:
            pass
        try:
            self.llm = make_llm()
        except ValueError as exc:
            self.llm, self.llm_error = None, str(exc)

    def put(self, data: dict[str, Any]) -> str:
        sid = uuid.uuid4().hex[:12]
        with self.lock:
            self.sessions[sid] = data
            while len(self.sessions) > MAX_SESSIONS:
                self.sessions.popitem(last=False)
        return sid

    def get(self, sid: str) -> dict[str, Any]:
        with self.lock:
            session = self.sessions.get(sid or "")
        if session is None:
            raise ApiError(404, "This analysis is no longer in memory: run it again.")
        return session


STATE = State()


class ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status, self.message = status, message


# ------------------------------------------------------------------ JSON views
def examples() -> list[dict[str, str]]:
    names = sorted(p.name for p in EXAMPLES.iterdir() if p.is_dir()) if EXAMPLES.exists() else []
    order = {n: i for i, n in enumerate(EXAMPLE_INFO)}
    return [{"name": n, "description": EXAMPLE_INFO.get(n, "")}
            for n in sorted(names, key=lambda n: (order.get(n, 99), n))]


def llm_status() -> dict[str, Any]:
    llm = STATE.llm
    if llm is None:
        return {"label": "AI", "model": "", "status": STATE.llm_error or "No AI model configured.",
                "online": False, "available": False}
    return {"label": getattr(llm, "label", "AI"), "model": getattr(llm, "model", ""),
            "status": getattr(llm, "status", ""), "online": bool(getattr(llm, "online", False)),
            "available": bool(getattr(llm, "available", False))}


def market_json(ms: MarketStudy, analysis=None) -> dict[str, Any]:
    caps = load_providers().get("capability_labels", {})
    offers = []
    for o in ms.offers:
        item = asdict(o)
        item["missing_labels"] = [caps.get(c, c) for c in o.missing]
        item["covered_labels"] = [caps.get(c, c) for c in o.covered]
        offers.append(item)
    best, cheap, close = ms.best, ms.best_by("price"), ms.best_by("proximity")
    return {"city": ms.city, "weights": ms.weights, "checked": ms.checked,
            "recommendation": ms.recommendation, "recommendation_source": ms.recommendation_source,
            "best": best.provider if best else None, "include_basic": ms.include_basic,
            "best_offer": asdict(best) if best else None, "cheapest": asdict(cheap) if cheap else None,
            "closest": asdict(close) if close else None,
            "machines": [asdict(m) for m in ms.machines], "needed": [caps.get(c, c) for c in ms.needed],
            "offers": offers, "map": map_points(analysis, ms) if analysis is not None else []}


def header_json(result: Result) -> dict[str, Any]:
    """The numbers at the top of the Streamlit app, in the same order."""
    analysis, score = result.analysis, result.score
    return {
        "score_rounded": score.rounded,
        "lock_in": score.label.split()[0] if score.value is not None else "n/a",
        "main_resources": score.counted,
        "not_fully_analysed": len(result.unknown) + len(analysis.errors)
                              + sum(1 for r in analysis.resources if r.origin == "module"),
        "by_provider": [{"provider": PROVIDER_NAMES.get(p, p), "score": s.rounded}
                        for p, s in result.by_provider.items()] if len(result.by_provider) > 1 else [],
        "waste_left_out": sum(1 for w in analysis.waste if w.exclude_from_migration),
    }


def inventory_json(result: Result) -> dict[str, Any]:
    """The tables of the Inventory tab."""
    analysis = result.analysis
    return {
        "main": [{"resource": r.address, "provider": r.provider, "category": r.category, "level": r.level,
                  "confidence": r.confidence, "classified_by": r.classified_by, "region": r.region, "file": r.file}
                 for r in ranked_main_resources(analysis.resources)],
        "services": service_rows(analysis),
        "other": [{"resource": r.address, "category": r.category, "level": r.level, "note": r.note, "file": r.file}
                  for r in analysis.resources if r.support or r.level is None],
    }


def migration_json(m: Migration) -> dict[str, Any]:
    data = _migration(m)
    data["files_content"] = m.files
    data["waste_excluded"] = m.waste_excluded
    return data


def analysis_json(sid: str, result: Result, ms: MarketStudy) -> dict[str, Any]:
    data = build_export(result)
    data["id"] = sid
    data["security"] = [asdict(f) for f in getattr(result.analysis, "security", [])]
    data["market"] = market_json(ms, result.analysis)
    data["header"] = header_json(result)
    data["inventory"] = inventory_json(result)
    data["dot"] = {"main": to_dot(result.analysis), "full": to_dot(result.analysis, include_support=True)}
    data["ir"] = result.analysis.to_dict()           # the common representation (Common JSON tab)
    data["legend"] = [[LEVEL_COLOURS[0], "level 0 standard"], [LEVEL_COLOURS[1], "level 1 compatible"],
                      [LEVEL_COLOURS[2], "level 2 managed, open-source equivalent"],
                      [LEVEL_COLOURS[3], "level 3 proprietary"], [SUPPORT_COLOUR, "support"],
                      [UNKNOWN_COLOUR, "unknown"]]
    return data


# ------------------------------------------------------------------ actions
def run_analysis(query: dict[str, str], body: bytes, content_type: str = "") -> dict[str, Any]:
    use_ai = query.get("ai") == "1"
    llm = STATE.llm if use_ai else None
    if body and "application/json" in content_type:
        # Several files (a .zip and/or single files: Terraform, docker-compose, .env, requirements, code),
        # sent as {"files": [{"name": ..., "data": <base64>}]}, like the Streamlit uploader.
        try:
            files = json.loads(body).get("files") or []
        except (json.JSONDecodeError, UnicodeDecodeError, AttributeError) as exc:
            raise ApiError(400, "Invalid upload.") from exc
        if not isinstance(files, list) or not all(isinstance(f, dict) for f in files):
            raise ApiError(400, "Invalid upload.")
        with tempfile.TemporaryDirectory() as tmp:
            for f in files:
                name = Path(str(f.get("name", ""))).name           # never a path: no escaping the folder
                if not name or name in (".", ".."):
                    continue
                try:
                    data = base64.b64decode(str(f.get("data", "")), validate=True)
                except (binascii.Error, ValueError) as exc:
                    raise ApiError(400, f"Could not read {name}: invalid data.") from exc
                if name.lower().endswith(".zip"):
                    try:
                        safe_extract_zip(data, tmp)
                    except Exception as exc:  # noqa: BLE001
                        raise ApiError(400, f"Could not read {name}: {exc}") from exc
                else:
                    (Path(tmp) / name).write_bytes(data)
            result = analyse_folder(tmp, llm=llm, project_name="uploaded project")
        ms = study(result.analysis)
        sid = STATE.put({"result": result, "market": ms, "migration": None, "ai": use_ai})
        return analysis_json(sid, result, ms)
    if body:
        name = Path(query.get("name") or "uploaded-project").stem[:60] or "uploaded-project"
        with tempfile.TemporaryDirectory() as tmp:
            try:
                count = safe_extract_zip(body, tmp)
            except Exception as exc:  # noqa: BLE001 - bad zip, too large...
                raise ApiError(400, f"Could not read the .zip: {exc}") from exc
            if count == 0:
                raise ApiError(400, "The .zip is empty.")
            result = analyse_folder(tmp, llm=llm, project_name=name)
    else:
        name = query.get("example", "")
        if name not in {e["name"] for e in examples()}:
            raise ApiError(400, "Unknown example.")
        result = analyse_folder(EXAMPLES / name, llm=llm, project_name=name)
    ms = study(result.analysis)
    sid = STATE.put({"result": result, "market": ms, "migration": None, "ai": use_ai})
    return analysis_json(sid, result, ms)


def run_market(payload: dict[str, Any], ask_ai: bool) -> dict[str, Any]:
    session = STATE.get(payload.get("id", ""))
    raw = payload.get("weights") if isinstance(payload.get("weights"), dict) else {}
    weights = {k: float(v) for k, v in raw.items()
               if k in DEFAULT_WEIGHTS and isinstance(v, (int, float)) and not isinstance(v, bool) and 0 <= v <= 100}
    city = payload.get("city") if payload.get("city") in CITIES else "Lisbon"
    analysis = session["result"].analysis
    ms = study(analysis, city=city, weights=weights, include_basic=bool(payload.get("include_basic")),
               skip_waste=bool(payload.get("skip_waste", True)) and bool(analysis.waste))
    ai_used = False
    if ask_ai:
        if STATE.llm is None or not getattr(STATE.llm, "available", False):
            raise ApiError(400, "No AI model is available: " + llm_status()["status"])
        ai_used = ai_recommendation(ms, STATE.llm)
    session["market"] = ms
    data = market_json(ms, analysis)
    data["ai_rejected"] = ask_ai and not ai_used
    return data


def run_migration(payload: dict[str, Any]) -> dict[str, Any]:
    session = STATE.get(payload.get("id", ""))
    result = session["result"]
    m = plan_migration(result, exclude_waste=bool(payload.get("exclude_waste", True)) and bool(result.analysis.waste))
    session["migration"] = m
    return migration_json(m)


def download(query: dict[str, str]) -> tuple[bytes, str, str]:
    session = STATE.get(query.get("id", ""))
    result = session["result"]
    project = result.report.project_name
    what = query.get("what")
    if what == "infrastructure.json":
        return (json.dumps(result.analysis.to_dict(), indent=2, ensure_ascii=False).encode(), "application/json",
                "infrastructure.json")
    if what == "analysis.json":
        data = build_export(result, session.get("migration"))
        return (json.dumps(data, indent=2, ensure_ascii=False).encode(), "application/json",
                f"{project}-analysis.json")
    if what == "report.md":
        return result.report.markdown.encode(), "text/markdown; charset=utf-8", f"{project}-report.md"
    if what == "market.md":
        return market_markdown(session["market"]).encode(), "text/markdown; charset=utf-8", f"{project}-market.md"
    if what == "migration.zip":
        if session.get("migration") is None:
            raise ApiError(404, "Generate the migration plan first.")
        return migration_zip(session["migration"]), "application/zip", f"{project}-migration.zip"
    raise ApiError(400, "Unknown download.")


def attachment(filename: str) -> str:
    """Content-Disposition for a download. The name can come from an uploaded file: keep the plain
    filename ASCII-only (quotes, line breaks or emoji would break the header) and give the exact
    name as filename* (RFC 6266), which browsers prefer."""
    plain = re.sub(r"[^A-Za-z0-9._-]+", "-", filename).strip("-.") or "download"
    return f"attachment; filename=\"{plain}\"; filename*=UTF-8''{quote(filename, safe='')}"


# ------------------------------------------------------------------ HTTP
class Handler(BaseHTTPRequestHandler):
    server_version = "ExitPilot"

    def log_message(self, fmt: str, *args: Any) -> None:     # quieter console
        if "/api/" in (args[0] if args else ""):
            super().log_message(fmt, *args)

    def _send(self, status: int, body: bytes, content_type: str, extra: Optional[dict[str, str]] = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, status: int, data: Any) -> None:
        self._send(status, json.dumps(data, ensure_ascii=False).encode(), "application/json; charset=utf-8")

    def _body(self) -> bytes:
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError as exc:
            raise ApiError(400, "Invalid Content-Length.") from exc
        if length < 0:
            raise ApiError(400, "Invalid Content-Length.")
        if length > MAX_UPLOAD:
            raise ApiError(413, f"Upload too large (max {MAX_UPLOAD // 1_000_000} MB).")
        return self.rfile.read(length) if length else b""

    def _payload(self) -> dict[str, Any]:
        try:
            data = json.loads(self._body() or b"{}")
        except json.JSONDecodeError as exc:
            raise ApiError(400, "Invalid JSON.") from exc
        if not isinstance(data, dict):
            raise ApiError(400, "Invalid JSON.")
        return data

    def _query(self) -> dict[str, str]:
        return {k: v[0] for k, v in parse_qs(urlparse(self.path).query).items()}

    def _handle(self, fn) -> None:
        try:
            fn()
        except ApiError as exc:
            self._json(exc.status, {"error": exc.message})
        except Exception as exc:  # noqa: BLE001 - never leave the browser waiting
            traceback.print_exc()
            self._json(500, {"error": f"{type(exc).__name__}: {exc}"})

    def do_GET(self) -> None:
        path = urlparse(self.path).path
        if path == "/api/status":
            self._handle(lambda: self._json(200, {
                "examples": examples(), "llm": llm_status(), "cities": list(CITIES),
                "weights": DEFAULT_WEIGHTS}))
        elif path == "/api/download":
            def send_file():
                body, ctype, filename = download(self._query())
                self._send(200, body, ctype, {"Content-Disposition": attachment(filename)})
            self._handle(send_file)
        elif path in ("/", "/index.html"):
            self._static("index.html")
        elif path.startswith("/static/"):
            self._static(path[len("/static/"):])
        else:
            self._json(404, {"error": "Not found."})

    def do_POST(self) -> None:
        path = urlparse(self.path).path
        routes = {
            "/api/analyse": lambda: self._json(200, run_analysis(self._query(), self._body(),
                                                                 self.headers.get("Content-Type", ""))),
            "/api/market": lambda: self._json(200, run_market(self._payload(), ask_ai=False)),
            "/api/market/ai": lambda: self._json(200, run_market(self._payload(), ask_ai=True)),
            "/api/migrate": lambda: self._json(200, run_migration(self._payload())),
        }
        if path in routes:
            self._handle(routes[path])
        else:
            self._json(404, {"error": "Not found."})

    def _static(self, name: str) -> None:
        target = (STATIC / name).resolve()
        if STATIC.resolve() not in target.parents or not target.is_file():
            self._json(404, {"error": "Not found."})
            return
        self._send(200, target.read_bytes(), CONTENT_TYPES.get(target.suffix, "application/octet-stream"))


def make_server(host: str = "127.0.0.1", port: int = 8000) -> ThreadingHTTPServer:
    STATE.load_llm()
    return ThreadingHTTPServer((host, port), Handler)


def main(argv: Optional[list[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="exitpilot.webapp", description="ExitPilot web app.")
    parser.add_argument("--host", default="127.0.0.1", help="default 127.0.0.1 (this machine only)")
    parser.add_argument("--port", type=int, default=8000)
    args = parser.parse_args(argv)
    server = make_server(args.host, args.port)
    status = llm_status()
    print(f"ExitPilot web app on http://{args.host}:{args.port}")
    print(f"AI: {status['label']} {status['model']} - {status['status']}")
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
