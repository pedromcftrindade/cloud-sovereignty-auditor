"""Common intermediate representation (IR).

Every input format (Terraform for AWS, Azure, GCP...) is converted into these
objects. The rest of the program (classifier, scoring, report, UI) only knows
about this structure, never about the original files.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from typing import Any, Optional

# Terraform type prefix -> provider id used in the IR
PROVIDER_PREFIXES = {
    "aws": "aws",
    "azurerm": "azure",
    "google": "gcp",
    "leaseweb": "leaseweb",      # target of phase 2 (migrate.py), so migrated code can be re-analysed
}

PROVIDER_NAMES = {
    "aws": "Amazon Web Services",
    "azure": "Microsoft Azure",
    "gcp": "Google Cloud",
    "leaseweb": "Leaseweb",
    "saas": "Third-party services",
    "other": "Other",
}


@dataclass
class Resource:
    """One piece of infrastructure.

    Three origins share this structure:
      * "resource": a Terraform `resource` block (parser.py)
      * "module":   a remote Terraform module, estimated from its name (parser.py)
      * "service":  a third-party service the application really uses, found in
                    config files, code, dependencies, images or network traffic
                    (services.py). type is "service" and name is the service id
                    from services.json, or the raw host when it is not known.
    """

    provider: str                      # "aws" | "azure" | "gcp" | "saas" | "other"
    type: str                          # e.g. "aws_db_instance", "module:vpc" or "service"
    name: str                          # name chosen by the user ("db") or service id ("sendgrid")
    file: str                          # file where it was declared / first seen (relative path)
    config: dict[str, Any] = field(default_factory=dict, repr=False)
    depends_on: list[str] = field(default_factory=list)   # ["aws_security_group.web", ...]
    region: Optional[str] = None       # resolved region/location, None if unknown
    origin: str = "resource"           # "resource" | "module" | "service"

    # Only for origin "service" (filled by services.py and the classifier)
    service: Optional[str] = None      # display name, e.g. "SendGrid"
    company: Optional[str] = None      # company that controls the service, e.g. "Twilio"
    company_country: Optional[str] = None   # where that company is headquartered
    company_zone: Optional[str] = None      # "EU" | "Europe (non-EU)" | "Outside Europe"
    evidence: list[dict[str, Any]] = field(default_factory=list)  # [{"file", "line", "kind", "detail"}]

    # Filled in by the classifier
    category: Optional[str] = None     # e.g. "database"
    level: Optional[int] = None        # 0..3, None = not classified
    support: bool = False              # support resources do not count for the score
    confidence: str = "high"           # "high" | "medium" | "low" | "none"
    classified_by: str = "none"        # "catalog" | "prefix" | "module" | "llm" | "none"
    note: str = ""                     # short explanation shown in the report

    @property
    def address(self) -> str:
        """Terraform-style address, e.g. 'aws_instance.web'."""
        return f"{self.type}.{self.name}"

    def to_dict(self, include_config: bool = False) -> dict[str, Any]:
        data = asdict(self)
        data["address"] = self.address
        if not include_config:
            data.pop("config", None)
        return data


@dataclass
class ParseError:
    file: str
    message: str


@dataclass
class Analysis:
    """Everything we learned from one folder of infrastructure code."""

    resources: list[Resource] = field(default_factory=list)
    errors: list[ParseError] = field(default_factory=list)
    files_read: list[str] = field(default_factory=list)

    @property
    def providers(self) -> list[str]:
        return sorted({r.provider for r in self.resources})

    def to_dict(self) -> dict[str, Any]:
        return {
            "resources": [r.to_dict() for r in self.resources],
            "errors": [asdict(e) for e in self.errors],
            "files_read": self.files_read,
        }
