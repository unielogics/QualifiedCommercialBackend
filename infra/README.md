# infra/

Terraform that **adds** runtime resources alongside the EC2 + RDS you already own.

## What this stack creates

| Resource | Purpose |
|---|---|
| `aws_secretsmanager_secret.qcbackend` | `qcbackend/prod` — the runtime env JSON the container reads |
| `aws_iam_role.qcbackend_instance` + instance profile | Lets the EC2 read the secret + write to S3 + use SSM |
| `aws_iam_openid_connect_provider.github` + `aws_iam_role.github_deploy` | GitHub Actions deploys without static AWS keys |
| `aws_vpc_security_group_ingress_rule.*` | Opens 80/443 on the EC2 SG, opens 5432 on the RDS SG (from EC2 only) |
| `aws_route53_record.api` | A-record `api.qualifiedcommercial.com` → EC2 public IP |

## What it does NOT touch

- The existing **VPC, EC2, and RDS** — referenced via `data` blocks only. Read-only.
- Your existing security group rules, EBS volumes, RDS parameter groups — untouched.

## First-time use

```bash
# 1. Bootstrap the remote state backend (one time)
cd infra/bootstrap
terraform init
terraform apply -var aws_account_id=<your-account-id>
# Copy the `backend_block` output into ../backend.tf and uncomment.

# 2. Configure the parent stack
cd ..
cp terraform.tfvars.example terraform.tfvars
# Edit terraform.tfvars — fill in github_org and the secret_payload values.

# 3. Plan + apply
terraform init -migrate-state    # only needed first time after editing backend.tf
terraform plan -out=tfplan
terraform apply tfplan

# 4. Run the manual one-liners from the `manual_post_apply` output
terraform output -raw manual_post_apply
```

## Updating the secret

Edit `terraform.tfvars`'s `secret_payload`, then:
```bash
terraform apply
```
The `aws_secretsmanager_secret_version` resource creates a new version. The EC2 picks it up on the next container restart (run a deploy or `docker restart qcbackend`).

## Day-to-day

- Code change → push to `main` → GitHub Actions builds image → SSM rolling restart on EC2.
- Schema change → push to `main` → container start runs `alembic upgrade head` automatically.
- Secret rotation → edit `terraform.tfvars` → `terraform apply` → trigger redeploy.

## Tearing down what we added (without touching your hand-built infra)

```bash
terraform destroy
```
This removes only the resources in our state — IAM role, secret, SG ingress rules, Route 53 record, OIDC provider. The VPC / EC2 / RDS are untouched.
