"""Phase 2: plan the exit to Leaseweb and European alternatives.

For every dependency the analysis found (Terraform resources and third-party
services) this module:

  1. chooses a target, by category (TARGETS below):
       leaseweb_instance  a Leaseweb Public Cloud instance (generated in Terraform)
       leaseweb_managed   a Leaseweb managed service (object storage, CDN, load balancer)
       self_hosted        open-source software on a Leaseweb instance (Docker Compose)
       eu_saas            a provider headquartered in the EU, taken from services.json
       keep               already European: nothing to do
       decision           no safe automatic target: a person has to decide
       drop               suspected waste (waste.py): not migrated, to review and delete
  2. orders the work in waves by effort ("effort" in catalog.json);
  3. generates the files for what can be automated:
       terraform/main.tf            Leaseweb instances (provider leaseweb/leaseweb)
       terraform/cloud-init.yaml    installs Docker and starts the self-hosted services
       services/docker-compose.yml  PostgreSQL or MariaDB, Valkey, RabbitMQ, Keycloak
       services/.env.example        the passwords the services need (empty)
       app.env.example              new values for the variables the application uses
       MIGRATION_PLAN.md            the plan, step by step, and what stays manual
  4. projects the sovereignty score and the jurisdiction exposure after the plan;
  5. checks the generated files: Terraform syntax, the arguments the Leaseweb
     provider requires, and that they add no dependency controlled outside the EU.

Nothing is applied: the output is a proposal for people to review.

Facts used for the Leaseweb provider (docs of leaseweb/terraform-provider-leaseweb,
resource leaseweb_public_cloud_instance): required arguments contract
{billing_frequency, term, type}, image {id}, region, root_disk_size,
root_disk_storage_type, type; token from the LEASEWEB_TOKEN environment variable.
The provider's Public Cloud API is in beta: pin a version after checking the registry.
"""

from __future__ import annotations

import copy
import io
import json
import re
import shutil
import subprocess
import tempfile
import zipfile
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, Optional

from .classifier import load_catalog
from .model import Resource
from .parser import parse_text
from .regions import EU_PROVIDERS
from .scoring import Exposure, Score, compute_exposure, compute_score, counted_resources
from .services import collect_services, load_services
from .waste import excluded_addresses

LEASEWEB = ("Leaseweb", "EU")              # company and zone of its headquarters (Netherlands)
INTERNAL_REGISTRY = "registry.internal"    # your own mirror (e.g. Harbor) inside the private network
WAVES = {"low": 1, "medium": 2, "high": 3}
INSTANCE_CATEGORIES = {"compute", "web_app_platform"}     # each becomes one Leaseweb instance
WAVE_TITLES = {1: "Wave 1: quick wins (low effort)", 2: "Wave 2: managed services (medium effort)",
               3: "Wave 3: deep changes (high effort)"}

# Target per category. "level" is the lock-in level after the move (self-hosted open source = 0).
# "manual" only adds what the catalog's "what_changes" text does not already say.
TARGETS: dict[str, dict[str, Any]] = {
    "compute":              {"kind": "leaseweb_instance", "level": 0},
    "disk":                 {"kind": "leaseweb_instance", "level": 0,
                             "manual": "Use the root disk (or an extra volume) of the Leaseweb instance."},
    "web_app_platform":     {"kind": "leaseweb_instance", "level": 0},
    "database":             {"kind": "self_hosted", "level": 0, "compose": "sql",
                             "manual": "Use pg_dump / pg_restore (or mysqldump) in a maintenance window, and set up "
                                       "backups: ExitPilot does not automate them."},
    "proprietary_database": {"kind": "self_hosted", "level": 0, "compose": "postgres",
                             "manual": "Target data model: PostgreSQL with JSONB columns."},
    "cache":                {"kind": "self_hosted", "level": 0, "compose": "valkey"},
    "messaging":            {"kind": "self_hosted", "level": 0, "compose": "rabbitmq"},
    "identity":             {"kind": "self_hosted", "level": 0, "compose": "keycloak",
                             "manual": "Configure the realm and the application clients in Keycloak."},
    "serverless":           {"kind": "self_hosted", "level": 0,
                             "manual": "Run the containers on the Leaseweb instance (e.g. with OpenFaaS or Knative)."},
    "containers":           {"kind": "self_hosted", "level": 0},
    "kubernetes":           {"kind": "self_hosted", "level": 0,
                             "manual": "Create a conformant cluster (e.g. k3s) on Leaseweb instances."},
    "container_registry":   {"kind": "self_hosted", "level": 0,
                             "manual": "Install Harbor and mirror every image (the upstream names are in the comments "
                                       "of services/docker-compose.yml)."},
    "api_gateway":          {"kind": "self_hosted", "level": 0},
    "secrets":              {"kind": "self_hosted", "level": 0},
    "search":               {"kind": "self_hosted", "level": 0},
    "file_storage":         {"kind": "self_hosted", "level": 0},
    "workflow":             {"kind": "self_hosted", "level": 1},
    "analytics":            {"kind": "self_hosted", "level": 1},
    "ai_ml":                {"kind": "self_hosted", "level": 1},
    "observability":        {"kind": "self_hosted", "level": 0, "env_host": "glitchtip.internal"},
    "maps":                 {"kind": "self_hosted", "level": 0},
    "web_fonts":            {"kind": "self_hosted", "level": 0},
    "object_storage":       {"kind": "leaseweb_managed", "level": 1,
                             "env_value": "<your Leaseweb Object Storage endpoint>",
                             "manual": "Create the bucket in Leaseweb Object Storage and copy the objects (e.g. with rclone)."},
    "cdn":                  {"kind": "leaseweb_managed", "level": 1},
    "load_balancer":        {"kind": "leaseweb_managed", "level": 0,
                             "manual": "The provider offers leaseweb_public_cloud_load_balancer; ExitPilot does not generate it yet."},
    "dns":                  {"kind": "leaseweb_managed", "level": 0},
    "email":                {"kind": "eu_saas", "service": "brevo", "env_host": "smtp-relay.brevo.com"},
    "payments":             {"kind": "eu_saas", "service": "mollie"},
    "web_analytics":        {"kind": "eu_saas", "service": "plausible"},
    "ai_api":               {"kind": "eu_saas", "service": "mistral"},
    "sms_voice":            {"kind": "decision"},
    "cloud_api":            {"kind": "decision"},
}

COMPOSE_TEMPLATES = {
    "postgres": ("docker.io/library/postgres:16", """\
  postgres:
    # mirror of docker.io/library/postgres:16
    image: {registry}/library/postgres:16
    restart: unless-stopped
    environment:
      POSTGRES_USER: app
      POSTGRES_DB: app
      POSTGRES_PASSWORD: ${{POSTGRES_PASSWORD:?set it in .env}}
    volumes:
      - postgres-data:/var/lib/postgresql/data
    ports:
      - "${{PRIVATE_IP:?set it in .env}}:5432:5432"   # listens on the private network only
""", "postgres-data"),
    "mariadb": ("docker.io/library/mariadb:11", """\
  mariadb:
    # mirror of docker.io/library/mariadb:11
    image: {registry}/library/mariadb:11
    restart: unless-stopped
    environment:
      MARIADB_DATABASE: app
      MARIADB_USER: app
      MARIADB_PASSWORD: ${{MARIADB_PASSWORD:?set it in .env}}
      MARIADB_ROOT_PASSWORD: ${{MARIADB_ROOT_PASSWORD:?set it in .env}}
    volumes:
      - mariadb-data:/var/lib/mysql
    ports:
      - "${{PRIVATE_IP:?set it in .env}}:3306:3306"   # listens on the private network only
""", "mariadb-data"),
    "valkey": ("docker.io/valkey/valkey:8", """\
  valkey:
    # mirror of docker.io/valkey/valkey:8 (open-source fork of Redis)
    image: {registry}/valkey/valkey:8
    restart: unless-stopped
    command: ["valkey-server", "--requirepass", "${{VALKEY_PASSWORD:?set it in .env}}"]
    ports:
      - "${{PRIVATE_IP:?set it in .env}}:6379:6379"   # listens on the private network only
""", None),
    "rabbitmq": ("docker.io/library/rabbitmq:3-management", """\
  rabbitmq:
    # mirror of docker.io/library/rabbitmq:3-management
    image: {registry}/library/rabbitmq:3-management
    restart: unless-stopped
    environment:
      RABBITMQ_DEFAULT_USER: app
      RABBITMQ_DEFAULT_PASS: ${{RABBITMQ_PASSWORD:?set it in .env}}
    volumes:
      - rabbitmq-data:/var/lib/rabbitmq
    ports:
      - "${{PRIVATE_IP:?set it in .env}}:5672:5672"   # listens on the private network only
""", "rabbitmq-data"),
    "keycloak": ("quay.io/keycloak/keycloak:26.0", """\
  keycloak:
    # mirror of quay.io/keycloak/keycloak:26.0
    image: {registry}/keycloak/keycloak:26.0
    restart: unless-stopped
    command: ["start-dev"]   # development mode: use "start" with TLS in production
    environment:
      KC_BOOTSTRAP_ADMIN_USERNAME: admin
      KC_BOOTSTRAP_ADMIN_PASSWORD: ${{KEYCLOAK_ADMIN_PASSWORD:?set it in .env}}
    ports:
      - "${{PRIVATE_IP:?set it in .env}}:8080:8080"   # listens on the private network only
""", None),
}
COMPOSE_PASSWORDS = {"postgres": ["POSTGRES_PASSWORD"], "mariadb": ["MARIADB_PASSWORD", "MARIADB_ROOT_PASSWORD"],
                     "valkey": ["VALKEY_PASSWORD"], "rabbitmq": ["RABBITMQ_PASSWORD"],
                     "keycloak": ["KEYCLOAK_ADMIN_PASSWORD"]}

# What the Leaseweb provider requires for leaseweb_public_cloud_instance (see module docstring)
REQUIRED_INSTANCE_ARGS = ["contract", "image", "region", "root_disk_size", "root_disk_storage_type", "type"]
REQUIRED_CONTRACT_ARGS = ["billing_frequency", "term", "type"]
ENV_VAR_RE = re.compile(r"^[A-Z][A-Z0-9_]*$")


def _ai_actions(res: Resource) -> list[str]:
    """Steps written by the AI for this dependency (validated, always marked "[AI]")."""
    ai = res.ai
    if not ai:
        return []
    actions = [f"[AI] {note}" for note in ai.get("migration_notes", [])]
    if ai.get("code_change_hint"):
        actions.append(f"[AI] Code: {ai['code_change_hint']}")
    guess = ai.get("owner_guess")
    if guess:
        where = f" ({guess['country']})" if guess.get("country") else ""
        actions.append(f"[AI] The owner may be {guess['company']}{where}: unverified, confirm it before "
                       "adding it to services.json.")
    return actions


@dataclass
class Step:
    address: str                    # source address, e.g. "aws_db_instance.orders" or "service.stripe"
    today: str                      # what it is today, for people
    category: str
    kind: str                       # see module docstring
    target: str                     # what it becomes, for people
    level_before: Optional[int]
    level_after: Optional[int]
    company_after: Optional[str]
    zone_after: Optional[str]
    effort: str                     # "low" | "medium" | "high" | "none" | "decide"
    wave: int                       # 1..3, 0 = nothing to do
    generated: list[str] = field(default_factory=list)    # files that cover this step
    actions: list[str] = field(default_factory=list)      # manual work, in plain words
    ai_risk: Optional[str] = None                         # AI's view: "low" | "medium" | "high"


@dataclass
class Check:
    name: str
    status: str                     # "passed" | "failed" | "not run"
    detail: str


@dataclass
class Migration:
    project: str
    steps: list[Step]
    files: dict[str, str]
    before: Score
    after: Score
    exposure_before: Exposure
    exposure_after: Exposure
    checks: list[Check] = field(default_factory=list)
    waste: list = field(default_factory=list)          # WasteFinding list from the analysis (waste.py)
    waste_excluded: bool = False                       # True: suspected waste was left out of the plan
    security: list = field(default_factory=list)       # SecurityFinding list from the analysis (security.py)

    @property
    def plan_markdown(self) -> str:
        return self.files["MIGRATION_PLAN.md"]

    def steps_in_wave(self, wave: int) -> list[Step]:
        return [s for s in self.steps if s.wave == wave]


# ------------------------------------------------------------------ choosing targets
def _category(category: Optional[str]) -> dict[str, Any]:
    return load_catalog()["categories"].get(category or "", {})


def _today(res: Resource) -> str:
    if res.origin == "service":
        who = f"{res.company}, {res.company_country}" if res.company else "owner unknown"
        return f"{res.service or res.name} ({who})"
    return res.address


def _sql_flavour(res: Resource) -> str:
    text = " ".join([res.type, json.dumps(res.config, default=str)]).lower()
    return "mariadb" if ("mysql" in text or "maria" in text) else "postgres"


def _step_for(res: Resource) -> Step:
    services = load_services()["services"]
    level_before = res.level

    if res.level is None:                                   # unknown external host
        return Step(address=res.address, today=_today(res), category="unknown", kind="decision",
                    target="Identify who operates this service", level_before=None, level_after=None,
                    company_after=None, zone_after=None, effort="decide", wave=1,
                    actions=[f"Find the company behind {res.name}, then add it to services.json."])

    if (res.origin == "service" and res.company_zone == "EU") or res.provider in EU_PROVIDERS:
        return Step(address=res.address, today=_today(res), category=res.category or "", kind="keep",
                    target="Keep: already a European provider", level_before=level_before,
                    level_after=level_before, company_after=res.company, zone_after="EU", effort="none", wave=0)

    spec = TARGETS.get(res.category or "", {"kind": "decision"})
    info = _category(res.category)
    effort = info.get("effort", "medium")
    actions = [a for a in (info.get("what_changes"), spec.get("manual")) if a]
    if res.note and (res.level or 0) >= 2 and res.origin != "service":
        actions.append(res.note)

    if spec["kind"] == "decision":
        return Step(address=res.address, today=_today(res), category=res.category or "", kind="decision",
                    target=info.get("leaseweb", "No automatic target: choose one"), level_before=level_before,
                    level_after=level_before, company_after=res.company, zone_after=res.company_zone,
                    effort="decide", wave=1, actions=actions or ["Identify the specific service first."])

    if spec["kind"] == "eu_saas":
        sid = spec["service"]
        svc = services[sid]
        if res.origin == "service" and res.name == sid:      # already on the target
            return Step(address=res.address, today=_today(res), category=res.category or "", kind="keep",
                        target="Keep", level_before=level_before, level_after=level_before,
                        company_after=res.company, zone_after=res.company_zone, effort="none", wave=0)
        return Step(address=res.address, today=_today(res), category=res.category or "", kind="eu_saas",
                    target=f"{svc['name']} ({svc['company']}, {svc['country']})", level_before=level_before,
                    level_after=svc["level"], company_after=svc["company"], zone_after=svc["zone"],
                    effort=effort, wave=WAVES.get(effort, 2), actions=actions)

    target = info.get("leaseweb", "Leaseweb")
    if spec["kind"] == "leaseweb_instance":
        target = "Leaseweb Public Cloud instance"
    if spec.get("compose") == "sql":
        target = f"{'PostgreSQL' if _sql_flavour(res) == 'postgres' else 'MariaDB'} on a Leaseweb instance"
    return Step(address=res.address, today=_today(res), category=res.category or "", kind=spec["kind"],
                target=target, level_before=level_before, level_after=spec["level"],
                company_after=LEASEWEB[0], zone_after=LEASEWEB[1], effort=effort,
                wave=WAVES.get(effort, 2), actions=actions)


def _with_ai(step: Step, res: Resource) -> Step:
    """Add what the AI wrote about this dependency to the step. The target, the levels and the wave
    stay as the catalogs decided: the AI adds steps and a risk, it never changes the plan's facts."""
    if res.ai:
        step.actions += _ai_actions(res)
        step.ai_risk = res.ai.get("risk")
    return step


def _drop_step(res: Resource, reason: str, disabled: bool) -> Step:
    target = ("Not migrated: it creates nothing today" if disabled
              else "Not migrated: suspected waste (review, then delete)")
    return Step(address=res.address, today=_today(res), category=res.category or "", kind="drop",
                target=target, level_before=res.level, level_after=None, company_after=None, zone_after=None,
                effort="none", wave=0, actions=[reason])


def _dependencies(resources: list[Resource]) -> list[Resource]:
    """What the plan covers: counted resources and services, plus unknown hosts."""
    unknown_hosts = [r for r in resources if r.origin == "service" and r.level is None]
    return counted_resources(resources) + unknown_hosts


# ------------------------------------------------------------------ projection
def project(resources: list[Resource], steps: list[Step]) -> list[Resource]:
    """Copy of the resources as they would be after every step is done."""
    by_address = {s.address: s for s in steps}
    projected = []
    for res in resources:
        step = by_address.get(res.address)
        if step and step.kind == "drop":                    # left out: it is gone after the plan
            continue
        new = copy.deepcopy(res)
        if step and step.kind not in ("keep", "decision"):
            new.level = step.level_after
            new.company, new.company_zone = step.company_after, step.zone_after
        projected.append(new)
    return projected


# ------------------------------------------------------------------ generating files
def _tf_name(name: str) -> str:
    clean = re.sub(r"[^A-Za-z0-9_-]", "_", name)
    return clean if re.match(r"^[A-Za-z_]", clean) else f"r_{clean}"


def _instance_block(name: str, reference: str, comment: str, user_data: bool) -> str:
    lines = [f"# {comment}",
             f'resource "leaseweb_public_cloud_instance" "{name}" {{',
             f'  reference = "{reference}"',
             "  region    = var.region",
             "  type      = var.instance_type",
             "",
             "  image = {",
             "    id = var.image_id",
             "  }",
             "",
             "  contract = {",
             "    billing_frequency = 1",
             "    term              = 0",
             '    type              = "HOURLY"',
             "  }",
             "",
             "  root_disk_size         = var.root_disk_size",
             '  root_disk_storage_type = "CENTRAL"',
             "  has_private_network    = true"]
    if user_data:
        lines.append('  user_data              = file("${path.module}/cloud-init.yaml")')
    lines.append("}")
    return "\n".join(lines)


def _terraform(project_name: str, instances: list[Resource], with_services: bool) -> str:
    parts = [
        f"# Generated by ExitPilot (phase 2) from \"{project_name}\". Review before applying:",
        "# nothing has been created. Set your API token in the LEASEWEB_TOKEN environment variable.",
        "# The Leaseweb Public Cloud API is in beta: pin a provider version after checking the registry.",
        "terraform {",
        "  required_providers {",
        "    leaseweb = {",
        '      source = "leaseweb/leaseweb"',
        "    }",
        "  }",
        "}",
        "",
        'provider "leaseweb" {}',
        "",
        'variable "region" {',
        '  description = "Leaseweb Public Cloud region (choose one in Europe)"',
        "  type        = string",
        '  default     = "eu-west-3"',
        "}",
        "",
        'variable "instance_type" {',
        '  description = "Leaseweb instance type, e.g. lsw.m3.large or lsw.m5a.xlarge"',
        "  type        = string",
        '  default     = "lsw.m3.large"',
        "}",
        "",
        'variable "image_id" {',
        '  description = "Operating system image"',
        "  type        = string",
        '  default     = "UBUNTU_22_04_64BIT"',
        "}",
        "",
        'variable "root_disk_size" {',
        '  description = "Root disk size in GB (at least 5 for Linux)"',
        "  type        = number",
        "  default     = 50",
        "}",
        "",
    ]
    used = set()
    for res in instances:
        name = _tf_name(res.name)
        while name in used:
            name += "_x"
        used.add(name)
        size = next((res.config.get(k) for k in ("instance_type", "size", "machine_type")
                     if isinstance(res.config.get(k), str)), None)
        details = ", ".join(x for x in [f"size {size}" if size else "", f"region {res.region}" if res.region else ""] if x)
        parts += [_instance_block(name, res.name, f"Was: {res.address}" + (f" ({details})" if details else ""), False), ""]
    if with_services:
        name = "services" if "services" not in used else "selfhosted_services"
        parts += [_instance_block(name, "self-hosted services",
                                  "Self-hosted replacements (see ../services/docker-compose.yml), "
                                  "started by cloud-init.yaml", True), ""]
    return "\n".join(parts)


def _compose(needed: list[str]) -> tuple[str, str]:
    blocks, volumes, passwords = [], [], []
    for key in needed:
        _, block, volume = COMPOSE_TEMPLATES[key]
        blocks.append(block.format(registry=INTERNAL_REGISTRY))
        if volume:
            volumes.append(volume)
        passwords += COMPOSE_PASSWORDS[key]
    compose = ["# Generated by ExitPilot (phase 2): open-source replacements for managed cloud services.",
               f"# Images come from your own registry ({INTERNAL_REGISTRY}, e.g. Harbor): mirror the upstream",
               "# image named in the comment above each one before starting. Passwords come from .env.",
               "services:"]
    compose += [b.rstrip("\n") for b in blocks]
    if volumes:
        compose += ["", "volumes:"] + [f"  {v}:" for v in volumes]
    env = ["# Copy to .env next to docker-compose.yml and set strong passwords. Never commit .env.",
           "# Private network address of the services instance: the services listen only there,",
           "# never on the public internet.", "PRIVATE_IP="]
    env += [f"{p}=" for p in passwords]
    return "\n".join(compose) + "\n", "\n".join(env) + "\n"


def _cloud_init(compose: str) -> str:
    indented = "\n".join(("      " + line) if line else "" for line in compose.splitlines())
    return "\n".join([
        "#cloud-config",
        "# Generated by ExitPilot (phase 2). Installs Docker from the Ubuntu archive and starts the",
        "# self-hosted services once /opt/exitpilot/.env exists (passwords are never put in user data).",
        "package_update: true",
        "packages:",
        "  - docker.io",
        "  - docker-compose-v2",
        "write_files:",
        "  - path: /opt/exitpilot/docker-compose.yml",
        '    permissions: "0644"',
        "    content: |",
        indented,
        "runcmd:",
        "  - [systemctl, enable, --now, docker]",
        '  - [sh, -c, "cd /opt/exitpilot && if [ -f .env ]; then docker compose up -d; '
        'else echo Create /opt/exitpilot/.env first; fi"]',
        "",
    ])


def _app_env(steps: list[Step], resources: list[Resource]) -> Optional[str]:
    """New values for the environment variables where a dependency was found."""
    by_address = {r.address: r for r in resources}
    lines = ["# Generated by ExitPilot (phase 2): environment variables to change in the application.",
             "# Only variables where a dependency was found are listed. Fill in the new credentials;",
             "# the old values were never read by ExitPilot.", ""]
    found = False
    for step in steps:
        res = by_address.get(step.address)
        if res is None or res.origin != "service":
            continue
        variables = sorted({e["detail"] for e in res.evidence
                            if e["kind"] == "config" and ENV_VAR_RE.match(str(e["detail"]))})
        if not variables:
            continue
        spec = TARGETS.get(res.category or "", {})
        lines.append(f"# {step.today} -> {step.target}")
        for var in variables:
            found = True
            if step.kind == "drop":
                lines.append(f"# {var}: remove it (suspected waste, see MIGRATION_PLAN.md)")
            elif step.kind in ("keep", "decision"):
                lines.append(f"# {var}: unchanged ({'already European' if step.kind == 'keep' else 'decide first'})")
            elif spec.get("env_host") and "HOST" in var:
                lines.append(f"{var}={spec['env_host']}")
            elif spec.get("env_host") and any(k in var for k in ("DSN", "URL", "ENDPOINT")):
                key = "<key>@" if "DSN" in var else ""
                lines.append(f"{var}=https://{key}{spec['env_host']}/1" if "DSN" in var
                             else f"{var}=https://{spec['env_host']}")
            elif spec.get("env_value"):
                lines.append(f"{var}={spec['env_value']}")
            else:
                lines.append(f"# {var}: new value for {step.target}")
                lines.append(f"{var}=")
        lines.append("")
    return "\n".join(lines) if found else None


def _plan_markdown(m: Migration) -> str:
    out: list[str] = []
    add = out.append
    b, a = m.before, m.after
    eb, ea = m.exposure_before, m.exposure_after
    add(f"# Migration plan to Leaseweb: {m.project}")
    add(f"_Generated by ExitPilot on {date.today().isoformat()}. A proposal to review: nothing has been applied._\n")
    add("## Before and after (projected)\n")
    add("| | Before | After the plan |\n|---|---|---|")
    add(f"| Sovereignty score | {b.rounded if b.rounded is not None else 'n/a'}/100 | "
        f"{a.rounded if a.rounded is not None else 'n/a'}/100 |")
    add(f"| Controlled outside the EU | {eb.outside_eu} of {eb.total} | {ea.outside_eu} of {ea.total} |")
    add(f"| Unknown owner | {eb.unknown} | {ea.unknown} |\n")
    add("The \"after\" column assumes every step below is done. Self-hosting lowers lock-in but adds "
        "operations work (updates, backups, monitoring): plan who will own it.\n")
    if any(a.startswith("[AI]") for s in m.steps for a in s.actions):
        add("Lines marked **[AI]** were written by the AI model from the facts of each dependency. They are "
            "validated but not verified: review them. Targets, levels and waves come from the catalogs.\n")

    def row(s: Step) -> str:
        files = ", ".join(f"`{f}`" for f in s.generated) or "-"
        work = " ".join(s.actions) or "-"
        before = "?" if s.level_before is None else s.level_before
        after = "?" if s.level_after is None else s.level_after
        return f"| {s.today} | {s.target} | {before} -> {after} | {files} | {work} |"

    header = "| Today | Target | Level | Generated | Manual work |\n|---|---|---|---|---|"
    decisions = [s for s in m.steps if s.kind == "decision"]
    if decisions:
        add("## Decisions needed first\n")
        add(header)
        add("\n".join(row(s) for s in decisions))
        add("")
    for wave in (1, 2, 3):
        steps = [s for s in m.steps_in_wave(wave) if s.kind != "decision"]
        if steps:
            add(f"## {WAVE_TITLES[wave]}\n")
            add(header)
            add("\n".join(row(s) for s in steps))
            add("")
    kept = [s for s in m.steps if s.kind == "keep"]
    if kept:
        add("## Already European (kept)\n")
        add("\n".join(f"- {s.today}" for s in kept))
        add("")

    if m.security:
        add("## Security: do not carry these over\n")
        add("The source infrastructure exposes these to the whole internet (see the lock-in report). Do not "
            "re-create them on the new provider: apply the fix instead.\n")
        add("| Severity | Item | Problem | Fix |\n|---|---|---|---|")
        for f in m.security:
            add(f"| {f.severity} | `{f.address}` | {f.title} | {f.fix} |")
        add("")

    if m.waste:
        add("## Suspected waste: clean up instead of migrating\n")
        add("These look unused in the analysed files (see the lock-in report). Review each one with the team. "
            + ("Items marked *left out* are not translated by this plan.\n" if m.waste_excluded else
               "This plan was generated with every item included.\n"))
        add("| Item | What | Why | In this plan |\n|---|---|---|---|")
        dropped = {s.address for s in m.steps if s.kind == "drop"}
        for w in m.waste:
            status = "left out" if w.address in dropped else ("included" if w.exclude_from_migration else "clean up only")
            add(f"| `{w.address}` | {w.title} | {w.reason} | {status} |")
        add("")

    add("## Generated files\n")
    descriptions = {
        "terraform/main.tf": "Leaseweb Public Cloud instances (provider `leaseweb/leaseweb`).",
        "terraform/cloud-init.yaml": "Installs Docker on the services instance and starts the self-hosted services.",
        "services/docker-compose.yml": "Open-source replacements for managed services, from your own registry.",
        "services/.env.example": "Passwords the services need (empty: fill them in, never commit them).",
        "app.env.example": "Environment variables to change in the application.",
    }
    for path in sorted(m.files):
        if path != "MIGRATION_PLAN.md":
            add(f"- `{path}`: {descriptions.get(path, '')}")
    add("")
    add("## Checks\n")
    add("| Check | Result | Detail |\n|---|---|---|")
    for c in m.checks:
        add(f"| {c.name} | {c.status} | {c.detail} |")
    add("")
    add("## How to use it\n")
    add("1. Review this plan with the application team and decide the open points.")
    add("2. Mirror the container images into your own registry (names in `services/docker-compose.yml`).")
    add("3. `cd terraform`, pin the provider version, set `LEASEWEB_TOKEN`, then `tofu init` and `tofu plan`.")
    add("4. Create `/opt/exitpilot/.env` on the services instance from `services/.env.example`.")
    add("5. Move the data wave by wave, then update the application with `app.env.example`.")
    return "\n".join(out) + "\n"


# ------------------------------------------------------------------ checks
def check_terraform(files: dict[str, str]) -> Check:
    problems, instances = [], 0
    for path, text in files.items():
        if not path.endswith(".tf"):
            continue
        try:
            data = parse_text(text)
        except Exception as exc:  # noqa: BLE001 - any parser error is a failed check
            return Check("Terraform syntax and required arguments", "failed", f"{path}: {exc}")
        for block in data.get("resource", []) or []:
            for name, body in (block.get("leaseweb_public_cloud_instance") or {}).items():
                instances += 1
                missing = [k for k in REQUIRED_INSTANCE_ARGS if k not in body]
                contract = body.get("contract") if isinstance(body.get("contract"), dict) else {}
                missing += [f"contract.{k}" for k in REQUIRED_CONTRACT_ARGS if k not in contract]
                if not isinstance(body.get("image"), dict) or "id" not in body["image"]:
                    missing.append("image.id")
                if "user_data" in body and "ssh_key" in body:
                    problems.append(f"{name}: user_data and ssh_key cannot be used together")
                if missing:
                    problems.append(f"{name}: missing {', '.join(sorted(set(missing)))}")
    if problems:
        return Check("Terraform syntax and required arguments", "failed", "; ".join(problems))
    return Check("Terraform syntax and required arguments", "passed",
                 f"{instances} Leaseweb instance(s) parsed; every required argument is present.")


def check_sovereignty(files: dict[str, str]) -> Check:
    """Scan the generated files with step 2b: they must not add a dependency outside the EU."""
    with tempfile.TemporaryDirectory() as tmp:
        for path, text in files.items():
            target = Path(tmp) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(text, encoding="utf-8")
        resources, _, _ = collect_services(tmp)
    outside = [f"{r.service or r.name} ({r.company_zone or 'unknown owner'})"
               for r in resources if r.company_zone != "EU"]
    if outside:
        return Check("Generated files stay in the EU", "failed", "Found: " + ", ".join(outside))
    found = ", ".join(sorted(r.service for r in resources if r.service)) or "no third-party service"
    return Check("Generated files stay in the EU", "passed", f"Dependencies in the generated files: {found}.")


PORT_MAPPING_RE = re.compile(r'^\s*-\s*(?:"([^"]+)"|(\S+))')


def check_exposure(files: dict[str, str]) -> Check:
    """The generated stack must not listen on every interface (which includes the public internet)."""
    name = "Generated services do not face the internet"
    problems = []
    for path, text in files.items():
        if not path.endswith(("docker-compose.yml", "cloud-init.yaml")):
            continue
        in_ports = False
        for line in text.splitlines():
            stripped = line.strip()
            if stripped.startswith("ports:"):
                in_ports = True
                continue
            if in_ports and not stripped.startswith("-"):
                in_ports = False
            if in_ports:
                mapping = PORT_MAPPING_RE.match(line)
                spec = (mapping.group(1) or mapping.group(2)) if mapping else ""
                parts = spec.rsplit(":", 2)                   # "<host ip>:<host port>:<container port>"
                if len(parts) < 3 or parts[0] in ("0.0.0.0", "::", "[::]"):   # "5432:5432" = every interface
                    problems.append(f"{path}: {stripped}")
    if problems:
        return Check(name, "failed", "Published on every interface: " + "; ".join(problems[:3]))
    return Check(name, "passed", "Every published port is bound to the private network address (PRIVATE_IP).")


def check_tofu(out_dir: Optional[str | Path], run: bool) -> Check:
    name = "OpenTofu validate"
    if not run or out_dir is None:
        return Check(name, "not run", "Optional: needs OpenTofu and internet to download the provider "
                                      "(python -m exitpilot <folder> --migrate <out> --tofu).")
    tofu = shutil.which("tofu")
    if tofu is None:
        return Check(name, "not run", "OpenTofu is not installed on this machine.")
    tf_dir = Path(out_dir) / "terraform"
    try:
        for args in ([tofu, "init", "-backend=false", "-input=false", "-no-color"], [tofu, "validate", "-no-color"]):
            proc = subprocess.run(args, cwd=tf_dir, capture_output=True, text=True, timeout=300)
            if proc.returncode != 0:
                return Check(name, "failed", (proc.stderr or proc.stdout).strip().splitlines()[-1][:300])
    except (OSError, subprocess.TimeoutExpired) as exc:
        return Check(name, "failed", f"{type(exc).__name__}: {exc}")
    return Check(name, "passed", "tofu validate succeeded.")


# ------------------------------------------------------------------ public API
def plan_migration(result, exclude_waste: bool = False) -> Migration:
    """Build the migration plan for an analysis Result (analysis.py).

    exclude_waste=True leaves suspected waste (waste.py) out of the plan. Disabled resources
    (count = 0) are always left out: migrating them would create something that does not exist today.
    """
    analysis = result.analysis
    resources = analysis.resources
    waste = list(getattr(analysis, "waste", []) or [])
    reasons = {w.address: w for w in waste}
    drop = excluded_addresses(analysis) if exclude_waste else set()
    drop |= {w.address for w in waste if w.kind == "disabled_resource"}
    deps = _dependencies(resources)
    steps = [_drop_step(r, reasons[r.address].reason, reasons[r.address].kind == "disabled_resource")
             if r.address in drop else _with_ai(_step_for(r), r) for r in deps]
    steps.sort(key=lambda s: (s.wave if s.wave else 9, s.kind != "decision", s.today))
    by_address = {r.address: r for r in resources}

    # Which files cover which steps
    instances = [by_address[s.address] for s in steps
                 if s.kind == "leaseweb_instance" and by_address[s.address].category in INSTANCE_CATEGORIES]
    needed: list[str] = []
    for s in steps:
        key = TARGETS.get(s.category, {}).get("compose") if s.kind == "self_hosted" else None
        if key == "sql":
            key = _sql_flavour(by_address[s.address])
        if key and key not in needed:
            needed.append(key)
        if key:
            s.generated += ["services/docker-compose.yml", "terraform/cloud-init.yaml"]
    for s in steps:
        if s.kind == "leaseweb_instance" and by_address[s.address].category in INSTANCE_CATEGORIES:
            s.generated.append("terraform/main.tf")

    files: dict[str, str] = {}
    if instances or needed:
        files["terraform/main.tf"] = _terraform(result.report.project_name, instances, bool(needed))
    if needed:
        compose, env = _compose(needed)
        files["services/docker-compose.yml"] = compose
        files["services/.env.example"] = env
        files["terraform/cloud-init.yaml"] = _cloud_init(compose)
    app_env = _app_env(steps, resources)
    if app_env:
        files["app.env.example"] = app_env
        for s in steps:
            res = by_address[s.address]
            if res.origin == "service" and s.kind not in ("keep", "decision") and any(
                    e["kind"] == "config" and ENV_VAR_RE.match(str(e["detail"])) for e in res.evidence):
                s.generated.append("app.env.example")

    projected = project(resources, steps)
    migration = Migration(project=result.report.project_name, steps=steps, files=files,
                          before=compute_score(resources), after=compute_score(projected),
                          exposure_before=compute_exposure(resources), exposure_after=compute_exposure(projected),
                          waste=waste, waste_excluded=exclude_waste)
    migration.checks = [check_terraform(files), check_sovereignty(files), check_exposure(files),
                        check_tofu(None, run=False)]                 # tofu stays last (write_migration)
    migration.security = list(getattr(analysis, "security", []) or [])
    migration.files["MIGRATION_PLAN.md"] = _plan_markdown(migration)
    return migration


def write_migration(migration: Migration, out_dir: str | Path, overwrite: bool = False,
                    run_tofu: bool = False) -> Path:
    """Write the generated files. Refuses a non-empty folder unless overwrite=True."""
    out = Path(out_dir)
    if out.exists() and any(out.iterdir()) and not overwrite:
        raise FileExistsError(f"{out} is not empty (use overwrite=True / --overwrite).")
    for path, text in migration.files.items():
        target = out / path
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    if run_tofu:
        migration.checks[-1] = check_tofu(out, run=True)
        migration.files["MIGRATION_PLAN.md"] = _plan_markdown(migration)
        (out / "MIGRATION_PLAN.md").write_text(migration.files["MIGRATION_PLAN.md"], encoding="utf-8")
    return out


def migration_zip(migration: Migration) -> bytes:
    buf = io.BytesIO()
    root = f"{re.sub(r'[^A-Za-z0-9_.-]', '_', migration.project)}-migration"
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as zf:
        for path, text in sorted(migration.files.items()):
            zf.writestr(f"{root}/{path}", text)
    return buf.getvalue()
