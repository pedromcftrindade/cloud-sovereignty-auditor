"""Step 2b: third-party services found in config, code, dependencies, images and traffic."""

import json

import pytest

from exitpilot.__main__ import main as cli_main
from exitpilot.analysis import analyse_folder
from exitpilot.classifier import classify
from exitpilot.model import Resource
from exitpilot.parser import parse_folder
from exitpilot.services import collect_services, is_internal_host, match_host, registry_of

EXPECTED_SERVICES = {"datadog", "docker_hub", "github", "google_analytics", "google_fonts",
                     "mistral", "openai", "sendgrid", "sentry", "stripe"}


def ids(resources):
    return {r.name for r in resources}


# ------------------------------------------------------------------ host matching
@pytest.mark.parametrize("host, expected", [
    ("api.stripe.com", "stripe"),
    ("s3.eu-west-1.amazonaws.com", "aws_s3"),                 # pattern beats the generic domain
    ("my-bucket.s3.amazonaws.com", "aws_s3"),
    ("sqs.eu-west-1.amazonaws.com", "aws_other"),
    ("fonts.googleapis.com", "google_fonts"),                  # longer domain beats googleapis.com
    ("photos.blob.core.windows.net", "azure_blob"),
    ("s3.fr-par.scw.cloud", "scaleway_object_storage"),
    ("http-intake.logs.datadoghq.eu", "datadog"),              # .eu site, US company
    ("API.Mistral.AI.", "mistral"),                            # case and trailing dot
])
def test_known_hosts(host, expected):
    assert match_host(host) == expected


@pytest.mark.parametrize("host", ["amazonaws-clone.eu", "notstripe.com", "sendgrid.com.evil.io", "stripe.co"])
def test_lookalikes_are_not_attributed(host):
    assert match_host(host) is None


@pytest.mark.parametrize("host, internal", [
    ("localhost", True), ("minio", True), ("db", True), ("10.0.0.12", True), ("127.0.0.1", True),
    ("192.168.1.10", True), ("host.docker.internal", True), ("api.example.com", True),
    ("payments.test", True), ("www.w3.org", True),
    ("api.stripe.com", False), ("8.8.8.8", False),
])
def test_internal_hosts(host, internal):
    assert is_internal_host(host) is internal


@pytest.mark.parametrize("image, registry", [
    ("postgres:16", "docker.io"), ("minio/minio:latest", "docker.io"),
    ("ghcr.io/acme/worker:1", "ghcr.io"), ("localhost:5000/app", "localhost"),
    ("${REGISTRY}/app:1", None), ("scratch", None),
])
def test_registry_of(image, registry):
    assert registry_of(image) == registry


# ------------------------------------------------------------------ mixed example
def test_mixed_example_finds_every_service(examples):
    resources, files, errors = collect_services(examples / "loja-mista")
    known = [r for r in resources if r.company]
    assert ids(known) == EXPECTED_SERVICES
    assert [r.name for r in resources if not r.company] == ["api.parcelpartner-demo.eu"]
    assert errors == []
    assert "main.tf" not in " ".join(files)                    # Terraform is step 2a, not scanned twice


def test_traps_are_ignored(examples):
    resources, _, _ = collect_services(examples / "loja-mista")
    hosts = {h for r in resources for h in r.config["hosts"]}
    for trap in ("minio", "db", "10.0.0.12", "www.w3.org", "docs.mistral.ai", "hooks.stripe.com"):
        assert trap not in hosts                                # internal, namespace, comment, disabled line


def test_evidence_kinds_and_runtime_only_service(examples):
    resources, _, _ = collect_services(examples / "loja-mista")
    by_id = {r.name: r for r in resources}
    assert [e["kind"] for e in by_id["datadog"].evidence] == ["observed"]   # only seen in traffic
    assert {e["kind"] for e in by_id["mistral"].evidence} == {"observed", "code", "dependency"}
    assert {e["detail"] for e in by_id["sendgrid"].evidence} == {"SMTP_HOST"}  # variable name, not value


def test_secret_values_are_never_stored(examples):
    resources, _, _ = collect_services(examples / "loja-mista")
    dumped = json.dumps([r.evidence for r in resources] + [r.config for r in resources])
    for secret in ("FAKE_KEY_0001", "FAKE_KEY_0002", "fakepublickey0000"):
        assert secret not in dumped


def test_score_and_exposure_of_mixed_example(examples):
    r = analyse_folder(examples / "loja-mista")
    # 3 Terraform resources (levels 0, 2, 1) + 10 services (levels 2,1,1,2,0,1,1,1,1,2) -> N = 15, R = 13
    assert (r.score.value, r.score.counted, r.score.level_sum) == (61.5, 13, 15)
    assert (r.exposure.total, r.exposure.outside_eu, r.exposure.in_eu, r.exposure.unknown) == (14, 12, 1, 1)
    assert r.exposure.companies["Amazon Web Services"] == 3


def test_report_has_services_and_unverified_host(examples):
    md = analyse_folder(examples / "loja-mista").report.markdown
    assert "### Third-party services the application uses" in md
    assert "12 of 14 dependencies are controlled" in md
    assert "api.parcelpartner-demo.eu (not verified)" in md
    assert "External host `api.parcelpartner-demo.eu` is not in the services catalog" in md
    assert "| Ireland | EU |" in md                              # Terraform regions still reported


def test_terraform_only_examples_are_unchanged(examples):
    r = analyse_folder(examples / "loja-simples")
    assert r.services == []
    assert r.score.value == 66.7
    assert r.exposure.outside_eu == r.exposure.total == 4      # all on AWS


# ------------------------------------------------------------------ edge cases
def test_comment_lines_and_python_annotations(tmp_path):
    (tmp_path / "models.py").write_text("# see https://api.stripe.com/docs\nimage: str\n")
    resources, _, _ = collect_services(tmp_path)
    assert resources == []


def test_dockerfile_stages_are_not_images(tmp_path):
    (tmp_path / "Dockerfile").write_text("FROM python:3.12 AS build\nRUN true\nFROM build\n")
    resources, _, _ = collect_services(tmp_path)
    assert [(r.name, [e["line"] for e in r.evidence]) for r in resources] == [("docker_hub", [1])]


def test_package_json_metadata_urls_are_ignored(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({
        "name": "shop", "repository": "https://github.com/acme/shop",
        "dependencies": {"stripe": "^15.0.0", "@sentry/node": "^8.0.0", "express": "^4.0.0"}}))
    resources, _, _ = collect_services(tmp_path)
    assert ids(resources) == {"stripe", "sentry"}


def test_broken_package_json_is_reported(tmp_path):
    (tmp_path / "package.json").write_text("{not json")
    _, _, errors = collect_services(tmp_path)
    assert [e.file for e in errors] == ["package.json"]


def test_virtualenvs_and_node_modules_are_skipped(tmp_path):
    for folder in (".venv-wsl/lib", "node_modules/x", "venv/lib"):
        (tmp_path / folder).mkdir(parents=True)
        (tmp_path / folder / "client.py").write_text('URL = "https://api.stripe.com"\n')
    resources, files, _ = collect_services(tmp_path)
    assert resources == [] and files == []


def test_unknown_hosts_are_never_sent_to_the_llm(examples, fake_llm):
    analysis = parse_folder(examples / "casos-dificeis")
    analysis.resources.append(Resource(provider="saas", type="service", name="api.mystery-vendor.eu",
                                       file=".env", origin="service"))
    llm = fake_llm({"results": []})
    unknown = classify(analysis, llm=llm)
    assert len(llm.prompts) == 1 and "mystery-vendor" not in llm.prompts[0]
    assert "api.mystery-vendor.eu" in {u.name for u in unknown}


# ------------------------------------------------------------------ command line
def test_cli_json_summary(examples, capsys):
    assert cli_main([str(examples / "loja-mista"), "--json"]) == 0
    data = json.loads(capsys.readouterr().out)
    assert data["score"] == 61.5
    assert {s["id"] for s in data["services"]} == EXPECTED_SERVICES
    assert data["unknown_external_hosts"] == ["api.parcelpartner-demo.eu"]


def test_cli_rejects_missing_folder(tmp_path):
    assert cli_main([str(tmp_path / "nope")]) == 2
