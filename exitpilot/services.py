"""Step 2b: find the third-party services an application really uses.

The Terraform parser (step 2a) sees the cloud infrastructure that is declared.
This module looks at what the application itself talks to, in five places:

  * configuration  .env files, docker-compose and other config files
  * code           URLs and known service hosts in source files
  * dependencies   SDK packages in requirements*.txt, pyproject.toml, Pipfile, package.json
  * images         the registry each container image is pulled from (compose, Dockerfile)
  * runtime        hosts listed in a network_snapshot.json (optional, e.g. exported DNS logs)

Every finding becomes a Resource with origin "service" (see model.py) that keeps
its evidence: file, line, kind and a short detail. Secret values are never
stored, only the variable name, the package name, the image or the host.

Hosts that cannot be a third-party service are skipped on purpose: localhost,
private and reserved IPs, Docker service names without a dot, reserved example
domains (example.com, *.test, *.internal...) and documentation-only domains
such as XML namespaces. Comment lines are skipped too: a URL in a comment is
not a dependency.

Known hosts are matched against services.json. Unknown public hosts are kept,
but left unclassified: they are listed in the report and never guessed.
"""

from __future__ import annotations

import fnmatch
import ipaddress
import json
import re
import tomllib
from collections import defaultdict
from functools import lru_cache
from pathlib import Path
from typing import Any, Optional

from .model import Analysis, ParseError, Resource

SERVICES_PATH = Path(__file__).with_name("services.json")
SNAPSHOT_NAME = "network_snapshot.json"
MAX_FILE_BYTES = 1_000_000
MAX_EVIDENCE = 25

SKIP_DIRS = {".git", ".terraform", "__pycache__", "node_modules", "venv", "env", "site-packages",
             "dist", "build", ".cache", ".pytest_cache", ".mypy_cache", ".tox", ".idea", ".vscode"}
LOCK_FILES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "Pipfile.lock",
              "composer.lock", "go.sum", "Cargo.lock", "uv.lock"}
METADATA_FILES = {"package.json", "pyproject.toml", "Pipfile", "composer.json"}   # read for packages only
CODE_SUFFIXES = {".py", ".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".go", ".php", ".rb", ".java",
                 ".kt", ".cs", ".rs", ".html", ".htm", ".vue", ".svelte", ".css", ".scss", ".sh"}
CONFIG_SUFFIXES = {".yml", ".yaml", ".json", ".toml", ".ini", ".cfg", ".conf", ".properties", ".xml", ".env"}

# Evidence kinds, strongest first (used to order the evidence list)
KIND_ORDER = {"observed": 0, "config": 1, "code": 2, "dependency": 3, "image": 4}

# Hosts that appear in files but are never runtime dependencies (namespaces, schemas, docs)
IGNORED_DOMAINS = ("w3.org", "xmlsoap.org", "json-schema.org", "schemastore.org", "schema.org",
                   "purl.org", "ietf.org", "xmlns.com", "readthedocs.io", "python.org", "mozilla.org")
RESERVED_SUFFIXES = (".localhost", ".local", ".internal", ".test", ".invalid", ".example", ".lan",
                     ".home.arpa", ".svc", ".cluster.local")
RESERVED_HOSTS = ("example.com", "example.net", "example.org")
# Unknown hosts are only reported if their top-level domain looks public (any 2-letter country
# code, or one of these). This keeps names like "redis.cache" (a Docker alias) out of the report.
COMMON_TLDS = {"com", "net", "org", "io", "ai", "co", "dev", "app", "cloud", "tech", "info", "biz",
               "xyz", "me", "sh", "so", "gg", "tv", "eu", "online", "site", "store", "shop", "email",
               "systems", "services", "digital", "solutions", "global", "media", "tools", "run", "host",
               "link", "live", "space", "network", "page"}

URL_RE = re.compile(
    r"\b[a-z][a-z0-9+.\-]*://"                     # scheme: https://, postgres://, wss://...
    r"(?:[^\s/@'\"<>`]+@)?"                         # optional user:password@ (never stored)
    r"([a-z0-9](?:[a-z0-9\-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?)*)",
    re.IGNORECASE,
)
BARE_HOST_RE = re.compile(r"^[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?(?:\.[a-z0-9](?:[a-z0-9\-]*[a-z0-9])?)+(?::\d+)?$",
                          re.IGNORECASE)
ENV_LINE_RE = re.compile(r"^\s*(?:export\s+)?([A-Za-z_][A-Za-z0-9_.\-]*)\s*=\s*(.*)$")
COMPOSE_IMAGE_RE = re.compile(r"^\s*image:\s*[\"']?([^\"'\s#]+)", re.IGNORECASE)
DOCKER_FROM_RE = re.compile(r"^\s*FROM\s+(?:--platform=\S+\s+)?(\S+)(?:\s+AS\s+(\S+))?", re.IGNORECASE)
REQUIREMENT_RE = re.compile(r"^\s*([A-Za-z0-9][A-Za-z0-9._\-]*)")
COMMENT_PREFIXES = ("#", "//", "/*", "*", "<!--", "--", ";")
# A value such as FLASK_APP=app.py or ENTRY=index.js is a file name, not a host, even though
# ".py" and ".js" look like 2-letter country domains.
FILE_EXTENSIONS = {"py", "js", "mjs", "cjs", "ts", "tsx", "jsx", "sh", "rb", "pl", "php", "go", "rs", "cs",
                   "kt", "java", "jar", "tf", "md", "txt", "log", "json", "yml", "yaml", "toml", "ini", "cfg",
                   "conf", "env", "html", "htm", "css", "scss", "sql", "db", "csv", "xml", "pem", "key", "crt",
                   "lock", "zip", "gz", "tar", "pdf", "png", "jpg", "svg", "exe", "dll", "so", "wasm"}
# Inline comments at the end of a code line: "x = 1  # see https://..." or "f()  // https://..."
INLINE_COMMENT_RE = re.compile(r"(?:(?<=\s)#|(?<=\s)//)\s.*$")


# ------------------------------------------------------------------ catalog
@lru_cache(maxsize=1)
def load_services(path: str = str(SERVICES_PATH)) -> dict[str, Any]:
    with open(path, encoding="utf-8") as f:
        data = json.load(f)
    services = data["services"]

    pypi, npm, discovery = {}, {}, set()
    for sid, entry in services.items():
        for name in entry.get("packages", {}).get("pypi", []):
            pypi[_normalise_pypi(name)] = sid
        for name in entry.get("packages", {}).get("npm", []):
            npm[name.lower()] = sid
        if entry.get("bare_match", True) is False:     # e.g. "docker.io" is also an Ubuntu package name
            continue
        discovery.update(d.lower() for d in entry.get("domains", []))
        for pattern in entry.get("host_patterns", []):
            discovery.add(pattern.lower().split("*")[-1].lstrip("."))   # "s3.*.scw.cloud" -> "scw.cloud"

    # Finds known service hosts written without a scheme, e.g. SMTP_HOST=smtp.sendgrid.net.
    # The look-ahead rejects lookalikes such as "sendgrid.com.evil.io".
    alternatives = "|".join(re.escape(d) for d in sorted(discovery, key=len, reverse=True))
    # The look-behind also rejects e-mail addresses ("billing@stripe.com" is not a dependency).
    known_re = re.compile(rf"(?<![a-z0-9.\-@])((?:[a-z0-9\-]+\.)*(?:{alternatives}))(?![a-z0-9\-]|\.[a-z0-9])",
                          re.IGNORECASE)
    return {"services": services, "pypi": pypi, "npm": npm, "known_re": known_re}


def _normalise_pypi(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()       # PEP 503


def normalise_host(host: str) -> str:
    return host.strip().strip("[]").rstrip(".").lower()


def match_host(host: str) -> Optional[str]:
    """Service id for a host, or None. Wildcard patterns beat domains; longer matches win."""
    h = normalise_host(host)
    best, best_score = None, -1
    for sid, entry in load_services()["services"].items():
        for pattern in entry.get("host_patterns", []):
            if fnmatch.fnmatchcase(h, pattern.lower()) and 1000 + len(pattern) > best_score:
                best, best_score = sid, 1000 + len(pattern)
        for domain in entry.get("domains", []):
            d = domain.lower()
            if (h == d or h.endswith("." + d)) and len(d) > best_score:
                best, best_score = sid, len(d)
    return best


def is_internal_host(host: str) -> bool:
    """True for hosts that cannot be a third-party service (local, private, placeholders, docs)."""
    h = normalise_host(host)
    if not h:
        return True
    try:
        ip = ipaddress.ip_address(h)
        return not ip.is_global
    except ValueError:
        pass
    if "." not in h:                                   # "localhost", "db", "minio" (Docker service names)
        return True
    if h.endswith(RESERVED_SUFFIXES):
        return True
    if any(h == d or h.endswith("." + d) for d in RESERVED_HOSTS + IGNORED_DOMAINS):
        return True
    return False


def _looks_public(host: str) -> bool:
    h = normalise_host(host)
    try:
        return ipaddress.ip_address(h).is_global
    except ValueError:
        pass
    tld = h.rsplit(".", 1)[-1]
    return (len(tld) == 2 and tld.isalpha()) or tld in COMMON_TLDS


def registry_of(image: str) -> Optional[str]:
    """Registry host of a container image. 'postgres:16' -> 'docker.io', 'ghcr.io/x/y' -> 'ghcr.io'."""
    if not image or "$" in image or image.lower() == "scratch":
        return None
    first = image.split("/", 1)[0]
    if "/" in image and ("." in first or ":" in first or first == "localhost"):
        return first.split(":", 1)[0].lower()
    return "docker.io"


# ------------------------------------------------------------------ scanning
class _Findings:
    """Collects (key, evidence) pairs. key = service id, or "host:<name>" for unknown hosts."""

    def __init__(self) -> None:
        self.evidence: dict[str, list[dict[str, Any]]] = defaultdict(list)
        self.hosts: dict[str, set[str]] = defaultdict(set)
        self._seen: set[tuple] = set()

    def add_host(self, host: str, file: str, line: Optional[int], kind: str, detail: str) -> None:
        h = normalise_host(host)
        if is_internal_host(h):
            return
        sid = match_host(h)
        if sid is None and not _looks_public(h):
            return
        key = sid or f"host:{h}"
        self.hosts[key].add(h)
        self._add(key, file, line, kind, detail)

    def add_service(self, sid: str, file: str, line: Optional[int], kind: str, detail: str) -> None:
        self._add(sid, file, line, kind, detail)

    def _add(self, key: str, file: str, line: Optional[int], kind: str, detail: str) -> None:
        marker = (key, file, line, kind)
        if marker in self._seen:
            return
        self._seen.add(marker)
        self.evidence[key].append({"file": file, "line": line, "kind": kind, "detail": detail})


def _is_env_file(name: str) -> bool:
    """.env, .env.example, production.env, app.env.example..."""
    return name.startswith(".env") or name.endswith(".env") or ".env." in name


def _wanted(path: Path) -> Optional[str]:
    """Kind of scan for a file ('config' or 'code'), or None to skip it."""
    name = path.name
    if name in LOCK_FILES or name == SNAPSHOT_NAME or path.suffix == ".tf":
        return None
    if _is_env_file(name) or name in ("Pipfile",):
        return "config"
    if _is_dockerfile(name):
        return "config"
    if path.suffix in CONFIG_SUFFIXES:
        return "config"
    if path.suffix in CODE_SUFFIXES:
        return "code"
    if name.startswith("requirements") and path.suffix == ".txt":
        return "config"
    return None


def _iter_files(root: Path):
    for path in sorted(root.rglob("*")):
        rel_parts = path.relative_to(root).parts
        if any(p in SKIP_DIRS or p.startswith(".venv") for p in rel_parts[:-1]):
            continue
        if path.is_file():
            yield path


def _read(path: Path) -> Optional[str]:
    try:
        if path.stat().st_size > MAX_FILE_BYTES:
            return None
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return None                                    # binary or unreadable: nothing to learn


def _is_comment(line: str) -> bool:
    return line.lstrip().startswith(COMMENT_PREFIXES)


def _scan_text(text: str, rel: str, kind: str, found: _Findings, is_env: bool) -> None:
    known_re = load_services()["known_re"]
    for number, line in enumerate(text.splitlines(), 1):
        if _is_comment(line):
            continue
        if kind == "code":
            line = INLINE_COMMENT_RE.sub("", line)      # a URL in a trailing comment is not a dependency
        env = ENV_LINE_RE.match(line) if is_env else None
        detail_for = (lambda host: env.group(1)) if env else (lambda host: host)
        for match in URL_RE.finditer(line):
            found.add_host(match.group(1), rel, number, kind, detail_for(match.group(1)))
        for match in known_re.finditer(line):
            found.add_host(match.group(1), rel, number, kind, detail_for(match.group(1)))
        if env:                                         # SMTP_HOST=smtp.sendgrid.net (no scheme)
            value = env.group(2).strip().strip("'\"")
            tld = value.split(":")[0].rsplit(".", 1)[-1]
            if BARE_HOST_RE.match(value) and tld.isalpha() and tld.lower() not in FILE_EXTENSIONS:
                found.add_host(value.split(":")[0], rel, number, kind, env.group(1))


def _is_dockerfile(name: str) -> bool:
    return name == "Dockerfile" or name.startswith("Dockerfile.") or name.endswith(".dockerfile")


def _scan_images(text: str, rel: str, name: str, found: _Findings) -> None:
    is_dockerfile = _is_dockerfile(name)
    stages: set[str] = set()
    for number, line in enumerate(text.splitlines(), 1):
        if is_dockerfile:
            match = DOCKER_FROM_RE.match(line)
            if not match:
                continue
            image, stage = match.group(1), match.group(2)
            if stage:
                stages.add(stage.lower())
            if image.lower() in stages:                 # FROM build AS final: an earlier stage, not an image
                continue
        else:
            match = COMPOSE_IMAGE_RE.match(line)
            if not match:
                continue
            image = match.group(1)
        registry = registry_of(image)
        if registry:
            found.add_host(registry, rel, number, "image", image)


def _line_of(text: str, needle: str) -> Optional[int]:
    for number, line in enumerate(text.splitlines(), 1):
        if needle in line:
            return number
    return None


def _scan_packages(path: Path, text: str, rel: str, found: _Findings, errors: list[ParseError]) -> None:
    catalog = load_services()
    name = path.name
    if name.startswith("requirements") and path.suffix == ".txt":
        for number, line in enumerate(text.splitlines(), 1):
            if _is_comment(line) or line.strip().startswith("-"):
                continue
            match = REQUIREMENT_RE.match(line)
            sid = match and catalog["pypi"].get(_normalise_pypi(match.group(1)))
            if sid:
                found.add_service(sid, rel, number, "dependency", match.group(1))
    elif name in ("pyproject.toml", "Pipfile"):
        try:
            data = tomllib.loads(text)
        except tomllib.TOMLDecodeError as exc:
            errors.append(ParseError(file=rel, message=f"TOMLDecodeError: {exc}"))
            return
        names = list(data.get("packages", {})) + list(data.get("dev-packages", {}))          # Pipfile
        project = data.get("project", {})
        for spec in project.get("dependencies", []) + [s for group in project.get("optional-dependencies", {}).values() for s in group]:
            match = REQUIREMENT_RE.match(spec)
            if match:
                names.append(match.group(1))
        names += list(data.get("tool", {}).get("poetry", {}).get("dependencies", {}))
        for pkg in names:
            sid = catalog["pypi"].get(_normalise_pypi(pkg))
            if sid:
                found.add_service(sid, rel, _line_of(text, pkg), "dependency", pkg)
    elif name == "package.json":
        try:
            data = json.loads(text)
        except json.JSONDecodeError as exc:
            errors.append(ParseError(file=rel, message=f"JSONDecodeError: {exc}"))
            return
        for section in ("dependencies", "devDependencies", "peerDependencies", "optionalDependencies"):
            for pkg in (data.get(section) or {}):
                sid = catalog["npm"].get(pkg.lower())
                if sid:
                    found.add_service(sid, rel, _line_of(text, f'"{pkg}"'), "dependency", pkg)


def _scan_snapshot(text: str, rel: str, found: _Findings, errors: list[ParseError]) -> None:
    """network_snapshot.json: [{"host": "...", "port": 443, "process": "..."}] or {"connections": [...]}"""
    try:
        data = json.loads(text)
    except json.JSONDecodeError as exc:
        errors.append(ParseError(file=rel, message=f"JSONDecodeError: {exc}"))
        return
    connections = data.get("connections", []) if isinstance(data, dict) else data
    for item in connections if isinstance(connections, list) else []:
        if isinstance(item, dict) and isinstance(item.get("host"), str):
            found.add_host(item["host"], rel, None, "observed", str(item.get("process") or item["host"]))


def collect_services(folder: str | Path) -> tuple[list[Resource], list[str], list[ParseError]]:
    """Scan a folder. Returns (service resources, files scanned, errors)."""
    root = Path(folder)
    found = _Findings()
    files_read: list[str] = []
    errors: list[ParseError] = []

    for path in _iter_files(root):
        rel = path.relative_to(root).as_posix()     # "a/b.py" on every OS (Windows gives "a\\b.py")
        if path.name == SNAPSHOT_NAME:
            text = _read(path)
            if text is not None:
                files_read.append(rel)
                _scan_snapshot(text, rel, found, errors)
            continue
        kind = _wanted(path)
        if kind is None:
            continue
        text = _read(path)
        if text is None:
            continue
        files_read.append(rel)
        is_env = _is_env_file(path.name)
        if path.name not in METADATA_FILES:          # their URLs are homepages and repositories, not runtime
            _scan_text(text, rel, kind, found, is_env=is_env)
        if path.suffix in (".yml", ".yaml") or _is_dockerfile(path.name):   # never "image: str" in Python
            _scan_images(text, rel, path.name, found)
        _scan_packages(path, text, rel, found, errors)

    return _to_resources(found), files_read, errors


def _to_resources(found: _Findings) -> list[Resource]:
    services = load_services()["services"]
    resources = []
    for key in sorted(found.evidence):
        evidence = sorted(found.evidence[key],
                          key=lambda e: (KIND_ORDER.get(e["kind"], 9), e["file"], e["line"] or 0))[:MAX_EVIDENCE]
        hosts = sorted(found.hosts.get(key, set()))
        if key.startswith("host:"):
            host = key.split(":", 1)[1]
            res = Resource(provider="saas", type="service", name=host, file=evidence[0]["file"],
                           config={"hosts": hosts}, origin="service", service=host, evidence=evidence)
        else:
            entry = services[key]
            res = Resource(provider="saas", type="service", name=key, file=evidence[0]["file"],
                           config={"hosts": hosts}, origin="service", service=entry["name"],
                           company=entry["company"], company_country=entry["country"],
                           company_zone=entry["zone"], evidence=evidence)
        resources.append(res)
    return resources


def add_services(analysis: Analysis, folder: str | Path) -> None:
    """Append the services found in `folder` to an Analysis built by the Terraform parser."""
    resources, files_read, errors = collect_services(folder)
    analysis.resources.extend(resources)
    analysis.files_read.extend(f for f in files_read if f not in analysis.files_read)
    analysis.errors.extend(errors)


def evidence_summary(res: Resource, limit: int = 3) -> str:
    """Short human text, e.g. 'config .env:3; dependency requirements.txt:2 (+2 more)'."""
    parts = [f"{e['kind']} {e['file']}" + (f":{e['line']}" if e.get("line") else "") for e in res.evidence[:limit]]
    extra = len(res.evidence) - limit
    return "; ".join(parts) + (f" (+{extra} more)" if extra > 0 else "")
