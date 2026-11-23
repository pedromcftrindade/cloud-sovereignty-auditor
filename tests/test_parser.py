from exitpilot.parser import module_provider, module_slug, parse_folder, parse_text


def test_strings_come_without_quotes():
    data = parse_text('resource "aws_instance" "web" { ami = "ami-1" }')
    block = data["resource"][0]
    assert "aws_instance" in block                       # not '"aws_instance"'
    assert block["aws_instance"]["web"]["ami"] == "ami-1"
    assert "__is_block__" not in block["aws_instance"]["web"]


def test_simple_shop_resources_and_dependencies(examples):
    a = parse_folder(examples / "loja-simples")
    assert a.errors == []
    assert len(a.resources) == 13
    web = next(r for r in a.resources if r.address == "aws_instance.web")
    assert set(web.depends_on) == {"aws_security_group.web", "aws_subnet.public_a"}
    assert web.provider == "aws"
    assert web.region == "eu-west-1"                     # resolved from var.region's default


def test_broken_file_is_reported_and_others_still_read(examples):
    a = parse_folder(examples / "casos-dificeis")
    assert [e.file for e in a.errors] == ["broken.tf"]
    assert "main.tf" in a.files_read
    assert "modules/cache/main.tf" in a.files_read


def test_modules_data_sources_and_local_modules(examples):
    a = parse_folder(examples / "casos-dificeis")
    addresses = {r.address for r in a.resources}
    assert "module:vpc.vpc" in addresses                  # remote module kept
    assert "module:lambda.image_resizer" in addresses
    assert not any(r.name == "cache" and r.origin == "module" for r in a.resources)  # local: read directly
    assert "aws_elasticache_cluster.sessions" in addresses
    assert not any("aws_ami" in r.type for r in a.resources)                       # data block ignored
    worker = next(r for r in a.resources if r.address == "aws_instance.worker")
    assert worker.depends_on == []                        # data.aws_ami.* is not a dependency
    assert worker.region == "eu-central-1"


def test_azure_location_is_inherited(examples):
    a = parse_folder(examples / "azure-loja")
    regions = {r.address: r.region for r in a.resources}
    assert regions["azurerm_linux_virtual_machine.web"] == "westeurope"   # via resource group
    assert regions["azurerm_subnet.web"] == "westeurope"                  # via its vnet
    assert regions["azurerm_storage_container.images"] == "westeurope"    # via storage account
    assert {r.provider for r in a.resources} == {"azure"}


def test_module_names_and_providers():
    assert module_slug("terraform-aws-modules/vpc/aws") == "vpc"
    assert module_slug("git::https://github.com/acme/terraform-aws-rds.git?ref=v1") == "rds"
    assert module_slug("Azure/aks/azurerm") == "aks"
    assert module_provider("terraform-aws-modules/vpc/aws") == "aws"
    assert module_provider("Azure/aks/azurerm") == "azure"
    assert module_provider("terraform-google-modules/kubernetes-engine/google") == "gcp"
