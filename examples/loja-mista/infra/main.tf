# Fictitious company: a small online shop. The infrastructure is declared in Terraform on
# AWS (Ireland); the application runs in containers and talks to third-party services.
# Used to show step 2a (Terraform) and step 2b (services) together.
terraform {
  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.0"
    }
  }
}

provider "aws" {
  region = "eu-west-1"
}

resource "aws_security_group" "web" {
  name = "shop-web"

  ingress {
    from_port   = 443
    to_port     = 443
    protocol    = "tcp"
    cidr_blocks = ["0.0.0.0/0"]
  }
}

resource "aws_instance" "app" {
  ami                    = "ami-0123456789abcdef0"
  instance_type          = "t3.medium"
  vpc_security_group_ids = [aws_security_group.web.id]
}

resource "aws_db_instance" "orders" {
  engine                      = "postgres"
  engine_version              = "16"
  instance_class              = "db.t3.micro"
  allocated_storage           = 20
  username                    = "shop"
  manage_master_user_password = true
  skip_final_snapshot         = true
}

resource "aws_s3_bucket" "product_images" {
  bucket = "fictional-shop-product-images"
}
