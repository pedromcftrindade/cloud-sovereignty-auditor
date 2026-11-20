"""Where the data physically is, and under which jurisdiction the provider falls.

This is information, not legal advice: the report always says "may be subject to".
"""

from __future__ import annotations

from typing import Optional

# region -> (country, zone) with zone in {"EU", "Europe (non-EU)", "Outside Europe"}
REGIONS: dict[str, tuple[str, str]] = {
    # AWS
    "eu-west-1": ("Ireland", "EU"), "eu-west-2": ("United Kingdom", "Europe (non-EU)"),
    "eu-west-3": ("France", "EU"), "eu-central-1": ("Germany", "EU"),
    "eu-central-2": ("Switzerland", "Europe (non-EU)"), "eu-north-1": ("Sweden", "EU"),
    "eu-south-1": ("Italy", "EU"), "eu-south-2": ("Spain", "EU"),
    "us-east-1": ("United States", "Outside Europe"), "us-east-2": ("United States", "Outside Europe"),
    "us-west-1": ("United States", "Outside Europe"), "us-west-2": ("United States", "Outside Europe"),
    # Azure
    "westeurope": ("Netherlands", "EU"), "northeurope": ("Ireland", "EU"),
    "francecentral": ("France", "EU"), "germanywestcentral": ("Germany", "EU"),
    "swedencentral": ("Sweden", "EU"), "italynorth": ("Italy", "EU"),
    "spaincentral": ("Spain", "EU"), "polandcentral": ("Poland", "EU"),
    "uksouth": ("United Kingdom", "Europe (non-EU)"), "switzerlandnorth": ("Switzerland", "Europe (non-EU)"),
    "eastus": ("United States", "Outside Europe"), "eastus2": ("United States", "Outside Europe"),
    "westus": ("United States", "Outside Europe"),
    # Google Cloud
    "europe-west1": ("Belgium", "EU"), "europe-west3": ("Germany", "EU"),
    "europe-west4": ("Netherlands", "EU"), "europe-west9": ("France", "EU"),
    "europe-west8": ("Italy", "EU"), "europe-southwest1": ("Spain", "EU"),
    "europe-north1": ("Finland", "EU"), "europe-central2": ("Poland", "EU"),
    "europe-west2": ("United Kingdom", "Europe (non-EU)"), "europe-west6": ("Switzerland", "Europe (non-EU)"),
    "us-central1": ("United States", "Outside Europe"), "us-east4": ("United States", "Outside Europe"),
}

# All three hyperscalers are headquartered in the United States.
US_PROVIDERS = {"aws", "azure", "gcp"}
# Providers headquartered in the EU: provider id -> (company, zone of its headquarters)
EU_PROVIDERS = {"leaseweb": ("Leaseweb", "EU")}      # Leaseweb is headquartered in the Netherlands


def normalise(region: Optional[str]) -> Optional[str]:
    if not region:
        return None
    return region.strip().lower().replace(" ", "")


def describe_region(region: Optional[str], provider: Optional[str] = None) -> dict[str, str]:
    key = normalise(region)
    if key is None:
        return {"region": "unknown", "country": "unknown", "zone": "Unknown"}
    if provider in EU_PROVIDERS:
        # Region names such as "eu-west-3" mean different places for different providers:
        # do not reuse the hyperscaler table for them.
        return {"region": region, "country": "see the provider's documentation", "zone": "Unknown"}
    # Google zones like "europe-west1-b" -> region "europe-west1"
    if key not in REGIONS and key.count("-") == 2 and key.rsplit("-", 1)[1].isalpha() and len(key.rsplit("-", 1)[1]) == 1:
        key = key.rsplit("-", 1)[0]
    country, zone = REGIONS.get(key, ("unknown", "Unknown"))
    return {"region": region or "unknown", "country": country, "zone": zone}


def jurisdiction_note(provider: str, zone: str) -> str:
    if provider in EU_PROVIDERS:
        company = EU_PROVIDERS[provider][0]
        return (f"Provider headquartered in the EU ({company}). Check the data centre location "
                "of this region in the provider's documentation.")
    if provider not in US_PROVIDERS:
        return "Provider jurisdiction not assessed."
    if zone == "EU":
        return ("Data is stored in the EU, but the provider is a US company and may be subject to "
                "US laws with extraterritorial reach (e.g. the CLOUD Act).")
    if zone == "Outside Europe":
        return "Data is stored outside Europe, under a US provider."
    if zone == "Europe (non-EU)":
        return "Data is stored in Europe but outside the EU, under a US provider."
    return "Region unknown: check where this data is stored."
