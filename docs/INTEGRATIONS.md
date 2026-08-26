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
| Plaid (business banking) | Production-capable; dashboard approval required | Uses Statements and Assets, update mode, signed webhooks, encrypted environment-scoped tokens, explicit Item/Asset Report removal, and the branded Link client name. Production activation still requires published Data Transparency Messaging, product access, production keys, and registered OAuth redirects. |
| RentCast (property) | ⏳ awaiting key | `RENTCAST_API_KEY`. SmartIntake autofill (sqft, taxes, comps). |
| EAS (mobile) | ⏳ awaiting Expo login | `unielogics/QCMobile`. Apple/Google accounts needed for store distribution. |
| Gmail Pub/Sub | 🛑 deferred | Local fake inbox covers the air-gap logic until prod. |
| Transactional SMS | Provider-switch ready | Set `SMS_PROVIDER=twilio` or `aws`. Both implementations remain available; sends never fall back silently. Twilio requires a Messaging Service or sender plus signature-validated webhooks. |
| Address search | Provider-switch ready | Super admins select Google or Geoapify in Settings. Geoapify keys are encrypted in `provider_secrets`; the Field Desk keeps the same backend API. |
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

## SMS provider switch

Keep `SMS_PRODUCTION=false` until the selected provider is fully registered and
tested. To use Twilio, configure `TWILIO_ACCOUNT_SID`, a Standard API key pair
(`TWILIO_API_KEY_SID`, `TWILIO_API_KEY_SECRET`), `TWILIO_AUTH_TOKEN`, and either
`TWILIO_MESSAGING_SERVICE_SID` (recommended) or `TWILIO_FROM_NUMBER`. Configure
the Messaging Service inbound webhook as:

`https://api.qualifiedcommercial.com/api/v1/webhooks/twilio/sms/inbound`

Delivery callbacks are attached automatically at:

`https://api.qualifiedcommercial.com/api/v1/webhooks/twilio/sms/status`

Then set `SMS_PROVIDER=twilio`, `SMS_PRODUCTION=true`, and restart the backend.
Switching back to AWS only requires `SMS_PROVIDER=aws` plus the existing AWS
origination identity and webhook token.

To use AWS End User Messaging, configure the production secret with:

- `SMS_PROVIDER=aws`
- `SMS_PRODUCTION=true`
- `SMS_ORIGINATION_NUMBER=<AWS phone number, pool ARN, or sender identity>`
- `SMS_WEBHOOK_TOKEN=<strong random token for inbound SMS callbacks>`

The EC2 backend role must allow `sms-voice:SendTextMessage` for outbound
transactional messages. It also needs `DescribeAccountAttributes`,
`DescribePhoneNumbers`, `DescribePools`, and `DescribeSenderIds` for production
readiness checks. The Terraform role policy in `infra/secrets.tf` includes
these permissions.

## Geoapify address switch

In the super-admin Settings page, save a Geoapify server API key and select
`Geoapify (recommended)` as the address provider. Restrict the key to Geocoding,
Address Autocomplete, and Place Details APIs and to production backend traffic.
No Field Desk frontend key or route change is required. Google stays configured
as a reversible option but is not used while Geoapify is selected.

## What's live in production right now

- `https://api.qualifiedcommercial.com/` — backend health check
- `https://api.qualifiedcommercial.com/api/v1/meta` — enums + lending limits
- `https://api.qualifiedcommercial.com/docs` — OpenAPI / Swagger UI
- `https://clerk.qualifiedcommercial.com/.well-known/jwks.json` — Clerk prod JWKS
