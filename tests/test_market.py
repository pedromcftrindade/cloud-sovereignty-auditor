"""Market study (market.py): machines to host, cheapest plans, distance, score and the AI text."""

import pytest

from exitpilot.analysis import analyse_folder
from exitpilot.market import (CITIES, _cheapest_plan, distance_km, load_providers, machines_needed,
                              market_markdown, study, ai_recommendation)


@pytest.fixture
def lixo(examples):
    return analyse_folder(examples / "loja-lixo").analysis


def test_providers_file_is_consistent():
    data = load_providers()
    labels = set(data["capability_labels"])
    for pid, p in data["providers"].items():
        assert p["zone"] == "EU" and p["plans"] and p["datacentres"] and p["source"].startswith("https://")
        assert set(p["capabilities"]) <= labels, pid
        for plan in p["plans"]:
            assert plan["vcpu"] > 0 and plan["ram_gb"] > 0 and plan["eur_month"] > 0


def test_machines_come_from_the_infrastructure(lixo):
    machines = machines_needed(lixo)
    assert [(m.source, m.vcpu, m.ram_gb) for m in machines] == [
        ("aws_instance.web", 2, 2),                                   # t3.small
        ("self-hosted services (database, cache, queues...)", 2, 8.0),  # RDS -> PostgreSQL on a machine
    ]
    assert "aws_instance.staging" not in {m.source for m in machines}   # count = 0: nothing to host


def test_cheapest_plan_that_fits():
    plans = load_providers()["providers"]["leaseweb"]["plans"]
    assert _cheapest_plan(plans, 2, 4, False)["name"] == "LSW.M3.LARGE"     # C3.LARGE has only 3 GB
    assert _cheapest_plan(plans, 2, 8, False)["name"] == "LSW.M4.LARGE"     # M3.LARGE has 7 GiB
    ovh = load_providers()["providers"]["ovhcloud"]["plans"]
    assert _cheapest_plan(ovh, 2, 4, False)["name"] == "b3-8"               # Discovery excluded by default
    assert _cheapest_plan(ovh, 2, 4, True)["name"] == "d2-4 (Discovery)"


def test_distance():
    km = distance_km(CITIES["Lisbon"], CITIES["Madrid"])
    assert 480 < km < 520                                                   # about 500 km
    assert distance_km(CITIES["Paris"], CITIES["Paris"]) == 0


def test_study_for_lisbon(lixo):
    s = study(lixo, "Lisbon")
    names = [o.name for o in s.offers]
    assert set(names) == {"Leaseweb", "OVHcloud", "Scaleway", "Hetzner", "IONOS"}
    ionos = next(o for o in s.offers if o.provider == "ionos")
    assert ionos.nearest_dc == "Logroño (Spain)" and s.best_by("proximity").provider == "ionos"
    hetzner = next(o for o in s.offers if o.provider == "hetzner")
    assert not hetzner.orderable and names[-1] == "Hetzner"                # listed, never recommended
    assert s.best.provider != "hetzner" and s.best.orderable
    assert s.best.name in s.recommendation and "cannot be ordered today: Hetzner" in s.recommendation


def test_weights_change_the_ranking(lixo):
    by_price = study(lixo, "Lisbon", {"price": 100, "proximity": 0, "coverage": 0})
    assert by_price.best.provider == by_price.best_by("price").provider
    automated = study(lixo, "Amsterdam", {"price": 0, "proximity": 0, "coverage": 0, "automation": 100})
    assert automated.best.provider == "leaseweb"                             # phase 2 generates its Terraform
    for o in automated.offers:
        assert o.score == (100.0 if o.phase2_automated else 0.0)


def test_city_changes_the_closest_provider(lixo):
    assert study(lixo, "Lisbon").best_by("proximity").provider == "ionos"      # Logroño
    berlin = study(lixo, "Berlin").best_by("proximity")
    assert berlin.provider == "ionos" and berlin.distance_km == 0               # IONOS has a Berlin data centre
    assert study(lixo, "Amsterdam").best_by("proximity").distance_km == 0       # Leaseweb / Scaleway


def test_markdown_has_sources(lixo):
    md = market_markdown(study(lixo, "Lisbon"))
    assert "## Market study: where to go (Lisbon)" in md and "https://www.leaseweb.com" in md


def test_ai_text_is_validated(lixo, fake_llm):
    s = study(lixo, "Lisbon")
    best = s.best
    good = {"pick": best.provider, "recommendation": f"{best.name} is the best fit: EUR {best.monthly_eur:g} "
                                                     f"per month and {best.distance_km} km away."}
    assert ai_recommendation(s, fake_llm(good)) and s.recommendation_source == "ai"

    for bad in [{"pick": "hetzner", "recommendation": "Hetzner is the cheapest."},          # not orderable
                {"pick": "aws", "recommendation": "AWS."},                                  # not in the list
                {"pick": best.provider, "recommendation": f"{best.name} costs EUR 1.99."},  # invented number
                {"pick": best.provider, "recommendation": "Good choice."}]:                 # does not name it
        s2 = study(lixo, "Lisbon")
        assert not ai_recommendation(s2, fake_llm(bad)) and s2.recommendation_source == "template"


def test_streamlit_market_tab(monkeypatch):
    from streamlit.testing.v1 import AppTest

    monkeypatch.setenv("EXITPILOT_OFFLINE", "1")
    monkeypatch.delenv("MISTRAL_API_KEY", raising=False)
    at = AppTest.from_file("../app.py", default_timeout=60)
    at.run()
    at.selectbox[0].select("loja-lixo").run()
    at.button[0].click().run()
    assert not at.exception
    assert any("Where to go" in s.value for s in at.subheader)
    assert any("IONOS" in m.value for m in at.metric)                    # Lisbon by default: closest is IONOS
    at.selectbox(key="mk_city").select("Amsterdam").run()
    assert not at.exception


def test_cli_market(examples, capsys):
    from exitpilot.__main__ import main as cli_main

    assert cli_main([str(examples / "loja-lixo"), "--market", "Lisbon"]) == 0
    assert "## Market study: where to go (Lisbon)" in capsys.readouterr().out
    assert cli_main([str(examples / "loja-lixo"), "--market", "Atlantis"]) == 2
