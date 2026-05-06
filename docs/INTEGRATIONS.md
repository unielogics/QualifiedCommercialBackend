# Integrations

Status of each external service.

## Status

| Integration | Status | Notes |
|---|---|---|
| Clerk (auth) | ✅ prod live | `clerk.qualifiedcommercial.com`. Backend verifies via JWKS. |
| Anthropic (AI) | ✅ wired & verified | Sonnet 4.6 + Haiku 4.5. Tool-use loop verified end-to-end. |
| AWS account 156041400244 | ✅ provisioned | VPC, EC2, RDS, Secrets Manager, IAM roles, OIDC, Route 53. |
| AWS Amplify (desktop hosting) | ⏳ awaiting console click | `unielogics/QCDashboard` → `app.qualifiedcommercial.com`. See `DEPLOY.md`. |
| GitHub Actions deploy | ⏳ awaiting 3 settings | Need `AWS_DEPLOY_ROLE_ARN` secret + `AWS_REGION` + `EC2_INSTANCE_ID` variables on QualifiedCommercialBackend repo. |
| AWS S3 (documents) | ⏳ awaiting bucket create | EC2 instance role already has `s3:*` on `qc-documents-*`. Just needs the bucket. |
| iSoftpull (credit) | ✅ demo wired | `ISOFTPULL_PUBLIC_KEY`, `ISOFTPULL_PRIVATE_KEY`, `ISOFTPULL_API_URL`. Real bureau pulls — no synthetic fallback. POST /credit/pull returns 503 if keys are absent. |
| RentCast (property) | ⏳ awaiting key | `RENTCAST_API_KEY`. SmartIntake autofill (sqft, taxes, comps). |
| EAS (mobile) | ⏳ awaiting Expo login | `unielogics/QCMobile`. Apple/Google accounts needed for store distribution. |
| Gmail Pub/Sub | 🛑 deferred | Local fake inbox covers the air-gap logic until prod. |
| Pinecone | ❌ replaced | Using pgvector (architecture constraint #1). |
| OpenAI | ❌ replaced | Using Anthropic only (architecture constraint #3). |
| Vercel | ❌ replaced | Switched to AWS Amplify (architecture constraint #9). |

## Prod credentials still needed

In rough order:
1. **GitHub PAT** (or you click in Settings) — to set the 3 deploy settings on the backend repo
2. **AWS Console access** for Amplify — to connect QCDashboard via the Amplify GitHub App and add env vars
3. **iSoftpull production credentials** — currently using demo keys; swap for production tier before shipping
4. **RentCast API key** — when SmartIntake autofill is needed
5. **Apple Developer + Google Play accounts** — for mobile store distribution

## What's live in production right now

- `https://api.qualifiedcommercial.com/` — backend health check
- `https://api.qualifiedcommercial.com/api/v1/meta` — enums + lending limits
- `https://api.qualifiedcommercial.com/docs` — OpenAPI / Swagger UI
- `https://clerk.qualifiedcommercial.com/.well-known/jwks.json` — Clerk prod JWKS
