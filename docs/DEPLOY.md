# Deploy Runbook

Reflects what's actually live in AWS / GitHub.

## Architecture

| Surface | Stack |
|---|---|
| Backend | EC2 t4g.medium (Ubuntu 26.04 ARM) → Docker container → Caddy reverse proxy (auto-TLS via Let's Encrypt) |
| Database | RDS PostgreSQL 18.3 (db.t4g.medium, single-AZ) with `pgvector` extension |
| Image registry | GHCR (`ghcr.io/unielogics/qcbackend`) |
| Secrets | AWS Secrets Manager (`qcbackend/prod`) → `/etc/qcbackend.env` on the EC2 |
| Auth | Clerk production instance (`clerk.qualifiedcommercial.com`) |
| Backend CI/CD | GitHub Actions in `unielogics/QualifiedCommercialBackend` → OIDC into AWS → SSM rolling restart |
| Desktop | AWS Amplify Hosting → `unielogics/QCDashboard` → SSR Next.js |
| Mobile | EAS (Expo Application Services) → `unielogics/QCMobile` |

## DNS — `qualifiedcommercial.com` (Route 53 zone `Z084364029QABG83X8LV3`)

| Subdomain | Type | Target | Source |
|---|---|---|---|
| `api.qualifiedcommercial.com` | A | `54.157.222.116` (EC2) | Terraform `aws_route53_record.api` |
| `app.qualifiedcommercial.com` | CNAME | Amplify-issued (TBD on first connect) | Manual (after Amplify gives the value) |
| `clerk.qualifiedcommercial.com` | CNAME | `*.clerk.services` (Clerk-issued) | Manual (Clerk prod setup) |

## Live AWS resources (current state)

| | Resource | ID |
|---|---|---|
| ✅ | VPC | `vpc-01987e73d0b8b590c` (10.0.0.0/16) |
| ✅ | EC2 | `i-093af5ff68e31616b` (t4g.medium, Ubuntu 26.04, key `qc_master`) |
| ✅ | RDS | `qualifiedcommercial` (PG 18.3, db.t4g.medium) |
| ✅ | Secret | `qcbackend/prod` (Secrets Manager) |
| ✅ | IAM role | `qcbackend-instance-role` + instance profile (attached to EC2) |
| ✅ | IAM role | `qcbackend-github-deploy` (OIDC trust to `unielogics/QualifiedCommercialBackend@main`) |
| ✅ | Route 53 A | `api.qualifiedcommercial.com` → EC2 |

All managed by Terraform in [`infra/`](../infra/). Everything is **additive** alongside hand-built VPC/EC2/RDS — `terraform destroy` won't touch the latter.

---

## Backend deploys (after first deploy)

```
Push to QualifiedCommercialBackend@main
  → GitHub Actions builds image → pushes to GHCR
  → assumes qcbackend-github-deploy role via OIDC
  → SSM Send-Command on EC2: docker pull → docker tag → systemctl restart qcbackend
  → live in ~3 min
```

GitHub repo settings needed (Settings → Secrets and variables → Actions):
- **Secret** `AWS_DEPLOY_ROLE_ARN` = `arn:aws:iam::156041400244:role/qcbackend-github-deploy`
- **Variable** `AWS_REGION` = `us-east-1`
- **Variable** `EC2_INSTANCE_ID` = `i-093af5ff68e31616b`

---

## Desktop deploys via AWS Amplify

### One-time setup

1. **AWS Console → Amplify → Create new app → Host web app**
2. **Source:** GitHub → authorize Amplify GitHub App → pick `unielogics/QCDashboard` → branch `main`
3. **Build settings:** Amplify auto-detects Next.js. Confirm:
   - Build spec: use the `amplify.yml` checked into the repo (Amplify will use the file automatically)
   - Platform: **Web compute** (required for SSR / middleware / Server Actions)
4. **Environment variables** — add ALL of these (Production scope). Get the Clerk values from the **Clerk Dashboard → API Keys**:
   ```
   NEXT_PUBLIC_API_URL                  = https://api.qualifiedcommercial.com
   NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY    = <pk_live_… from Clerk dashboard>
   CLERK_SECRET_KEY                     = <sk_live_… from Clerk dashboard>
   NEXT_PUBLIC_CLERK_SIGN_IN_URL        = /sign-in
   NEXT_PUBLIC_CLERK_SIGN_UP_URL        = /sign-up
   NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL  = /
   NEXT_PUBLIC_CLERK_AFTER_SIGN_UP_URL  = /
   ```
   Mark `CLERK_SECRET_KEY` as a **secret** (Amplify masks it from build logs). The other Clerk URL values are static and the same as in `.env.local.example`.
5. **Service role:** Amplify will prompt to create `amplifyconsole-backend-role` — accept.
6. **Save and deploy.** First build takes ~5 min.

### Custom domain

Once the first build succeeds:
1. **App settings → Domain management → Add domain**
2. Domain: `qualifiedcommercial.com`
3. Subdomain: `app` → branch: `main`
4. Amplify shows you a CNAME to add in Route 53 → add it (or it'll do it automatically via Route 53 detection if your AWS account owns the zone, which it does).
5. Cert provisioning takes ~5 min, then `app.qualifiedcommercial.com` is live.

### Subsequent deploys

Push to `unielogics/QCDashboard@main` → Amplify webhook fires → builds via `amplify.yml` → atomic switch → ~3-5 min.

---

## Backend first deploy (already done — for reference)

Two times in the lifecycle of the EC2 we run `deploy/ec2-bootstrap.sh`:
- **First time:** clones repo + builds image locally + sets up systemd unit + Caddy + qc database + pgvector. Output: `qcbackend.service` enabled but not started; user runs `systemctl start qcbackend`.
- **Re-runs:** idempotent — only does work if state is missing. Useful if you bring up a new instance.

```bash
ssh -i qc_master.pem ubuntu@54.157.222.116
curl -fsSL https://raw.githubusercontent.com/unielogics/QualifiedCommercialBackend/main/deploy/ec2-bootstrap.sh \
  | sudo -E bash
sudo systemctl start qcbackend
sudo journalctl -u qcbackend -f       # watch startup
```

After it's up:
```bash
curl https://api.qualifiedcommercial.com/                 # → {"status":"ok",...}
docker exec qcbackend python -m app.promote_user \
  --email franco@unielogics.com --role super_admin
```

---

## Rollback

```bash
# List recent GHCR tags
gh api repos/unielogics/QualifiedCommercialBackend/actions/workflows/deploy.yml/runs \
  --jq '.workflow_runs[:5] | .[] | "\(.head_sha[:8]) \(.head_commit.message)"'

# On the EC2 (SSH or SSM):
docker pull ghcr.io/unielogics/qcbackend:<sha-from-above>
docker tag ghcr.io/unielogics/qcbackend:<sha> qcbackend:current
sudo systemctl restart qcbackend
```

Database rollback: `alembic downgrade -1` works but is **dangerous** with real data. Snapshot RDS first:
```bash
aws rds create-db-snapshot --db-instance-identifier qualifiedcommercial \
  --db-snapshot-identifier pre-rollback-$(date +%s)
```

---

## Secret rotation

Edit `infra/terraform.tfvars` (gitignored), change the value, then:
```bash
cd infra && terraform apply
```
The EC2's daily cron (`/etc/cron.daily/qcbackend-refresh-env`) catches the diff and restarts the service. To force immediate refresh:
```bash
ssh ubuntu@54.157.222.116 "sudo /etc/cron.daily/qcbackend-refresh-env"
```

---

## Ongoing operations

- **Backend logs:** `journalctl -u qcbackend -f` on the EC2
- **Backend metrics:** `docker stats qcbackend` for CPU/mem; CloudWatch via Docker `awslogs` driver if you want centralized logs
- **Caddy logs (TLS / 4xx / 5xx):** `journalctl -u caddy -f`
- **RDS metrics:** RDS console → `qualifiedcommercial` → Monitoring tab
- **Amplify logs:** Amplify console → app → Hosting → Build/Deploy logs
- **Bedrock spend:** AWS Cost Explorer / Bedrock usage, filter on the Qualified Commercial account/project tags
- **Cost watch:** AWS Cost Explorer, filter on tag `Project=qualified-commercial`
- **Security:** Bedrock runs through the EC2 IAM role by default; rotate Clerk `sk_live_*` and any optional provider API keys every 90 days via Terraform.
