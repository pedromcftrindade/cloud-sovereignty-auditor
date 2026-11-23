# Fictitious company: an online shop on AWS (Ireland) that grew for years without clean-ups.
# Several things here are unused ON PURPOSE, to show ExitPilot's waste detection (step 2c).
provider "aws" {
  region = "eu-west-1"
}

variable "staging_count" {
  description = "Staging was switched off last year"
  default     = 0
}

# ---------------------------------------------------------------- in use
resource "aws_instance" "web" {
  ami                    = "ami-0abcdef1234567890"
  instance_type          = "t3.small"
  vpc_security_group_ids = [aws_security_group.web.id]
}

resource "aws_security_group" "web" {
  name = "web"
}

resource "aws_ebs_volume" "data" {
  availability_zone = "eu-west-1a"
  size              = 50
}

resource "aws_volume_attachment" "data" {
  device_name = "/dev/sdf"
  volume_id   = aws_ebs_volume.data.id
  instance_id = aws_instance.web.id
}

resource "aws_eip" "web" {
  domain   = "vpc"
  instance = aws_instance.web.id
}

resource "aws_s3_bucket" "uploads" {
  bucket = "loja-lixo-uploads"
}

resource "aws_db_instance" "db" {
  engine              = "postgres"
  instance_class      = "db.t3.micro"
  allocated_storage   = 20
  publicly_accessible = true   # security problem on purpose: the database faces the internet
}

output "data_volume_id" {
  value = aws_ebs_volume.data.id
}

# ---------------------------------------------------------------- suspected waste
# Disabled: count is 0, so this creates nothing.
resource "aws_instance" "staging" {
  count         = var.staging_count
  ami           = "ami-0abcdef1234567890"
  instance_type = "t3.large"
}

# Old backup disk, attached to nothing.
resource "aws_ebs_volume" "old_backup" {
  availability_zone = "eu-west-1a"
  size              = 500
}

# Public IP reserved for a server that no longer exists.
resource "aws_eip" "legacy" {
  domain = "vpc"
}

# Security group from an old admin panel: only its own rule refers to it.
resource "aws_security_group" "old_admin" {
  name = "old-admin"
}

resource "aws_security_group_rule" "old_admin_ssh" {
  type              = "ingress"
  from_port         = 22
  to_port           = 22
  protocol          = "tcp"
  cidr_blocks       = ["0.0.0.0/0"]
  security_group_id = aws_security_group.old_admin.id
}
