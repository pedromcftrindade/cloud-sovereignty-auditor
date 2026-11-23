"""Step 2c (waste.py): suspected waste, and phase 2 leaving it out."""

from exitpilot.__main__ import main as cli_main
from exitpilot.analysis import analyse_folder
from exitpilot.migrate import plan_migration
from exitpilot.services import collect_services


def waste_of(folder):
    return {w.address: w for w in analyse_folder(folder).analysis.waste}


# ------------------------------------------------------------------ detection
def test_waste_example_finds_every_planted_item(examples):
    waste = waste_of(examples / "loja-lixo")
    kinds = {addr: w.kind for addr, w in waste.items()}
    assert kinds == {
        "aws_instance.staging": "disabled_resource",       # count = var.staging_count (default 0)
        "aws_ebs_volume.old_backup": "unattached_disk",
        "aws_eip.legacy": "unattached_ip",
        "aws_security_group.old_admin": "unused_firewall",  # only its own rule refers to it
        "service.twilio": "unused_service",                 # SDK never imported + variable never read
        "service.mixpanel": "unused_service",               # SDK never imported
        "service.google_maps": "unused_service",            # variable never read
        "env:LEGACY_FEATURE_FLAG": "unused_env_var",
    }
    assert waste["aws_instance.staging"].confidence == "high"
    assert waste["env:LEGACY_FEATURE_FLAG"].exclude_from_migration is False


def test_things_in_use_are_not_waste(examples):
    waste = waste_of(examples / "loja-lixo")
    for used in ["aws_instance.web", "aws_security_group.web", "aws_ebs_volume.data",   # attachment + output
                 "aws_eip.web",                                                       # attaches itself
                 "service.stripe", "service.sentry", "service.aws_s3"]:              # imported / read
        assert used not in waste
    assert "env:SENTRY_DSN" not in waste          # read by sentry_sdk.init() on its own
    assert "env:OLD_MAPS_URL" not in waste        # already explained by service.google_maps


def test_existing_examples_keep_their_scores(examples):
    expected = {"loja-simples": 66.7, "startup-serverless": 13.3, "casos-dificeis": 44.4, "azure-loja": 33.3}
    for name, score in expected.items():
        assert analyse_folder(examples / name).score.value == score


def test_no_code_means_no_application_findings(tmp_path):
    (tmp_path / ".env.example").write_text("TWILIO_API_URL=https://api.twilio.com\nFOO=1\n")
    (tmp_path / "requirements.txt").write_text("twilio==9.0\n")
    assert analyse_folder(tmp_path).analysis.waste == []      # nothing to check against: say nothing


def test_unused_package_but_service_used_another_way(tmp_path):
    (tmp_path / "app.py").write_text("import os\nprint(os.getenv('SMTP_HOST'))\n")
    (tmp_path / "requirements.txt").write_text("sendgrid==6.11\n")
    (tmp_path / ".env.example").write_text("SMTP_HOST=smtp.sendgrid.net\n")
    waste = waste_of(tmp_path)
    assert waste["service.sendgrid"].kind == "unused_package"
    assert waste["service.sendgrid"].exclude_from_migration is False    # SendGrid itself is still used


def test_runtime_traffic_always_counts_as_use(tmp_path):
    (tmp_path / "app.py").write_text("print('hello')\n")
    (tmp_path / "requirements.txt").write_text("twilio==9.0\n")
    (tmp_path / "network_snapshot.json").write_text('[{"host": "api.twilio.com", "port": 443}]')
    finding = waste_of(tmp_path).get("service.twilio")
    assert finding is None or (finding.kind == "unused_package" and not finding.exclude_from_migration)


def test_python_import_forms(tmp_path):
    (tmp_path / "a.py").write_text("from google import genai\nimport sentry_sdk as s\n")
    (tmp_path / "requirements.txt").write_text("google-genai\nsentry-sdk\nopenai\n")
    waste = waste_of(tmp_path)
    assert "service.google_gemini" not in waste and "service.sentry" not in waste
    assert waste["service.openai"].kind == "unused_service"


def test_javascript_imports(tmp_path):
    (tmp_path / "index.js").write_text("const sg = require('@sendgrid/mail');\nimport Stripe from 'stripe';\n")
    (tmp_path / "package.json").write_text('{"dependencies": {"@sendgrid/mail": "8", "stripe": "17", "twilio": "5"}}')
    waste = waste_of(tmp_path)
    assert set(waste) == {"service.twilio"}


# ------------------------------------------------------------------ scanner fixes (services.py)
def test_file_names_in_env_values_are_not_hosts(tmp_path):
    (tmp_path / ".env.example").write_text("FLASK_APP=app.py\nENTRY=index.js\nSCRIPT=deploy.sh\n"
                                           "SMTP_HOST=smtp.sendgrid.net\n")
    names = {r.name for r in collect_services(tmp_path)[0]}
    assert names == {"sendgrid"}


def test_trailing_comments_and_emails_are_not_evidence(tmp_path):
    (tmp_path / "app.py").write_text('x = 1  # see https://stackoverflow.com/q/1\n'
                                     'contact = "billing@stripe.com"\n'
                                     'URL = "https://api.mistral.ai/v1"  # the real one\n')
    names = {r.name for r in collect_services(tmp_path)[0]}
    assert names == {"mistral"}


def test_evidence_paths_use_forward_slashes(examples):
    resources = collect_services(examples / "loja-lixo")[0]
    files = {e["file"] for r in resources for e in r.evidence}
    assert "app/requirements.txt" in files and not any("\\" in f for f in files)


# ------------------------------------------------------------------ report and phase 2
def test_report_has_waste_section_only_when_needed(examples):
    md = analyse_folder(examples / "loja-lixo").report.markdown
    assert "### Suspected waste: 8 item(s)" in md and "`aws_ebs_volume.old_backup`" in md
    assert "Suspected waste" not in analyse_folder(examples / "loja-simples").report.markdown


def test_migration_leaves_waste_out_when_asked(examples):
    result = analyse_folder(examples / "loja-lixo")
    full = plan_migration(result)
    lean = plan_migration(result, exclude_waste=True)
    dropped = {s.address for s in lean.steps if s.kind == "drop"}
    assert {"aws_ebs_volume.old_backup", "service.twilio", "service.mixpanel", "service.google_maps",
            "aws_instance.staging"} <= dropped
    assert lean.after.counted < full.after.counted                  # dropped items are gone after the plan
    assert lean.exposure_after.total < full.exposure_after.total
    assert "## Suspected waste: clean up instead of migrating" in lean.plan_markdown
    assert "TWILIO_API_URL: remove it (suspected waste" in lean.files.get("app.env.example", "")


def test_disabled_resource_is_never_migrated(examples):
    result = analyse_folder(examples / "loja-lixo")
    for exclude in (False, True):
        tf = plan_migration(result, exclude_waste=exclude).files["terraform/main.tf"]
        assert '"web"' in tf and '"staging"' not in tf             # would create a server that does not exist


def test_cli_exclude_waste(examples, tmp_path, capsys):
    out = tmp_path / "plan"
    assert cli_main([str(examples / "loja-lixo"), "--migrate", str(out), "--exclude-waste"]) == 0
    assert "left out" in (out / "MIGRATION_PLAN.md").read_text(encoding="utf-8")


def test_streamlit_app_shows_waste_and_leaves_it_out(monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("EXITPILOT_OFFLINE", "1")
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    at = AppTest.from_file("../app.py", default_timeout=60)
    at.run()
    at.selectbox[0].select("loja-lixo").run()
    at.button[0].click().run()
    assert not at.exception
    assert any("suspected waste" in i.value for i in at.info)
    assert at.checkbox(key="exclude_waste").value is True
    at.button(key="migrate").click().run()
    assert not at.exception
    assert any("Not migrated: suspected waste" in s.value for s in at.subheader)
