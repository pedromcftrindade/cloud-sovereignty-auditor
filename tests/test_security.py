"""Security check (security.py): what the infrastructure code exposes to the whole internet."""

from exitpilot.analysis import analyse_folder
from exitpilot.migrate import check_exposure, plan_migration
from exitpilot.model import Analysis
from exitpilot.parser import parse_folder
from exitpilot.security import find_security_issues


def scan(tmp_path, tf):
    (tmp_path / "main.tf").write_text(tf)
    analysis = parse_folder(tmp_path)
    return {f.address: f for f in find_security_issues(analysis)}


def test_examples(examples):
    simples = analyse_folder(examples / "loja-simples").analysis.security
    assert [(f.address, f.ports, f.severity) for f in simples] == [("aws_security_group.web", [22], "high")]
    lixo = {f.address: f for f in analyse_folder(examples / "loja-lixo").analysis.security}
    assert lixo["aws_db_instance.db"].severity == "critical"                    # publicly_accessible = true
    assert lixo["aws_security_group_rule.old_admin_ssh"].ports == [22]
    assert analyse_folder(examples / "startup-serverless").analysis.security == []


def test_public_web_ports_are_fine(tmp_path):
    found = scan(tmp_path, '''
resource "aws_security_group" "web" {
  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
  ingress {
    from_port   = 5432
    to_port     = 5432
    protocol    = "tcp"
    cidr_blocks = ["10.0.0.0/16"]
  }
}''')
    assert found == {}                                  # 443 is a website, 5432 only from the private network


def test_aws_all_ports_and_ranges(tmp_path):
    found = scan(tmp_path, '''
resource "aws_security_group" "all" {
  ingress {
    from_port   = 0
    to_port     = 0
    protocol    = "-1"
    cidr_blocks = ["0.0.0.0/0"]
  }
}
resource "aws_vpc_security_group_ingress_rule" "db" {
  security_group_id = "sg-1"
  cidr_ipv4         = "0.0.0.0/0"
  from_port         = 3000
  to_port           = 6500
  ip_protocol       = "tcp"
}''')
    assert found["aws_security_group.all"].rule == "all_ports_open"
    db = found["aws_vpc_security_group_ingress_rule.db"]
    assert db.ports == [3000, 3306, 3389, 5432, 5601, 5672, 6379, 6443] and db.severity == "critical"


def test_azure_and_gcp(tmp_path):
    found = scan(tmp_path, '''
resource "azurerm_network_security_group" "nsg" {
  security_rule {
    name                   = "rdp"
    direction              = "Inbound"
    access                 = "Allow"
    protocol               = "Tcp"
    source_address_prefix  = "Internet"
    destination_port_range = "3389"
  }
  security_rule {
    name                   = "deny"
    direction              = "Inbound"
    access                 = "Deny"
    protocol               = "Tcp"
    source_address_prefix  = "*"
    destination_port_range = "22"
  }
}
resource "google_compute_firewall" "ssh" {
  source_ranges = ["0.0.0.0/0"]
  allow {
    protocol = "tcp"
    ports    = ["22", "80"]
  }
}''')
    assert found["azurerm_network_security_group.nsg"].ports == [3389]       # the Deny rule is ignored
    assert found["azurerm_network_security_group.nsg"].severity == "critical"
    assert found["google_compute_firewall.ssh"].ports == [22]


def test_public_buckets(tmp_path):
    found = scan(tmp_path, '''
resource "aws_s3_bucket_acl" "pub" {
  bucket = "b"
  acl    = "public-read"
}
resource "azurerm_storage_container" "c" {
  name                  = "c"
  container_access_type = "blob"
}''')
    assert {f.rule for f in found.values()} == {"public_bucket"} and len(found) == 2


def test_migration_lists_findings_and_binds_ports_privately(examples):
    m = plan_migration(analyse_folder(examples / "loja-lixo"))
    assert "## Security: do not carry these over" in m.plan_markdown
    assert "aws_db_instance.db" in m.plan_markdown.split("## Security: do not carry these over")[1]
    exposure = next(c for c in m.checks if c.name == "Generated services do not face the internet")
    assert exposure.status == "passed" and m.checks[-1].name == "OpenTofu validate"   # tofu stays last
    assert '"${PRIVATE_IP:?set it in .env}:5432:5432"' in m.files["services/docker-compose.yml"]
    assert "PRIVATE_IP=" in m.files["services/.env.example"]


def test_exposure_check_catches_public_mappings():
    bad = {"services/docker-compose.yml": 'services:\n  db:\n    ports:\n      - "5432:5432"\n'}
    assert check_exposure(bad).status == "failed"
    good = {"services/docker-compose.yml": 'services:\n  db:\n    ports:\n      - "10.0.0.5:5432:5432"\n'}
    assert check_exposure(good).status == "passed"


def test_report_section_and_empty_analysis(examples):
    assert "### Security: 2 thing(s) exposed" in analyse_folder(examples / "loja-lixo").report.markdown
    assert find_security_issues(Analysis()) == []
