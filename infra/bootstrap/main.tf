# Run this ONCE, before the parent infra/, to create the S3 + DynamoDB
# backend that the parent stack uses for state. This sub-folder uses local
# state — that's intentional, you only run it once.
#
# Usage:
#   cd infra/bootstrap
#   terraform init
#   terraform apply -var aws_account_id=<your-account-id>

terraform {
  required_version = ">= 1.6.0"
  required_providers {
    aws = { source = "hashicorp/aws", version = "~> 5.70" }
  }
}

provider "aws" {
  region = "us-east-1"
}

variable "aws_account_id" {
  type        = string
  description = "Your 12-digit AWS account ID. Used in the bucket name to make it globally unique."
}

resource "aws_s3_bucket" "tf_state" {
  bucket = "qc-terraform-state-${var.aws_account_id}"
}

resource "aws_s3_bucket_versioning" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id
  versioning_configuration { status = "Enabled" }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "tf_state" {
  bucket = aws_s3_bucket.tf_state.id
  rule {
    apply_server_side_encryption_by_default { sse_algorithm = "AES256" }
  }
}

resource "aws_s3_bucket_public_access_block" "tf_state" {
  bucket                  = aws_s3_bucket.tf_state.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_dynamodb_table" "tf_locks" {
  name         = "qc-terraform-locks"
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "LockID"
  attribute {
    name = "LockID"
    type = "S"
  }
}

output "backend_block" {
  description = "Paste this into ../backend.tf and run `terraform init -migrate-state`."
  value       = <<-EOT
    terraform {
      backend "s3" {
        bucket         = "${aws_s3_bucket.tf_state.bucket}"
        key            = "qcbackend/terraform.tfstate"
        region         = "us-east-1"
        dynamodb_table = "${aws_dynamodb_table.tf_locks.name}"
        encrypt        = true
      }
    }
  EOT
}
