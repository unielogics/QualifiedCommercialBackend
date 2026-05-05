# Your hand-built setup already has the right ingress rules in place:
#
#   brokerage-backend-sg (sg-03a875c172ce720b3) — EC2 main SG
#       ✅ 80   inbound from 0.0.0.0/0   (Caddy ACME challenge + redirect)
#       ✅ 443  inbound from 0.0.0.0/0   (HTTPS)
#       ✅ 22   inbound from 0.0.0.0/0   (consider scoping to your IP later)
#
#   rds-ec2-8 (sg-064f867b2639d659d) — RDS main SG
#       ✅ 5432 inbound from sg-017d119be6fe70319 (ec2-rds-8 helper SG on EC2)
#
# So Terraform doesn't need to add any ingress — it would conflict with the
# existing rules. We only manage the Route 53 A-record here.

# api.qualifiedcommercial.com → EC2 public IP (Caddy on the box terminates TLS).
resource "aws_route53_record" "api" {
  zone_id = var.route53_zone_id
  name    = "${var.api_subdomain}.${var.domain_root}"
  type    = "A"
  ttl     = 300
  records = [data.aws_instance.backend.public_ip]
}
