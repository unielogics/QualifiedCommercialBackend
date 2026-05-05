# Deploy Runbook

Architecture (locked in):
- **Backend** → EC2 (Docker container) + RDS Postgres (pgvector) + ALB + ACM
- **Desktop** → Vercel (Next.js)
- **Mobile** → EAS (iOS/Android binaries)
- **Domain** → `qualifiedcommercial.com`

| Subdomain | Points to | Purpose |
|---|---|---|
| `app.qualifiedcommercial.com` | Vercel | Operator desktop console |
| `api.qualifiedcommercial.com` | EC2 ALB | Backend FastAPI |
| `clerk.qualifiedcommercial.com` | Clerk | Production Clerk instance (CNAME) |
| `qualifiedcommercial.com` (apex) | Vercel | Marketing or 301 → `app.` |

---

## One-time AWS setup (do this in order)

### 1. Provision foundation
```
us-east-1
├── VPC qc-vpc (10.0.0.0/16)
│   ├── 2× public subnets (for ALB)
│   └── 2× private subnets (for EC2 + RDS)
├── ECR repo: qcbackend
├── Secrets Manager: qcbackend/prod (JSON blob with all env vars)
├── ACM cert for *.qualifiedcommercial.com (in us-east-1)
└── IAM:
    ├── qcbackend-instance-role (S3 write, Secrets read, SSM agent)
    └── qcbackend-deploy-role  (assumed by GitHub Actions via OIDC)
```

### 2. RDS Postgres 16 with pgvector
- Engine: PostgreSQL 16.x (pgvector extension is built in from 15.2+)
- Instance: `db.t4g.micro` to start (~$13/mo)
- Storage: 20 GB gp3
- Multi-AZ: **off** for now, **on** before paying customers
- Backups: 7 days
- Subnet group: the 2 private subnets in `qc-vpc`
- Security group: inbound TCP 5432 from EC2 SG only
- After creation, connect once and run: `CREATE EXTENSION vector;`

### 3. EC2 instance
- AMI: Amazon Linux 2023 (ARM if you went `t4g.*` for RDS — match for cost)
- Type: `t4g.small` to start (~$15/mo)
- Subnet: one of the public subnets (or private + NAT if you want stricter)
- Security group:
  - Inbound 80/443 from ALB SG only
  - Outbound: anywhere (for ECR, RDS, Anthropic, Clerk, S3)
- IAM instance profile: `qcbackend-instance-role`
- User data script (runs on first boot):
  ```bash
  #!/bin/bash
  set -eux
  dnf install -y docker
  systemctl enable --now docker
  usermod -aG docker ec2-user

  # SSM agent is preinstalled on AL2023 — confirm enabled
  systemctl enable --now amazon-ssm-agent

  # Pull /etc/qcbackend.env from Secrets Manager
  aws secretsmanager get-secret-value \
    --secret-id qcbackend/prod \
    --region us-east-1 \
    --query SecretString --output text \
  | jq -r 'to_entries[] | "\(.key)=\(.value)"' > /etc/qcbackend.env
  chmod 600 /etc/qcbackend.env
  ```
- **Tag** the instance with `Service=qcbackend` — the GitHub Actions deploy targets by tag.

### 4. Application Load Balancer (ALB)
- Listener: 443 (HTTPS) using the ACM cert
- Listener: 80 → 301 to 443
- Target group: HTTP:8000 → EC2 instance, health check `GET /`
- Route 53 record: `api.qualifiedcommercial.com` ALIAS → ALB

### 5. GitHub Actions OIDC role
Trust policy on `qcbackend-deploy-role`:
```json
{
  "Effect": "Allow",
  "Principal": { "Federated": "arn:aws:iam::<acct>:oidc-provider/token.actions.githubusercontent.com" },
  "Action": "sts:AssumeRoleWithWebIdentity",
  "Condition": {
    "StringEquals": { "token.actions.githubusercontent.com:aud": "sts.amazonaws.com" },
    "StringLike": { "token.actions.githubusercontent.com:sub": "repo:<your-org>/<your-repo>:ref:refs/heads/main" }
  }
}
```
Permissions on the role: ECR push, SSM SendCommand on tagged instances.

In GitHub repo Settings → Secrets & variables → Actions:
- secret `AWS_DEPLOY_ROLE_ARN` = ARN of `qcbackend-deploy-role`
- variable `AWS_REGION` = `us-east-1`

---

## Vercel setup (desktop)

1. Import the repo in Vercel.
2. **Root directory:** `qcdesktop`
3. **Build command:** `pnpm build` (auto-detected from `vercel.json`)
4. Add env vars (Production scope):
   - `NEXT_PUBLIC_API_URL` = `https://api.qualifiedcommercial.com`
   - `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY` = your **prod** Clerk publishable key
   - `CLERK_SECRET_KEY` = your **prod** Clerk secret key
   - `NEXT_PUBLIC_CLERK_SIGN_IN_URL=/sign-in`
   - `NEXT_PUBLIC_CLERK_SIGN_UP_URL=/sign-up`
   - `NEXT_PUBLIC_CLERK_AFTER_SIGN_IN_URL=/`
5. Add domain: `app.qualifiedcommercial.com` → follow Vercel's CNAME instructions (point CNAME to `cname.vercel-dns.com`).
6. Optional: add apex `qualifiedcommercial.com` with redirect to `app.`

---

## Clerk production instance

The current `pk_test_*` is dev only. For prod:

1. In Clerk dashboard, create a **Production Instance**.
2. Set the production frontend domain to `app.qualifiedcommercial.com`.
3. Set the Clerk-issued production frontend API to a CNAME you own: `clerk.qualifiedcommercial.com` → Clerk's address.
4. Enable production-grade settings: 2FA, session lifetime, etc.
5. Get new `pk_live_*` and `sk_live_*` keys; put them in Vercel env vars + Secrets Manager (`CLERK_SECRET_KEY`, `CLERK_JWKS_URL` = `https://clerk.qualifiedcommercial.com/.well-known/jwks.json`, `CLERK_ISSUER` = `https://clerk.qualifiedcommercial.com`).
6. Add `https://app.qualifiedcommercial.com` to allowed origins in Clerk dashboard.

---

## Secrets Manager payload (`qcbackend/prod`)

JSON blob:
```json
{
  "DATABASE_URL": "postgresql+asyncpg://qc:<pw>@<rds-endpoint>:5432/qc",
  "DATABASE_URL_SYNC": "postgresql+psycopg://qc:<pw>@<rds-endpoint>:5432/qc",
  "CORS_ORIGINS": "https://app.qualifiedcommercial.com",
  "CLERK_SECRET_KEY": "sk_live_...",
  "CLERK_JWKS_URL": "https://clerk.qualifiedcommercial.com/.well-known/jwks.json",
  "CLERK_ISSUER": "https://clerk.qualifiedcommercial.com",
  "ANTHROPIC_API_KEY": "sk-ant-...",
  "ANTHROPIC_MODEL_HEAVY": "claude-sonnet-4-6",
  "ANTHROPIC_MODEL_LIGHT": "claude-haiku-4-5-20251001",
  "AWS_REGION": "us-east-1",
  "S3_BUCKET": "qc-documents-prod",
  "USE_FAKE_INBOX": "true",
  "APP_ENV": "production",
  "LOG_LEVEL": "INFO"
}
```
(S3 access happens via the EC2 instance role — no `AWS_ACCESS_KEY_ID` needed in env.)

---

## First deploy

```bash
# 1. Build + push image manually (one-off, before the GH Actions role is set up)
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <acct>.dkr.ecr.us-east-1.amazonaws.com
docker build -t qcbackend:v0.1.0 qcbackend
docker tag qcbackend:v0.1.0 <acct>.dkr.ecr.us-east-1.amazonaws.com/qcbackend:latest
docker push <acct>.dkr.ecr.us-east-1.amazonaws.com/qcbackend:latest

# 2. SSH (or SSM Session Manager) into the EC2 instance, run once:
aws ecr get-login-password --region us-east-1 | docker login --username AWS --password-stdin <acct>.dkr.ecr.us-east-1.amazonaws.com
docker run -d --name qcbackend --restart=always -p 8000:8000 \
  --env-file /etc/qcbackend.env \
  <acct>.dkr.ecr.us-east-1.amazonaws.com/qcbackend:latest

# 3. Smoke-test
curl https://api.qualifiedcommercial.com/                       # → {"status":"ok",...}
curl https://api.qualifiedcommercial.com/api/v1/meta             # → enums + lending limits

# 4. Promote yourself to super_admin
docker exec qcbackend python -m app.promote_user \
  --email franco@unielogics.com --role super_admin
```

After that, every push to `main` auto-deploys via GitHub Actions.

---

## Rollback

```bash
# List recent images
aws ecr describe-images --repository-name qcbackend --query 'imageDetails[*].[imageTags[0],imagePushedAt]' --output table

# On the EC2 instance, run with the previous tag:
docker stop qcbackend && docker rm qcbackend
docker run -d --name qcbackend --restart=always -p 8000:8000 \
  --env-file /etc/qcbackend.env \
  <acct>.dkr.ecr.us-east-1.amazonaws.com/qcbackend:<previous-sha>
```

Database rollback: Alembic supports `alembic downgrade -1`. **But this is dangerous** — only use after confirming no data depends on the new schema, and take an RDS snapshot first.

---

## Ongoing operations

- **Logs:** `docker logs -f qcbackend` on the instance, OR ship to CloudWatch via the Docker awslogs driver
- **Metrics:** ALB target health is the basic uptime signal; add CloudWatch alarms on 5xx rate + target health
- **DB backups:** automatic via RDS (7d retention), plus weekly manual snapshot
- **Cost watch:** Anthropic spend dashboard, AWS Cost Explorer with a tag filter on `Service=qcbackend`
- **Security:** rotate ANTHROPIC_API_KEY + Clerk secret on a 90-day cadence, store in Secrets Manager, never in `.env` for prod
