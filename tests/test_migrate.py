"""Phase 2: migration plan to Leaseweb and European alternatives."""

import io
import json
import zipfile

import pytest

from exitpilot.__main__ import main as cli_main
from exitpilot.analysis import analyse_folder
from exitpilot.migrate import (Migration, check_sovereignty, check_terraform, check_tofu, migration_zip,
                               plan_migration, write_migration)
from exitpilot.parser import parse_folder


@pytest.fixture
def mixed(examples) -> Migration:
    return plan_migration(analyse_folder(examples / "loja-mista"))


def step(m: Migration, address: str):
    return next(s for s in m.steps if s.address == address)


# ------------------------------------------------------------------ projection
@pytest.mark.parametrize("example, before, after, outside_before, outside_after, total", [
    ("loja-simples", 66.7, 91.7, 4, 0, 4),
    ("startup-serverless", 13.3, 100.0, 5, 0, 5),
    ("casos-dificeis", 44.4, 100.0, 3, 0, 3),
    ("azure-loja", 33.3, 93.3, 5, 0, 5),
    ("loja-mista", 61.5, 82.1, 12, 0, 14),
])
def test_before_and_after(examples, example, before, after, outside_before, outside_after, total):
    m = plan_migration(analyse_folder(examples / example))
    assert (m.before.value, m.after.value) == (before, after)
    assert (m.exposure_before.outside_eu, m.exposure_after.outside_eu, m.exposure_after.total) == \
           (outside_before, outside_after, total)


def test_targets_of_the_mixed_example(mixed):
    assert step(mixed, "aws_instance.app").kind == "leaseweb_instance"
    assert step(mixed, "aws_db_instance.orders").target == "PostgreSQL on a Leaseweb instance"
    assert step(mixed, "aws_s3_bucket.product_images").kind == "leaseweb_managed"
    assert step(mixed, "service.sendgrid").target.startswith("Brevo")
    assert step(mixed, "service.stripe").target.startswith("Mollie")
    assert step(mixed, "service.openai").target.startswith("Mistral AI")
    assert step(mixed, "service.mistral").kind == "keep"                          # already European
    unknown = step(mixed, "service.api.parcelpartner-demo.eu")
    assert unknown.kind == "decision" and unknown.level_after is None              # never guessed
    assert mixed.exposure_after.unknown == 1


def test_waves_follow_the_effort(mixed):
    assert step(mixed, "service.google_fonts").wave == 1                          # low effort
    assert step(mixed, "aws_db_instance.orders").wave == 2                        # medium
    assert step(mixed, "service.stripe").wave == 3                                # high
    assert step(mixed, "service.mistral").wave == 0                               # nothing to do


def test_deep_changes_are_flagged_as_manual(examples):
    m = plan_migration(analyse_folder(examples / "startup-serverless"))
    dynamo = step(m, "aws_dynamodb_table.orders")
    assert dynamo.wave == 3 and any("rewritten" in a for a in dynamo.actions)
    assert "postgres" in m.files["services/docker-compose.yml"]
    assert "rabbitmq" in m.files["services/docker-compose.yml"]                    # SQS
    assert "keycloak" in m.files["services/docker-compose.yml"]                    # Cognito


def test_mysql_becomes_mariadb_and_postgres_stays_postgres(examples):
    m = plan_migration(analyse_folder(examples / "loja-simples"))                 # RDS engine = postgres
    assert "postgres:" in m.files["services/docker-compose.yml"]
    assert "mariadb" not in m.files["services/docker-compose.yml"]


# ------------------------------------------------------------------ generated files and checks
def test_generated_files_and_checks_pass(mixed):
    assert set(mixed.files) == {"MIGRATION_PLAN.md", "app.env.example", "services/.env.example",
                                "services/docker-compose.yml", "terraform/cloud-init.yaml", "terraform/main.tf"}
    status = {c.name: c.status for c in mixed.checks}
    assert status["Terraform syntax and required arguments"] == "passed"
    assert status["Generated files stay in the EU"] == "passed"
    assert status["OpenTofu validate"] == "not run"


def test_app_env_points_to_european_targets_without_secrets(mixed):
    env = mixed.files["app.env.example"]
    assert "SMTP_HOST=smtp-relay.brevo.com" in env
    assert "SENTRY_DSN=https://<key>@glitchtip.internal/1" in env
    assert "# SHIPPING_API_URL: unchanged (decide first)" in env
    for secret in ("FAKE_KEY", "fakepublickey", "sendgrid.net"):
        assert secret not in env


def test_generated_terraform_is_readable_by_exitpilot(mixed, tmp_path):
    write_migration(mixed, tmp_path / "out")
    again = analyse_folder(tmp_path / "out")                                      # round trip
    assert again.analysis.errors == []
    assert {r.type for r in again.analysis.resources if r.origin == "resource"} == {"leaseweb_public_cloud_instance"}
    assert again.score.value == 88.9                                              # 2 instances (0) + Brevo (1)
    assert (again.exposure.outside_eu, again.exposure.in_eu) == (0, 3)           # 2 instances + Brevo
    assert {s.name for s in again.services} == {"brevo"}


def test_terraform_check_catches_a_missing_argument():
    broken = 'resource "leaseweb_public_cloud_instance" "x" {\n  region = "eu-west-3"\n}\n'
    check = check_terraform({"terraform/main.tf": broken})
    assert check.status == "failed" and "contract" in check.detail and "image.id" in check.detail


def test_sovereignty_check_catches_a_non_eu_dependency():
    check = check_sovereignty({"app.env.example": "SMTP_HOST=smtp.sendgrid.net\n"})
    assert check.status == "failed" and "SendGrid" in check.detail


def test_ubuntu_package_named_docker_io_is_not_docker_hub(mixed):
    assert "docker.io" in mixed.files["terraform/cloud-init.yaml"]                # the apt package
    assert check_sovereignty({"cloud-init.yaml": mixed.files["terraform/cloud-init.yaml"]}).status == "passed"


def test_tofu_check_is_optional(tmp_path, monkeypatch):
    assert check_tofu(tmp_path, run=False).status == "not run"
    monkeypatch.setattr("exitpilot.migrate.shutil.which", lambda name: None)
    assert check_tofu(tmp_path, run=True).detail == "OpenTofu is not installed on this machine."


def test_plan_markdown_has_every_section(mixed):
    md = mixed.plan_markdown
    for title in ("## Before and after (projected)", "## Decisions needed first", "## Wave 1",
                  "## Wave 2", "## Wave 3", "## Already European (kept)", "## Generated files", "## Checks"):
        assert title in md
    assert "| Controlled outside the EU | 12 of 14 | 0 of 14 |" in md


# ------------------------------------------------------------------ writing
def test_write_refuses_a_non_empty_folder(mixed, tmp_path):
    (tmp_path / "keep.txt").write_text("mine")
    with pytest.raises(FileExistsError):
        write_migration(mixed, tmp_path)
    write_migration(mixed, tmp_path, overwrite=True)
    assert (tmp_path / "keep.txt").read_text() == "mine"                          # never deletes anything
    assert (tmp_path / "terraform" / "main.tf").exists()


def test_zip_contains_every_file(mixed):
    with zipfile.ZipFile(io.BytesIO(migration_zip(mixed))) as zf:
        names = zf.namelist()
    assert "loja-mista-migration/terraform/main.tf" in names
    assert len(names) == len(mixed.files)


def test_cli_migrate(examples, tmp_path, capsys):
    out = tmp_path / "plan"
    assert cli_main([str(examples / "loja-mista"), "--migrate", str(out)]) == 0
    printed = capsys.readouterr().out
    assert "12 of 14 -> 0 of 14" in printed
    assert (out / "MIGRATION_PLAN.md").exists()
    assert cli_main([str(examples / "loja-mista"), "--migrate", str(out)]) == 2   # not empty any more


def test_parse_folder_reads_leaseweb_resources(tmp_path, mixed):
    write_migration(mixed, tmp_path)
    analysis = parse_folder(tmp_path)
    assert {r.provider for r in analysis.resources} == {"leaseweb"}
    assert json.dumps(analysis.to_dict())                                         # serialisable
