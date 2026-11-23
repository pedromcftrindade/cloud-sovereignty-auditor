import pytest

from exitpilot.classifier import classify, classify_resource
from exitpilot.model import Resource
from exitpilot.parser import parse_folder
from exitpilot.scoring import compute_score


def res(rtype, name="x", origin="resource"):
    return Resource(provider="aws", type=rtype, name=name, file="main.tf", origin=origin)


@pytest.mark.parametrize("rtype, category, level, support", [
    ("aws_instance", "compute", 0, False),
    ("aws_s3_bucket", "object_storage", 1, False),
    ("aws_db_instance", "database", 2, False),
    ("aws_lambda_function", "serverless", 3, False),
    ("aws_dynamodb_table", "proprietary_database", 3, False),
    ("aws_security_group", "firewall", 0, True),
    ("aws_iam_role_policy_attachment", "permissions", 1, True),      # prefix aws_iam_
    ("aws_route53_record", "configuration", 1, True),                 # aws_route53_ beats aws_route
    ("aws_route_table", "network", 0, True),
    ("azurerm_storage_account", "object_storage", 2, False),          # Blob is not S3-compatible
    ("google_bigquery_dataset", "analytics", 3, False),
])
def test_catalog_levels(rtype, category, level, support):
    r = res(rtype)
    assert classify_resource(r)
    assert (r.category, r.level, r.support) == (category, level, support)


def test_remote_module_is_estimated():
    r = res("module:lambda", origin="module")
    assert classify_resource(r)
    assert (r.category, r.level, r.confidence) == ("serverless", 3, "medium")


def test_unknown_type_without_ai_stays_unclassified(examples):
    a = parse_folder(examples / "casos-dificeis")
    unknown = classify(a, llm=None)
    assert [u.address for u in unknown] == ["aws_lightsail_instance.blog"]
    assert unknown[0].level is None and unknown[0].confidence == "none"


def test_ai_classifies_unknown_type(examples, fake_llm):
    llm = fake_llm({"results": [{"type": "aws_lightsail_instance", "category": "compute",
                                 "level": 1, "reason": "Simple VPS"}]})
    a = parse_folder(examples / "casos-dificeis")
    unknown = classify(a, llm=llm)
    assert unknown == []
    blog = next(r for r in a.resources if r.address == "aws_lightsail_instance.blog")
    assert (blog.level, blog.classified_by, blog.confidence) == (1, "llm", "medium")
    assert len(llm.prompts) == 1                       # one request for all unknown types


@pytest.mark.parametrize("bad_item", [
    {"type": "aws_lightsail_instance", "category": "compute", "level": 7},          # invalid level
    {"type": "aws_lightsail_instance", "category": "teleportation", "level": 1},    # invented category
    {"type": "aws_lightsail_instance", "category": "compute", "level": True},       # bool, not int
    {"type": "aws_made_up_thing", "category": "compute", "level": 0},               # type we never asked
])
def test_invalid_ai_answers_are_discarded(examples, fake_llm, bad_item):
    a = parse_folder(examples / "casos-dificeis")
    unknown = classify(a, llm=fake_llm({"results": [bad_item]}))
    assert [u.address for u in unknown] == ["aws_lightsail_instance.blog"]


def test_ai_returning_garbage_does_not_crash(examples, fake_llm):
    a = parse_folder(examples / "casos-dificeis")
    assert len(classify(a, llm=fake_llm("not json at all"))) == 1


@pytest.mark.parametrize("example, expected, counted, level_sum", [
    ("loja-simples", 66.7, 4, 4),          # 100 * (1 - 4/12)
    ("startup-serverless", 13.3, 5, 13),   # 100 * (1 - 13/15)
    ("casos-dificeis", 44.4, 3, 5),        # 100 * (1 - 5/9), lightsail excluded
    ("azure-loja", 33.3, 5, 10),           # 100 * (1 - 10/15)
])
def test_scores_of_examples(examples, example, expected, counted, level_sum):
    a = parse_folder(examples / example)
    classify(a)
    s = compute_score(a.resources)
    assert (s.value, s.counted, s.level_sum) == (expected, counted, level_sum)


def test_score_without_main_resources_is_none():
    r = res("aws_vpc")
    classify_resource(r)
    s = compute_score([r])
    assert s.value is None and s.label == "n/a"
