# Useful values + manual one-liners that need to run after `terraform apply`.

output "ec2_public_ip" {
  description = "Public IP of the existing EC2 (also where api.* now points)."
  value       = data.aws_instance.backend.public_ip
}

output "rds_endpoint" {
  description = "RDS connection endpoint."
  value       = data.aws_db_instance.postgres.endpoint
}

output "secret_arn" {
  description = "Secrets Manager ARN — referenced by the EC2 bootstrap script."
  value       = aws_secretsmanager_secret.qcbackend.arn
}

output "instance_profile_name" {
  description = "Attach this to the existing EC2 with the command in 'manual_post_apply'."
  value       = aws_iam_instance_profile.qcbackend.name
}

output "github_deploy_role_arn" {
  description = "Set as repo secret AWS_DEPLOY_ROLE_ARN in GitHub."
  value       = aws_iam_role.github_deploy.arn
}

output "api_url" {
  value       = "https://${var.api_subdomain}.${var.domain_root}"
  description = "Will resolve once Route 53 propagates + Caddy gets a cert."
}

# ---------- Manual one-liners ----------
#
# Terraform can't attach an IAM instance profile to an existing-and-running EC2
# without importing the instance (which we deliberately avoid). Run these once,
# in this order, after `terraform apply` completes.

output "manual_post_apply" {
  description = "Copy/paste these once after terraform apply."
  value = <<-EOT

    # 1. Attach the IAM instance profile to the EC2 (one-shot, idempotent)
    aws ec2 associate-iam-instance-profile \
      --region ${var.aws_region} \
      --instance-id ${var.existing_ec2_instance_id} \
      --iam-instance-profile Name=${aws_iam_instance_profile.qcbackend.name}

    # 2. Set GitHub repo secret
    gh secret set AWS_DEPLOY_ROLE_ARN \
      --repo ${var.github_org}/${var.github_repo} \
      --body "${aws_iam_role.github_deploy.arn}"

    gh variable set AWS_REGION \
      --repo ${var.github_org}/${var.github_repo} \
      --body "${var.aws_region}"

    # 3. SSH (or SSM Session Manager) into the EC2 and run the bootstrap script:
    aws ssm start-session --target ${var.existing_ec2_instance_id} --region ${var.aws_region}
    # then: curl -fsSL https://raw.githubusercontent.com/${var.github_org}/${var.github_repo}/main/deploy/ec2-bootstrap.sh | sudo bash

  EOT
}
