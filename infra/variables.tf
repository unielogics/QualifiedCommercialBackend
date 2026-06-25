variable "aws_region" {
  type        = string
  default     = "us-east-1"
  description = "AWS region. Existing VPC/EC2/RDS must be in the same region."
}

# ---------- Existing resources (read-only via data blocks) ----------

variable "existing_vpc_id" {
  type        = string
  description = "ID of the pre-provisioned VPC."
}

variable "existing_ec2_instance_id" {
  type        = string
  description = "ID of the pre-provisioned EC2 instance."
}

variable "existing_rds_identifier" {
  type        = string
  description = "Identifier of the pre-provisioned RDS instance (e.g. 'qualifiedcommercial')."
}

# ---------- Domain ----------

variable "domain_root" {
  type        = string
  default     = "qualifiedcommercial.com"
  description = "Apex domain. Used to look up the Route 53 hosted zone."
}

variable "api_subdomain" {
  type        = string
  default     = "api"
  description = "Subdomain for the backend (e.g. api.qualifiedcommercial.com)."
}

variable "route53_zone_id" {
  type        = string
  description = "Hosted zone ID for domain_root. Hardcoded so we skip route53:ListTagsForResource."
}

# ---------- GitHub Actions OIDC ----------

variable "github_org" {
  type        = string
  description = "GitHub org/user that owns the qualifiedcommercial repo."
}

variable "github_repo" {
  type        = string
  default     = "QualifiedCommercial"
  description = "GitHub repo name."
}

# ---------- Image registry ----------

variable "ghcr_image" {
  type        = string
  default     = "ghcr.io/REPLACE-ME/qcbackend"
  description = "Full GHCR image path. The deploy workflow pushes here."
}

# ---------- Secrets payload ----------
#
# These are written into AWS Secrets Manager at apply time. The EC2 instance
# pulls them on boot via the IAM role we attach. Mark sensitive so they don't
# print to stdout.

variable "secret_payload" {
  type      = map(string)
  sensitive = true
  description = <<-EOT
    Map of env-var name to value, written into Secrets Manager as a JSON blob.

    Required keys:
      DATABASE_URL          postgresql+asyncpg://<user>:<pw>@<rds-endpoint>:5432/<db>
      DATABASE_URL_SYNC     postgresql+psycopg://<user>:<pw>@<rds-endpoint>:5432/<db>
      CORS_ORIGINS          https://app.qualifiedcommercial.com
      CLERK_SECRET_KEY      sk_live_...
      CLERK_JWKS_URL        https://clerk.qualifiedcommercial.com/.well-known/jwks.json
      CLERK_ISSUER          https://clerk.qualifiedcommercial.com
      BEDROCK_ENABLED          true
      BEDROCK_REGION           us-east-1
      BEDROCK_MODEL_HEAVY      us.anthropic.claude-sonnet-4-6
      BEDROCK_MODEL_LIGHT      us.anthropic.claude-haiku-4-5-20251001-v1:0
      S3_BUCKET             qc-documents-prod
      BUCKETS_KMS_KEY_ID    arn:aws:kms:<region>:<account>:key/<key-id>
      USE_FAKE_INBOX        true
      APP_ENV               production
      LOG_LEVEL             INFO
  EOT
}
