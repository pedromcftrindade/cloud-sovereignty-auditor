# Fictitious company: a small online shop on AWS (Ireland).
variable "region" {
  description = "AWS region"
  default     = "eu-west-1"
}

variable "db_password" {
  description = "Database password (never commit real secrets)"
  sensitive   = true
}
