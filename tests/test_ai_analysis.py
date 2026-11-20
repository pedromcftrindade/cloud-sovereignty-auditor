"""The AI analyses every dependency and returns structured data that the rest of the program uses."""

import json

import pytest

from exitpilot.__main__ import main as cli_main
from exitpilot.ai_analysis import (BATCH_SIZE, MAX_NOTES, MAX_WHY, clean_item, dependencies_to_analyse, enrich,
                                   facts_for)
from exitpilot.analysis import analyse_folder
from exitpilot.export import build_export
from exitpilot.migrate import plan_migration
from exitpilot.model import Resource

GOOD = {"risk": "high", "why_it_matters": "Payments depend on a US company.",
        "migration_notes": ["Open an account with the new provider", "Replace the client"],
        "code_change_hint": "Replace import stripe.", "confidence": "medium"}


def answer_for(prompt, overrides=None):
    """A model that analyses every dependency named in the prompt."""
    overrides = overrides or {}
    items = []
    for line in prompt.splitlines():
        if line.strip().startswith('"address": "'):
            address = line.split('"address": "', 1)[1].split('"', 1)[0]
            item = {"address": address, **GOOD, **overrides.get(address, {})}
            items.append(item)
    return {"items": items}


@pytest.fixture
def ai_llm(fake_llm):
    def make(overrides=None):
        return fake_llm(lambda p: {"results": []} if "Types to classify" in p
                        else (answer_for(p, overrides) if "DEPENDENCIES:" in p else {"executive_summary": ""}))
    return make


# ------------------------------------------------------------------ what the model sees
def test_facts_never_contain_configuration_values(examples):
    res = Resource(provider="aws", type="aws_db_instance", name="db", file="main.tf", region="eu-west-1",
                   config={"password": "hunter2-SECRET", "engine": "postgres", "instance_class": "db.t3.micro",
                           "username": "admin", "engine_version": "${var.v}"})
    res.category, res.level = "database", 2
    sent = json.dumps(facts_for(res))
    assert "hunter2" not in sent and "admin" not in sent
    assert '"engine": "postgres"' in sent and "db.t3.micro" in sent
    assert "engine_version" not in sent                                   # unresolved expressions are skipped


def test_prompt_sent_to_the_model_has_no_secret_values(examples, ai_llm):
    llm = ai_llm()
    analyse_folder(examples / "loja-mista", llm=llm)
    prompts = "\n".join(p for p in llm.prompts if "DEPENDENCIES:" in p)
    assert prompts
    for secret in ("FAKE_KEY_0001", "FAKE_KEY_0002", "fakepublickey0000", "manage_master_user_password"):
        assert secret not in prompts


def test_dependencies_are_sent_in_batches(examples, ai_llm):
    llm = ai_llm()
    result = analyse_folder(examples / "loja-mista", llm=llm)
    n = len(dependencies_to_analyse(result.analysis))
    batches = [p for p in llm.prompts if "DEPENDENCIES:" in p]
    assert n == 14 and len(batches) == -(-n // BATCH_SIZE)
    assert all("security_group" not in p for p in batches)                # support configuration is skipped


# ------------------------------------------------------------------ what is accepted
def test_every_dependency_gets_a_validated_ai_block(examples, ai_llm):
    result = analyse_folder(examples / "loja-mista", llm=ai_llm())
    analysed = [r for r in result.analysis.resources if r.ai]
    assert len(analysed) == 14 and result.ai["dependencies_analysed"] == 14
    block = analysed[0].ai
    assert block["source"] == "ai" and block["provider"] == "AI" and block["model"] == "fake-model"
    assert block["risk"] == "high" and block["confidence"] == "medium" and len(block["migration_notes"]) == 2


def test_validation_drops_what_is_not_acceptable():
    res = Resource(provider="saas", type="service", name="stripe", file=".env", origin="service")
    res.level = 2
    assert clean_item("not a dict", res) is None
    assert clean_item({"risk": "high"}, res) is None                                      # nothing useful
    trimmed = clean_item({"why_it_matters": "x" * 1000, "migration_notes": ["n"] * 10 + [""], "risk": "catastrophic",
                          "confidence": "sure", "owner_guess": {"company": "Evil"}}, res)
    assert len(trimmed["why_it_matters"]) == MAX_WHY and len(trimmed["migration_notes"]) == MAX_NOTES
    assert trimmed["risk"] is None and trimmed["confidence"] == "low"
    assert "owner_guess" not in trimmed                                                   # only for unknown hosts


def test_invented_and_repeated_addresses_are_ignored(examples, fake_llm):
    def answer(prompt):
        if "DEPENDENCIES:" not in prompt:
            return {"results": []}
        good = answer_for(prompt)["items"]
        return {"items": good + [{"address": "aws_quantum_computer.q", **GOOD},
                                 {**good[0], "why_it_matters": "second answer for the same address"}]}

    result = analyse_folder(examples / "loja-mista", llm=fake_llm(answer))
    assert not any(r.address == "aws_quantum_computer.q" for r in result.analysis.resources)
    first = dependencies_to_analyse(result.analysis)[0]
    assert first.ai and "second answer" not in first.ai["why_it_matters"]                 # the first one wins


def test_garbage_from_the_model_does_not_crash(examples, fake_llm):
    for garbage in ("not json at all", {"items": "nope"}, {"items": [1, None, {"address": 5}]}, None):
        result = analyse_folder(examples / "loja-mista", llm=fake_llm(garbage))
        assert not any(r.ai for r in result.analysis.resources)
        assert result.score.value == 61.5


def test_without_a_model_nothing_is_analysed(examples):
    assert enrich(analyse_folder(examples / "loja-mista").analysis, None) == 0
    assert analyse_folder(examples / "loja-mista").ai is None


def test_ai_analysis_can_be_switched_off(examples, ai_llm):
    llm = ai_llm()
    result = analyse_folder(examples / "loja-mista", llm=llm, ai_analysis=False)
    assert not any(r.ai for r in result.analysis.resources)
    assert not any("DEPENDENCIES:" in p for p in llm.prompts)


# ------------------------------------------------------------------ facts stay facts
def test_ai_never_changes_the_numbers(examples, ai_llm):
    plain = analyse_folder(examples / "loja-mista")
    with_ai = analyse_folder(examples / "loja-mista", llm=ai_llm())
    assert (with_ai.score.value, with_ai.exposure.outside_eu, with_ai.exposure.unknown) == \
           (plain.score.value, plain.exposure.outside_eu, plain.exposure.unknown)
    for a, b in zip(plain.analysis.resources, with_ai.analysis.resources):
        assert (a.level, a.category, a.company, a.company_zone) == (b.level, b.category, b.company, b.company_zone)


def test_owner_guess_is_a_suggestion_not_a_fact(examples, ai_llm):
    llm = ai_llm({"service.api.parcelpartner-demo.eu": {
        "owner_guess": {"company": "Parcel Partner BV", "country": "Netherlands"}}})
    result = analyse_folder(examples / "loja-mista", llm=llm)
    unknown = next(r for r in result.analysis.resources if r.name == "api.parcelpartner-demo.eu")
    assert unknown.ai["owner_guess"] == {"company": "Parcel Partner BV", "country": "Netherlands", "verified": False}
    assert unknown.level is None and unknown.company is None                              # still unknown
    assert result.exposure.unknown == 1                                                   # and not counted as EU
    assert "The AI suggests it may be operated by Parcel Partner BV (Netherlands): unverified" in result.report.markdown


# ------------------------------------------------------------------ used downstream
def test_report_has_the_ai_section_and_labels_it(examples, ai_llm):
    md = analyse_folder(examples / "loja-mista", llm=ai_llm()).report.markdown
    assert "### AI analysis of the main dependencies" in md
    assert "not verified by ExitPilot" in md and "Why it matters: Payments depend on a US company." in md
    plain = analyse_folder(examples / "loja-mista").report.markdown
    assert "### AI analysis" not in plain


def test_migration_plan_uses_the_ai_steps_but_keeps_the_targets(examples, ai_llm):
    result = analyse_folder(examples / "loja-mista", llm=ai_llm())
    plan = plan_migration(result)
    base = plan_migration(analyse_folder(examples / "loja-mista"))
    stripe = next(s for s in plan.steps if s.address == "service.stripe")
    assert "[AI] Replace the client" in stripe.actions and "[AI] Code: Replace import stripe." in stripe.actions
    assert stripe.ai_risk == "high"
    assert [(s.address, s.target, s.wave, s.level_after) for s in plan.steps] == \
           [(s.address, s.target, s.wave, s.level_after) for s in base.steps]
    assert "Lines marked **[AI]**" in plan.plan_markdown and "Lines marked **[AI]**" not in base.plan_markdown
    assert (plan.after.value, plan.exposure_after.outside_eu) == (base.after.value, base.exposure_after.outside_eu)


# ------------------------------------------------------------------ analysis.json
def test_export_is_complete_and_serialisable(examples, ai_llm):
    result = analyse_folder(examples / "loja-mista", llm=ai_llm())
    data = json.loads(json.dumps(build_export(result, plan_migration(result))))
    assert data["version"] == 1 and data["project"] == "loja-mista"
    assert data["ai"]["dependencies_analysed"] == 14
    assert data["score"]["value"] == 61.5 and data["exposure"]["controlled_outside_eu"] == 12
    stripe = next(r for r in data["resources"] if r["name"] == "stripe")
    assert stripe["company"] == "Stripe" and stripe["classified_by"] == "services"
    assert stripe["ai"]["source"] == "ai" and stripe["ai"]["risk"] == "high"
    assert next(r for r in data["resources"] if r["type"] == "aws_security_group")["ai"] is None
    assert data["report"]["markdown"].startswith("# Lock-in report") and data["report"]["executive_summary"]
    assert data["migration"]["after"]["exposure"]["controlled_outside_eu"] == 0
    assert any(s["address"] == "service.stripe" for s in data["migration"]["steps"])
    assert "terraform/main.tf" in data["migration"]["files"]


def test_export_without_ai_or_migration(examples):
    data = build_export(analyse_folder(examples / "loja-simples"))
    assert data["ai"] is None and data["migration"] is None
    assert all(r["ai"] is None for r in data["resources"])


def test_cli_out_writes_the_complete_file(examples, tmp_path, capsys):
    out = tmp_path / "deep" / "analysis.json"
    assert cli_main([str(examples / "loja-mista"), "--out", str(out)]) == 0
    assert "no AI used" in capsys.readouterr().out
    data = json.loads(out.read_text())
    assert data["migration"]["after"]["score"]["value"] == 82.1 and data["ai"] is None
