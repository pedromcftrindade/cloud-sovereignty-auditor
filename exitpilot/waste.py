"""Step 2c: find suspected waste, i.e. things that look unused.

Moving to a new provider is the best moment to stop paying for things nobody
uses. This module only looks for patterns we can prove from the files, and
every finding is a suspicion for people to review: nothing is ever deleted.

Infrastructure (Terraform):
  * disabled_resource   count = 0 (or an empty for_each): declared, but creates nothing
  * unattached_disk     a disk no machine or attachment refers to
  * unattached_ip       a public IP attached to nothing
  * unused_firewall     a security group no resource uses (its own rules do not count)
  * orphan_target_group a load balancer target group no listener or attachment uses

Application (uses the evidence collected by services.py):
  * unused_service      every trace of a service is dead: its SDK is installed but never
                        imported, and its variables are set but never read
  * unused_package      the SDK is never imported, but the service is used another way
  * unused_env_var      a variable in a .env file that no code or config file reads

Rules that protect against false positives:
  * a reference anywhere else (another resource, a module input, an output, a local) counts as use;
  * traffic seen in network_snapshot.json always counts as use;
  * application rules only run when the folder contains code to check against;
  * variables that SDKs and frameworks read on their own (SENTRY_DSN, DATABASE_URL, FLASK_*...)
    are never reported.

The LLM is not used here: "unused" must be a fact we can show, not a guess.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

from .model import Analysis, Resource, WasteFinding
from .parser import parse_text
from .services import (CODE_SUFFIXES, CONFIG_SUFFIXES, ENV_LINE_RE, LOCK_FILES, _is_comment,
                       _is_dockerfile, _is_env_file, _iter_files, _normalise_pypi, _read)

# ------------------------------------------------------------------ Terraform rules
# type -> (kind, title, confidence)
ORPHAN_RULES: dict[str, tuple[str, str, str]] = {
    "aws_ebs_volume":                 ("unattached_disk", "Disk not attached to any machine", "high"),
    "azurerm_managed_disk":           ("unattached_disk", "Disk not attached to any machine", "high"),
    "google_compute_disk":            ("unattached_disk", "Disk not attached to any machine", "high"),
    "aws_eip":                        ("unattached_ip", "Public IP address attached to nothing", "high"),
    "azurerm_public_ip":              ("unattached_ip", "Public IP address attached to nothing", "high"),
    "google_compute_address":         ("unattached_ip", "Public IP address attached to nothing", "high"),
    "aws_security_group":             ("unused_firewall", "Security group not used by any resource", "medium"),
    "azurerm_network_security_group": ("unused_firewall", "Security group not used by any resource", "medium"),
    "aws_lb_target_group":            ("orphan_target_group", "Target group with no listener or targets", "medium"),
}
# A resource that attaches itself (aws_eip { instance = ... }) is in use even if nothing refers to it
SELF_ATTACH_KEYS = {"aws_eip": ("instance", "network_interface", "associate_with_private_ip")}
# References from these types do not prove use (a security group's own rules)
NOT_A_USE = {"aws_security_group": ("aws_security_group_rule", "aws_vpc_security_group_ingress_rule",
                                    "aws_vpc_security_group_egress_rule")}
VARIABLE_ONLY = re.compile(r"^\$\{var\.([A-Za-z0-9_-]+)\}$")

# ------------------------------------------------------------------ application rules
PY_FROM_RE = re.compile(r"^\s*from\s+([A-Za-z_][\w.]*)\s+import\s+(.+)$")
PY_IMPORT_RE = re.compile(r"^\s*import\s+(.+)$")
JS_IMPORT_RE = re.compile(r"""(?:require\(\s*|import\(\s*|\bfrom\s+|^\s*import\s+)['"]([^'"]+)['"]""")
PY_SUFFIXES = {".py"}
JS_SUFFIXES = {".js", ".mjs", ".cjs", ".ts", ".tsx", ".jsx", ".vue", ".svelte"}
# PyPI name -> import name(s) when they differ from the usual "name with - replaced by _"
PYPI_IMPORTS = {
    "sentry-sdk": ["sentry_sdk"], "firebase-admin": ["firebase_admin"],
    "google-generativeai": ["google.generativeai"], "google-genai": ["google.genai"],
    "auth0-python": ["auth0"], "mollie-api-python": ["mollie"], "adyen": ["adyen"],
}
# Read by SDKs, frameworks or container images on their own: never reported as unused
IMPLICIT_ENV = {
    "SENTRY_DSN", "SENTRY_ENVIRONMENT", "SENTRY_RELEASE", "DATABASE_URL", "REDIS_URL", "PORT", "HOST",
    "DEBUG", "SECRET_KEY", "ALLOWED_HOSTS", "TZ", "LANG", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY",
    "OPENAI_API_KEY", "OPENAI_BASE_URL", "ANTHROPIC_API_KEY", "STRIPE_API_KEY", "SENDGRID_API_KEY",
    "TWILIO_ACCOUNT_SID", "TWILIO_AUTH_TOKEN",
}
IMPLICIT_PREFIXES = ("FLASK_", "DJANGO_", "NEXT_PUBLIC_", "VITE_", "REACT_APP_", "NODE_", "NPM_", "PYTHON",
                     "DD_", "OTEL_", "SPRING_", "AWS_", "AZURE_", "ARM_", "GOOGLE_", "TF_VAR_", "POSTGRES_",
                     "PG", "MYSQL_", "MARIADB_", "MONGO_INITDB_", "REDIS_", "RABBITMQ_", "KC_", "KEYCLOAK_",
                     "GF_", "DOCKER_", "COMPOSE_", "MISTRAL_", "EXITPILOT_")
MAX_ENV_FINDINGS = 15


# ------------------------------------------------------------------ helpers
def _tf_texts(root: Path) -> list[tuple[str, dict[str, Any]]]:
    out = []
    for path in sorted(root.rglob("*.tf")):
        if any(part in {".terraform", ".git"} for part in path.relative_to(root).parts):
            continue
        try:
            out.append((path.relative_to(root).as_posix(), parse_text(path.read_text(encoding="utf-8"))))
        except Exception:  # noqa: BLE001 - broken files are already reported by the parser
            continue
    return out


def _variables(parsed: list[tuple[str, dict[str, Any]]]) -> dict[str, Any]:
    values: dict[str, Any] = {}
    for _, data in parsed:
        for block in data.get("variable", []) or []:
            for name, body in block.items():
                if isinstance(body, dict) and "default" in body:
                    values[name] = body["default"]
    return values


def _is_disabled(config: dict[str, Any], variables: dict[str, Any]) -> Optional[str]:
    """'count = 0' / 'for_each = {}' (directly or through a variable default) -> the reason."""
    for key in ("count", "for_each"):
        if key not in config:
            continue
        value = config[key]
        if isinstance(value, str):
            match = VARIABLE_ONLY.match(value.strip())
            if match and match.group(1) in variables:
                value = variables[match.group(1)]
        if key == "count" and value in (0, "0", False):
            return "count = 0: the block is declared but creates nothing."
        if key == "for_each" and value in ({}, [], "{}", "[]"):
            return "for_each is empty: the block is declared but creates nothing."
    return None


def _outside_resource_text(parsed: list[tuple[str, dict[str, Any]]]) -> str:
    """Modules, outputs, locals and data blocks as text: a reference there counts as use."""
    parts = []
    for _, data in parsed:
        for key, value in data.items():
            if key != "resource":
                parts.append(json.dumps(value, default=str))
    return "\n".join(parts)


def _terraform_waste(analysis: Analysis, root: Path) -> list[WasteFinding]:
    parsed = _tf_texts(root)
    variables = _variables(parsed)
    elsewhere = _outside_resource_text(parsed)
    findings: list[WasteFinding] = []
    terraform = [r for r in analysis.resources if r.origin == "resource"]

    for res in terraform:
        reason = _is_disabled(res.config, variables)
        if reason:
            findings.append(WasteFinding(
                kind="disabled_resource", address=res.address, title="Disabled resource (creates nothing)",
                reason=reason, confidence="high", exclude_from_migration=True,
                evidence=[{"file": res.file, "line": None, "detail": res.address}]))

    disabled = {f.address for f in findings}
    for res in terraform:
        rule = ORPHAN_RULES.get(res.type)
        if rule is None or res.address in disabled:
            continue
        if any(k in res.config for k in SELF_ATTACH_KEYS.get(res.type, ())):
            continue
        ignored = NOT_A_USE.get(res.type, ())
        used_by = [r.address for r in terraform
                   if res.address in r.depends_on and r.type not in ignored and r.address not in disabled]
        if used_by or re.search(rf"\b{re.escape(res.address)}\b", elsewhere):
            continue
        kind, title, confidence = rule
        findings.append(WasteFinding(
            kind=kind, address=res.address, title=title, confidence=confidence,
            reason=("Nothing in the analysed code refers to it. If it is attached by hand or by another "
                    "project, keep it; otherwise it is probably costing money for nothing."),
            exclude_from_migration=True, evidence=[{"file": res.file, "line": None, "detail": res.address}]))
    return findings


def _scan_code(root: Path) -> dict[str, Any]:
    """Imports and searchable text of the project (env files and lock files excluded)."""
    py_imports: set[str] = set()
    js_imports: set[str] = set()
    corpus: list[str] = []
    has_py = has_js = has_code = False
    for path in _iter_files(root):
        name, suffix = path.name, path.suffix.lower()
        if _is_env_file(name) or name in LOCK_FILES:
            continue
        searchable = (suffix in CODE_SUFFIXES or suffix in CONFIG_SUFFIXES or suffix == ".tf"
                      or _is_dockerfile(name))
        if not searchable:
            continue
        text = _read(path)
        if text is None:
            continue
        corpus.append(text)
        if suffix in CODE_SUFFIXES:
            has_code = True
        if suffix in PY_SUFFIXES:
            has_py = True
            for line in text.splitlines():
                if _is_comment(line):
                    continue
                m = PY_FROM_RE.match(line)
                if m:
                    base = m.group(1).lower()
                    py_imports.add(base)
                    for item in re.split(r"[,()\s]+", m.group(2)):
                        if item and item not in ("as", "*") and item.isidentifier():
                            py_imports.add(f"{base}.{item.lower()}")
                    continue
                m = PY_IMPORT_RE.match(line)
                if m:
                    for item in m.group(1).split(","):
                        module = item.strip().split(" as ")[0].strip()
                        if module:
                            py_imports.add(module.lower())
        if suffix in JS_SUFFIXES:
            has_js = True
            for m in JS_IMPORT_RE.finditer(text):
                js_imports.add(m.group(1).lower())
    return {"py": py_imports, "js": js_imports, "corpus": "\n".join(corpus),
            "has_py": has_py, "has_js": has_js, "has_code": has_code}


def _python_imported(package: str, imports: set[str]) -> bool:
    names = PYPI_IMPORTS.get(_normalise_pypi(package), [_normalise_pypi(package).replace("-", "_")])
    return any(m == n or m.startswith(n + ".") for n in names for m in imports)


def _js_imported(package: str, imports: set[str]) -> bool:
    p = package.lower()
    return any(m == p or m.startswith(p + "/") for m in imports)


def _env_variables(root: Path) -> list[tuple[str, str, int]]:
    out = []
    for path in _iter_files(root):
        if not _is_env_file(path.name):
            continue
        text = _read(path)
        if text is None:
            continue
        rel = path.relative_to(root).as_posix()
        for number, line in enumerate(text.splitlines(), 1):
            if _is_comment(line):
                continue
            m = ENV_LINE_RE.match(line)
            if m:
                out.append((m.group(1), rel, number))
    return out


def _sentence(text: str) -> str:
    return text[:1].upper() + text[1:]


def _is_implicit(var: str) -> bool:
    return var in IMPLICIT_ENV or var.startswith(IMPLICIT_PREFIXES)


def _application_waste(analysis: Analysis, root: Path) -> list[WasteFinding]:
    code = _scan_code(root)
    if not code["has_code"]:
        return []                                    # nothing to check against: say nothing
    findings: list[WasteFinding] = []

    # Variables set in .env files but never read anywhere
    unused_vars: dict[str, list[tuple[str, int]]] = {}
    for var, file, line in _env_variables(root):
        if _is_implicit(var):
            continue
        if not re.search(rf"\b{re.escape(var)}\b", code["corpus"]):
            unused_vars.setdefault(var, []).append((file, line))

    # Services whose every trace is dead
    for res in analysis.resources:
        if res.origin != "service" or not res.evidence:
            continue
        dead, live, unused_pkgs = [], [], []
        for e in res.evidence:
            kind, detail, file = e.get("kind"), str(e.get("detail", "")), str(e.get("file", ""))
            if kind == "dependency":
                is_npm = file.endswith("package.json")
                if (is_npm and not code["has_js"]) or (not is_npm and not code["has_py"]):
                    live.append(e)                   # no code in that language: cannot tell
                    continue
                imported = _js_imported(detail, code["js"]) if is_npm else _python_imported(detail, code["py"])
                (live if imported else dead).append(e)
                if not imported:
                    unused_pkgs.append((detail, file, e.get("line")))
            elif kind == "config" and _is_env_file(Path(file).name) and detail in unused_vars:
                dead.append(e)
            else:
                live.append(e)                       # code, observed traffic, images, other config
        if not dead:
            continue
        evidence = [{"file": e["file"], "line": e.get("line"), "detail": e.get("detail")} for e in dead]
        name = res.service or res.name
        if not live:
            parts = []
            if unused_pkgs:
                parts.append("its SDK (" + ", ".join(sorted({p for p, _, _ in unused_pkgs}))
                             + ") is installed but never imported")
            dead_vars = sorted({str(e.get("detail")) for e in dead if e.get("kind") == "config"})
            if dead_vars:
                parts.append("its variables (" + ", ".join(dead_vars) + ") are set but never read")
            findings.append(WasteFinding(
                kind="unused_service", address=res.address, title=f"{name}: configured but apparently not used",
                reason=_sentence(" and ".join(parts)) + ". Remove it instead of migrating it.",
                confidence="medium", exclude_from_migration=True, evidence=evidence))
        elif unused_pkgs:
            pkgs = ", ".join(sorted({p for p, _, _ in unused_pkgs}))
            findings.append(WasteFinding(
                kind="unused_package", address=res.address, title=f"Unused package for {name}: {pkgs}",
                reason=(f"The package is installed but never imported. {name} itself is still used "
                        "another way, so it stays in the migration; only the package can go."),
                confidence="medium", exclude_from_migration=False,
                evidence=[{"file": f, "line": ln, "detail": p} for p, f, ln in unused_pkgs]))

    # Variables already explained by an unused service are not repeated on their own
    covered = {str(e.get("detail")) for f in findings if f.kind == "unused_service" for e in f.evidence}
    for var in [v for v in sorted(unused_vars) if v not in covered][:MAX_ENV_FINDINGS]:
        places = unused_vars[var]
        findings.append(WasteFinding(
            kind="unused_env_var", address=f"env:{var}", title=f"Variable {var} is never read",
            reason="Set in an environment file, but no code or configuration file uses it.",
            confidence="medium", exclude_from_migration=False,
            evidence=[{"file": f, "line": ln, "detail": var} for f, ln in places]))
    return findings


# ------------------------------------------------------------------ public API
def find_waste(analysis: Analysis, folder: str | Path) -> list[WasteFinding]:
    """Fill analysis.waste and return it."""
    root = Path(folder)
    analysis.waste = _terraform_waste(analysis, root) + _application_waste(analysis, root)
    return analysis.waste


def excluded_addresses(analysis: Analysis) -> set[str]:
    """Addresses phase 2 leaves out when the user chooses to skip suspected waste."""
    return {w.address for w in analysis.waste if w.exclude_from_migration}


def is_waste(res: Resource, analysis: Analysis) -> bool:
    return res.address in excluded_addresses(analysis)
