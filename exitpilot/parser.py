"""Read Terraform files and convert them into the common representation (model.py).

Notes on python-hcl2 >= 8:
  * By default it keeps the quotes inside strings ('"aws_instance"') and adds
    '__is_block__' markers. We switch both off with SerializationOptions,
    otherwise every comparison with a type name silently fails.
  * A file with a syntax error raises an exception: we record it as a
    ParseError and keep going with the other files.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Optional

import hcl2
from hcl2.utils import SerializationOptions

from .model import PROVIDER_PREFIXES, Analysis, ParseError, Resource

HCL_OPTIONS = SerializationOptions(
    strip_string_quotes=True,   # "eu-west-1" instead of '"eu-west-1"'
    explicit_blocks=False,      # no '__is_block__' keys
    with_comments=False,
)

# References to other resources inside a config, e.g. "${aws_security_group.web.id}".
# The lookbehind skips data sources ("data.aws_ami.ubuntu"), which are lookups, not resources.
REFERENCE = re.compile(r"(?<!data\.)\b((?:aws|azurerm|google)_[a-z0-9_]+)\.([A-Za-z0-9_-]+)")
# A whole value that is just a variable, e.g. "${var.region}"
VARIABLE_ONLY = re.compile(r"^\$\{var\.([A-Za-z0-9_-]+)\}$")

IGNORED_DIRS = {".terraform", ".git", "__pycache__", "node_modules"}


def provider_of_type(resource_type: str) -> str:
    """'aws_instance' -> 'aws', 'azurerm_subnet' -> 'azure', unknown -> 'other'."""
    prefix = resource_type.split("_", 1)[0]
    return PROVIDER_PREFIXES.get(prefix, "other")


def parse_text(text: str) -> dict[str, Any]:
    """Parse HCL text into a Python dict (raises on syntax errors)."""
    return hcl2.loads(text, serialization_options=HCL_OPTIONS)


def _iter_blocks(data: dict[str, Any], kind: str):
    """Yield (label, body) for one-label blocks such as `variable "x" {}`."""
    for block in data.get(kind, []) or []:
        for label, body in block.items():
            yield label, (body if isinstance(body, dict) else {})


def _iter_resources(data: dict[str, Any]):
    """Yield (type, name, config) for every `resource "type" "name" {}` block."""
    for block in data.get("resource", []) or []:
        for rtype, named in block.items():
            for name, config in (named or {}).items():
                yield rtype, name, (config if isinstance(config, dict) else {})


def _resolve(value: Any, variables: dict[str, Any]) -> Optional[str]:
    """Return a plain string value, following a simple `${var.x}` to its default."""
    if isinstance(value, list):
        value = value[0] if value else None
    if not isinstance(value, str):
        return None
    match = VARIABLE_ONLY.match(value.strip())
    if match:
        default = variables.get(match.group(1))
        return default if isinstance(default, str) else None
    if "${" in value:          # any other expression we cannot evaluate
        return None
    return value


def module_slug(source: str) -> str:
    """Short name of a remote module from its source.

    'terraform-aws-modules/vpc/aws'                       -> 'vpc'
    'git::https://github.com/org/terraform-aws-rds.git'   -> 'rds'
    'Azure/aks/azurerm'                                    -> 'aks'
    """
    src = source.split("?")[0].rstrip("/")
    parts = [p for p in re.split(r"[/:]", src) if p]
    if len(parts) >= 3 and parts[-1] in {"aws", "azurerm", "google"}:
        return parts[-2].lower()
    last = parts[-1] if parts else src
    last = re.sub(r"\.git$", "", last)
    last = re.sub(r"^terraform-(aws|azurerm|google)-", "", last)
    return last.lower()


def module_provider(source: str) -> str:
    s = source.lower()
    if s.endswith("/aws") or "terraform-aws-" in s:
        return "aws"
    if s.endswith("/azurerm") or "terraform-azurerm-" in s or s.startswith("azure/"):
        return "azure"
    if s.endswith("/google") or "terraform-google-" in s:
        return "gcp"
    return "other"


def _is_local_module(source: str) -> bool:
    # Local modules live inside the analysed folder, so their files are read anyway.
    return source.startswith("./") or source.startswith("../")


def parse_folder(folder: str | Path) -> Analysis:
    """Read every .tf file under `folder` and return an Analysis (IR)."""
    root = Path(folder)
    analysis = Analysis()
    files = sorted(
        p for p in root.rglob("*.tf")
        if not any(part in IGNORED_DIRS for part in p.relative_to(root).parts)
    )

    parsed: list[tuple[str, dict[str, Any]]] = []
    for path in files:
        rel = path.relative_to(root).as_posix()   # always "a/b.tf", also on Windows ("a\\b.tf")
        try:
            parsed.append((rel, parse_text(path.read_text(encoding="utf-8"))))
            analysis.files_read.append(rel)
        except Exception as exc:  # noqa: BLE001 - any parser failure must not stop the analysis
            first_line = str(exc).strip().splitlines()[0] if str(exc).strip() else type(exc).__name__
            analysis.errors.append(ParseError(file=rel, message=f"{type(exc).__name__}: {first_line}"))

    # 1st pass: variables (defaults) and provider-level regions, across all files
    variables: dict[str, Any] = {}
    provider_region: dict[str, str] = {}
    for _, data in parsed:
        for name, body in _iter_blocks(data, "variable"):
            if "default" in body:
                variables[name] = body["default"]
    for _, data in parsed:
        for name, body in _iter_blocks(data, "provider"):
            region = _resolve(body.get("region"), variables)
            pid = PROVIDER_PREFIXES.get(name, "other")
            if region and pid not in provider_region:
                provider_region[pid] = region

    # 2nd pass: resources and remote modules
    for rel, data in parsed:
        for rtype, name, config in _iter_resources(data):
            provider = provider_of_type(rtype)
            own_address = f"{rtype}.{name}"
            refs = sorted({f"{t}.{n}" for t, n in REFERENCE.findall(json.dumps(config))} - {own_address})
            region = (
                _resolve(config.get("location"), variables)        # Azure / GCP buckets
                or _resolve(config.get("region"), variables)       # GCP resources
                or provider_region.get(provider)                    # AWS / GCP provider block
            )
            analysis.resources.append(Resource(
                provider=provider, type=rtype, name=name, file=rel,
                config=config, depends_on=refs, region=region,
            ))

        for name, body in _iter_blocks(data, "module"):
            source = _resolve(body.get("source"), variables) or ""
            if not source or _is_local_module(source):
                continue
            provider = module_provider(source)
            analysis.resources.append(Resource(
                provider=provider, type=f"module:{module_slug(source)}", name=name, file=rel,
                config={"source": source}, depends_on=[], region=provider_region.get(provider),
                origin="module",
            ))

    _inherit_locations(analysis)
    return analysis


LOCATION_REFERENCE = re.compile(r"^\$\{([a-z0-9_]+\.[A-Za-z0-9_-]+)\.location\}$")


def _inherit_locations(analysis: Analysis) -> None:
    """Fill missing regions.

    1. Azure idiom: `location = azurerm_resource_group.main.location` -> copy that location.
    2. Resources without any location (e.g. an Azure subnet or storage container) live where
       the resource they depend on lives -> copy the region of their first dependency.
    """
    by_address = {r.address: r for r in analysis.resources}
    for res in analysis.resources:
        loc = res.config.get("location")
        if not res.region and isinstance(loc, str):
            match = LOCATION_REFERENCE.match(loc.strip())
            if match and match.group(1) in by_address:
                res.region = by_address[match.group(1)].region
    for _ in range(3):  # a few passes cover chains such as container -> account -> resource group
        for res in analysis.resources:
            if res.region:
                continue
            for dep in res.depends_on:
                parent = by_address.get(dep)
                if parent is not None and parent.region and parent.provider == res.provider:
                    res.region = parent.region
                    break
