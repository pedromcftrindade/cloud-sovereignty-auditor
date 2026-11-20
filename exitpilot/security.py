"""Security check: what the infrastructure code exposes to the whole internet.

A static check of the Terraform files: nothing is scanned on the network (scanning servers you
do not own is not allowed, and the code shows the intent before anything is deployed). It finds:

  * open_port        a firewall rule that lets anyone on the internet (0.0.0.0/0, ::/0, "*",
                     "Internet") reach a sensitive port: SSH, RDP, databases, caches, admin APIs...
                     Ports 80 and 443 (public web) are expected and not reported.
  * all_ports_open   the same, for every port (protocol -1 / "*" / 0-65535)
  * public_database  a managed database with public access switched on
  * public_bucket    object storage readable by anyone

Each finding says why it matters and how to fix it, and phase 2 (migrate.py) lists them under
"do not carry these over": moving to a new provider is the moment to close them.
These are facts read from the files, not AI output.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional

from .model import Analysis, Resource

WORLD = {"0.0.0.0/0", "::/0", "*", "internet", "any"}
SEVERITY_ORDER = {"critical": 0, "high": 1, "medium": 2}

# port -> (service, severity)
SENSITIVE_PORTS: dict[int, tuple[str, str]] = {
    22: ("SSH", "high"), 23: ("Telnet", "critical"), 21: ("FTP", "high"), 3389: ("RDP (Windows remote desktop)", "critical"),
    445: ("SMB file sharing", "critical"), 3306: ("MySQL", "critical"), 5432: ("PostgreSQL", "critical"),
    1433: ("SQL Server", "critical"), 1521: ("Oracle DB", "critical"), 27017: ("MongoDB", "critical"),
    6379: ("Redis", "critical"), 11211: ("Memcached", "critical"), 9200: ("Elasticsearch", "critical"),
    5672: ("RabbitMQ", "high"), 15672: ("RabbitMQ admin", "high"), 2375: ("Docker API (no TLS)", "critical"),
    2376: ("Docker API", "high"), 2379: ("etcd", "critical"), 6443: ("Kubernetes API", "high"),
    10250: ("Kubelet API", "critical"), 8080: ("HTTP admin / alternative", "medium"), 9090: ("Prometheus", "medium"),
    5601: ("Kibana", "high"), 3000: ("Grafana / dev server", "medium"),
}
FIX_FOR = {
    "SSH": "Allow SSH only from your office or VPN IP addresses (or use a bastion / VPN instead of a public port).",
    "RDP (Windows remote desktop)": "Never expose RDP: put it behind a VPN or a bastion host.",
}
DEFAULT_FIX = "Restrict the source to the private network or to known IP addresses; this service should not face the internet."


@dataclass
class SecurityFinding:
    rule: str                      # open_port | all_ports_open | public_database | public_bucket
    address: str                   # resource that holds the rule
    severity: str                  # critical | high | medium
    title: str
    detail: str
    fix: str
    file: str
    ports: list[int] = field(default_factory=list)


def _list(value: Any) -> list[Any]:
    if value is None:
        return []
    return value if isinstance(value, list) else [value]


def _is_world(sources: list[Any]) -> bool:
    return any(isinstance(s, str) and s.strip().lower() in WORLD for s in sources)


def _int(value: Any) -> Optional[int]:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return None


def _ports_from_range(low: Any, high: Any) -> Optional[tuple[int, int]]:
    lo, hi = _int(low), _int(high)
    if lo is None or hi is None:
        return None
    return (lo, hi)


def _parse_port_spec(spec: Any) -> Optional[tuple[int, int]]:
    """'22' -> (22, 22); '1000-2000' -> (1000, 2000); '*' -> (0, 65535)."""
    text = str(spec).strip()
    if text in ("*", ""):
        return (0, 65535)
    if "-" in text:
        a, b = text.split("-", 1)
        return _ports_from_range(a, b)
    port = _int(text)
    return None if port is None else (port, port)


def _finding_for(res: Resource, ranges: list[tuple[int, int]], all_protocols: bool) -> Optional[SecurityFinding]:
    if all_protocols or any(lo <= 0 and hi >= 65535 for lo, hi in ranges):
        return SecurityFinding(
            rule="all_ports_open", address=res.address, severity="critical",
            title="Every port is open to the whole internet",
            detail="The rule allows any protocol and any port from 0.0.0.0/0.",
            fix="Open only the ports the service needs (usually 443) and restrict the rest to the private network.",
            file=res.file)
    exposed = sorted({p for lo, hi in ranges for p in SENSITIVE_PORTS if lo <= p <= hi})
    if not exposed:
        return None                                    # e.g. only 80/443: a public website, as intended
    names = [SENSITIVE_PORTS[p][0] for p in exposed]
    severity = min((SENSITIVE_PORTS[p][1] for p in exposed), key=lambda s: SEVERITY_ORDER[s])
    fix = FIX_FOR.get(names[0], DEFAULT_FIX) if len(names) == 1 else DEFAULT_FIX
    return SecurityFinding(
        rule="open_port", address=res.address, severity=severity,
        title=f"{', '.join(names)} open to the whole internet",
        detail=f"Port(s) {', '.join(map(str, exposed))} accept connections from anyone (0.0.0.0/0).",
        fix=fix, file=res.file, ports=exposed)


def _aws_rules(res: Resource) -> list[SecurityFinding]:
    rules: list[dict[str, Any]] = []
    if res.type == "aws_security_group":
        rules = [r for r in _list(res.config.get("ingress")) if isinstance(r, dict)]
    elif res.type == "aws_security_group_rule" and str(res.config.get("type", "")).lower() == "ingress":
        rules = [res.config]
    elif res.type == "aws_vpc_security_group_ingress_rule":
        c = res.config
        rules = [{"from_port": c.get("from_port"), "to_port": c.get("to_port"), "protocol": c.get("ip_protocol"),
                  "cidr_blocks": _list(c.get("cidr_ipv4")), "ipv6_cidr_blocks": _list(c.get("cidr_ipv6"))}]
    ranges, all_protocols = [], False
    for rule in rules:
        if not _is_world(_list(rule.get("cidr_blocks")) + _list(rule.get("ipv6_cidr_blocks"))):
            continue
        if str(rule.get("protocol", "")).strip() in ("-1", "all"):
            all_protocols = True
            continue
        r = _ports_from_range(rule.get("from_port"), rule.get("to_port"))
        if r:
            ranges.append(r)
    finding = _finding_for(res, ranges, all_protocols)
    return [finding] if finding else []


def _azure_rules(res: Resource) -> list[SecurityFinding]:
    if res.type == "azurerm_network_security_group":
        rules = [r for r in _list(res.config.get("security_rule")) if isinstance(r, dict)]
    elif res.type == "azurerm_network_security_rule":
        rules = [res.config]
    else:
        return []
    ranges = []
    for rule in rules:
        if str(rule.get("direction", "")).lower() != "inbound" or str(rule.get("access", "")).lower() != "allow":
            continue
        sources = _list(rule.get("source_address_prefix")) + _list(rule.get("source_address_prefixes"))
        if not _is_world(sources):
            continue
        for spec in _list(rule.get("destination_port_range")) + _list(rule.get("destination_port_ranges")):
            r = _parse_port_spec(spec)
            if r:
                ranges.append(r)
    finding = _finding_for(res, ranges, False)
    return [finding] if finding else []


def _gcp_rules(res: Resource) -> list[SecurityFinding]:
    if res.type != "google_compute_firewall":
        return []
    c = res.config
    if str(c.get("direction", "INGRESS")).upper() != "INGRESS" or not _is_world(_list(c.get("source_ranges"))):
        return []
    ranges, all_protocols = [], False
    for allow in [a for a in _list(c.get("allow")) if isinstance(a, dict)]:
        ports = _list(allow.get("ports"))
        if str(allow.get("protocol", "")).lower() == "all" or not ports:
            all_protocols = True
            continue
        for spec in ports:
            r = _parse_port_spec(spec)
            if r:
                ranges.append(r)
    finding = _finding_for(res, ranges, all_protocols)
    return [finding] if finding else []


def _public_data(res: Resource) -> list[SecurityFinding]:
    c = res.config
    if res.type in ("aws_db_instance", "aws_rds_cluster") and c.get("publicly_accessible") is True:
        return [SecurityFinding(
            rule="public_database", address=res.address, severity="critical",
            title="Database reachable from the internet", detail="publicly_accessible = true.",
            fix="Set publicly_accessible = false and reach the database only from the private network.",
            file=res.file)]
    if res.type in ("aws_s3_bucket", "aws_s3_bucket_acl") and str(c.get("acl", "")).lower() in (
            "public-read", "public-read-write"):
        return [SecurityFinding(
            rule="public_bucket", address=res.address, severity="high",
            title="Bucket readable by anyone", detail=f"acl = {c.get('acl')}.",
            fix="Make the bucket private and share files with signed URLs or a CDN.", file=res.file)]
    if res.type == "azurerm_storage_container" and str(c.get("container_access_type", "")).lower() in ("blob", "container"):
        return [SecurityFinding(
            rule="public_bucket", address=res.address, severity="high",
            title="Storage container readable by anyone", detail=f"container_access_type = {c.get('container_access_type')}.",
            fix="Set container_access_type = \"private\" and share files with SAS tokens.", file=res.file)]
    if res.type in ("google_storage_bucket_iam_member", "google_storage_bucket_iam_binding") and any(
            m in ("allUsers", "allAuthenticatedUsers") for m in _list(c.get("member")) + _list(c.get("members"))):
        return [SecurityFinding(
            rule="public_bucket", address=res.address, severity="high",
            title="Bucket readable by anyone", detail="Granted to allUsers.",
            fix="Remove the allUsers grant and share files with signed URLs.", file=res.file)]
    return []


def find_security_issues(analysis: Analysis) -> list[SecurityFinding]:
    """Fill analysis.security and return it, worst first."""
    findings: list[SecurityFinding] = []
    for res in analysis.resources:
        if res.origin != "resource":
            continue
        for check in (_aws_rules, _azure_rules, _gcp_rules, _public_data):
            findings.extend(check(res))
    findings.sort(key=lambda f: (SEVERITY_ORDER[f.severity], f.address))
    analysis.security = findings
    return findings


def security_markdown(findings: list[SecurityFinding]) -> list[str]:
    if not findings:
        return []
    crit = sum(1 for f in findings if f.severity == "critical")
    lines = [f"### Security: {len(findings)} thing(s) exposed to the whole internet ({crit} critical)\n",
             "Read from the infrastructure code (nothing was scanned on the network). Close these before or "
             "during the migration: do not carry them over to the new provider.\n",
             "| Severity | Item | Problem | Fix | File |\n|---|---|---|---|---|"]
    for f in findings:
        lines.append(f"| {f.severity} | `{f.address}` | {f.title}. {f.detail} | {f.fix} | {f.file} |")
    lines.append("")
    return lines
