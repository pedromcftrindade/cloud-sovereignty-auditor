# This file has a syntax error on purpose (missing closing brace).
# ExitPilot must report it and keep analysing the other files.
resource "aws_s3_bucket" "logs" {
  bucket = "company-logs"
