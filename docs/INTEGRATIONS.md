# Integrations

Status of each external service.

## Status

| Integration | Status | Env vars | Notes |
|---|---|---|---|
| Clerk (auth) | ✅ wired (test instance) | `CLERK_SECRET_KEY`, `CLERK_JWKS_URL`, `CLERK_ISSUER`, `NEXT_PUBLIC_CLERK_PUBLISHABLE_KEY`, `EXPO_PUBLIC_CLERK_PUBLISHABLE_KEY` | Test instance: `normal-mollusk-16.clerk.accounts.dev`. Replace with prod (`clerk.qualifiedcommercial.com`) before launch. |
| Anthropic (AI) | ✅ wired & verified | `ANTHROPIC_API_KEY` | Models: `claude-sonnet-4-6` (heavy), `claude-haiku-4-5-20251001` (light). End-to-end tool-use verified. |
| AWS S3 (docs) | ⏳ awaiting keys | `S3_BUCKET` (creds via EC2 instance role) | Server-side encryption required. Bucket: `qc-documents-prod`. |
| AWS infra | ⏳ awaiting account | — | EC2 + RDS + ALB + ECR + Secrets Manager. See `DEPLOY.md`. |
| Vercel (desktop hosting) | ⏳ awaiting account | — | Project root: `qcdesktop`. Domain: `app.qualifiedcommercial.com`. |
| iSoftpull (credit) | ⏳ awaiting keys | `ISOFTPULL_API_KEY`, `ISOFTPULL_API_URL` | FCRA consent stored with each pull. Dev mode returns synthetic 712. |
| RentCast (property) | ⏳ awaiting keys | `RENTCAST_API_KEY` | SmartIntake autofill (sqft, taxes, comps). |
| Gmail Pub/Sub | 🛑 deferred | — | Local fake inbox covers air-gap logic until prod. |
| Pinecone | ❌ replaced | — | Using pgvector (architecture constraint #1). |
| OpenAI | ❌ replaced | — | Using Anthropic only (architecture constraint #3). |

## Prod credentials still needed

In rough order:
1. **AWS account ID + region + IAM** — to provision EC2, RDS, ECR, Secrets Manager, ACM
2. **Route 53 hosted zone for `qualifiedcommercial.com`** (or DNS access wherever it lives) — to point `api.` at ALB and `app.` at Vercel
3. **Clerk production instance keys** (`pk_live_*`, `sk_live_*`) — once you flip Clerk to production mode
4. **AWS S3 bucket name** — I'll create it, you confirm the name pattern (`qc-documents-prod`?)
5. **iSoftpull credentials** — when you're ready to do real soft pulls
6. **RentCast API key** — when you turn on SmartIntake autofill
7. **Apple Developer + Google Play accounts** — for mobile distribution beyond TestFlight/internal track
