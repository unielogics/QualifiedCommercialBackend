# Read-only references to resources you already provisioned by hand.
# Terraform will NEVER touch these.

data "aws_caller_identity" "current" {}
data "aws_region" "current" {}

# Existing EC2 — needed for its public IP (Route 53 A-record).
data "aws_instance" "backend" {
  instance_id = var.existing_ec2_instance_id
}

# Existing RDS — referenced for its endpoint in outputs / docs.
data "aws_db_instance" "postgres" {
  db_instance_identifier = var.existing_rds_identifier
}

# Route 53 zone ID is passed as a variable (var.route53_zone_id) — this avoids
# needing route53:ListTagsForResource which the data source requires.
