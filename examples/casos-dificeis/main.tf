# Edge cases on purpose: remote modules, a local module, count with a variable,
# a data block, a type that is not in the catalog, and (in broken.tf) a syntax error.
variable "region" {
  default = "eu-central-1"
}

variable "n_workers" {
  default = 3
}

provider "aws" {
  region = var.region
}

data "aws_ami" "ubuntu" {
  most_recent = true
  owners      = ["099720109477"]
}

module "vpc" {
  source = "terraform-aws-modules/vpc/aws"
  name   = "main"
  cidr   = "10.0.0.0/16"
}

module "image_resizer" {
  source        = "terraform-aws-modules/lambda/aws"
  function_name = "image-resizer"
  handler       = "index.handler"
  runtime       = "nodejs20.x"
}

# Local module: its files are inside this folder, so they are analysed directly.
module "cache" {
  source = "./modules/cache"
}

resource "aws_instance" "worker" {
  count         = var.n_workers
  ami           = data.aws_ami.ubuntu.id
  instance_type = "t3.medium"
  subnet_id     = module.vpc.private_subnets[0]
}

# Not in the catalog on purpose: the AI (if available) must classify it.
resource "aws_lightsail_instance" "blog" {
  name              = "company-blog"
  availability_zone = "eu-central-1a"
  blueprint_id      = "wordpress"
  bundle_id         = "nano_3_0"
}
