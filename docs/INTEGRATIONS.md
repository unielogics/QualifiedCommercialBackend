# Integrations

Status of each external service.

## Status

| Integration | Status | Notes |
|---|---|---|
| Clerk (auth) | ✅ prod live | `clerk.qualifiedcommercial.com`. Backend verifies via JWKS. |
| AWS Bedrock Claude (AI) | ✅ wired | Heavy + light Claude tiers through Bedrock Runtime. Tool-use loop stays native. |
| AWS account 156041400244 | ✅ provisioned | VPC, EC2, RDS, Secrets Manager, IAM roles, OIDC, Route 53. |
| AWS Amplify (desktop hosting) | ⏳ awaiting console click | `unielogics/QCDashboard` → `app.qualifiedcommercial.com`. See `DEPLOY.md`. |
| GitHub Actions deploy | ⏳ awaiting 3 settings | Need `AWS_DEPLOY_ROLE_ARN` secret + `AWS_REGION` + `EC2_INSTANCE_ID` variables on QualifiedCommercialBackend repo. |
| AWS S3 (documents) | ⏳ awaiting bucket create | EC2 instance role already has `s3:*` on `qc-documents-*`. Just needs the bucket. |
| iSoftpull (credit) | ⛔ production credentials missing | `ISOFTPULL_PUBLIC_KEY`, `ISOFTPULL_PRIVATE_KEY`, `ISOFTPULL_API_URL=https://app.isoftpull.com/api/v2`. Real bureau pulls only; the UI reports provider unavailable when keys are absent. |
| Plaid (business banking) | ⚠️ sandbox configured | Production requires the production secret, `DEALER_OS_PLAID_ENV=production`, Statements access, registered OAuth redirects, and the signed HTTPS webhook. Invalid environments fail closed. |
| RentCast (property) | ⏳ awaiting key | `RENTCAST_API_KEY`. SmartIntake autofill (sqft, taxes, comps). |
| EAS (mobile) | ⏳ awaiting Expo login | `unielogics/QCMobile`. Apple/Google accounts needed for store distribution. |
| Gmail Pub/Sub | 🛑 deferred | Local fake inbox covers the air-gap logic until prod. |
| Pinecone | ❌ replaced | Using pgvector (architecture constraint #1). |
| OpenAI | ❌ replaced | Using AWS Bedrock Claude only (architecture constraint #3). |
| Vercel | ❌ replaced | Switched to AWS Amplify (architecture constraint #9). |

## Prod credentials still needed

In rough order:
1. **GitHub PAT** (or you click in Settings) — to set the 3 deploy settings on the backend repo
2. **AWS Console access** for Amplify — to connect QCDashboard via the Amplify GitHub App and add env vars
3. **iSoftpull production credentials** — store the public/private pair in `qcbackend/prod`; production pulls remain disabled until present
4. **Plaid production credentials and Statements approval** — the current `qcbackend/prod` configuration remains sandbox and must not be relabeled as production
5. **RentCast API key** — when SmartIntake autofill is needed
6. **Apple Developer + Google Play accounts** — for mobile store distribution

## What's live in production right now

- `https://api.qualifiedcommercial.com/` — backend health check
- `https://api.qualifiedcommercial.com/api/v1/meta` — enums + lending limits
- `https://api.qualifiedcommercial.com/docs` — OpenAPI / Swagger UI
- `https://clerk.qualifiedcommercial.com/.well-known/jwks.json` — Clerk prod JWKS
