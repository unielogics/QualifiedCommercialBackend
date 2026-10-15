# Plaid operations

Verified 2026-10-15: Assets is enabled in the Plaid Dashboard and working against
Production. Statements is not enabled. Transfer's Dashboard allows Sandbox only;
its empty Sandbox task list is not evidence of Production approval.

## Evidence configuration

Use `DEALER_OS_PLAID_ENV=production` and `DEALER_OS_PLAID_PRODUCTS=assets` until
Statements is approved. Product selection on a file cannot override deployment
availability. Keep the existing production Client ID/secret and dedicated
`PROVIDER_SECRETS_ENCRYPTION_KEY` in Secrets Manager `qcbackend/prod`.

Registered OAuth returns:

- Team: `https://audit.qualifiedcommercial.com/plaid/oauth`
- Client room: `https://app.qualifiedcommercial.com/plaid/oauth`
- Webhook: `https://api.qualifiedcommercial.com/api/v1/webhooks/plaid`

Statements may be added only after its production entitlement is approved and
both initial and update-mode Link sessions succeed. Bank support is a separate
question: combined mode requests Statements only where the institution supports
it. This does not bypass Plaid account entitlement.

## Report and connection lifecycle

Alembic `0238_plaid_lifecycle_safety` queues existing ingested reports for cleanup.
New ingestion queues cleanup in the same transaction that saves the PDF reference
and normalized evidence. Every five minutes the cleanup job confirms the archived
PDF can be read before calling `/asset_report/remove`. The ingested status and
30-day freshness are preserved. PDF endpoints serve the retained archive, so
remote deletion does not break downloads or trigger another billed report.

Manual report removal and permission revocation invalidate the local report and
remove the remote report. Temporary failures retain the encrypted report token
and retry automatically. `provider_removed_at` is set only after confirmation;
`provider_removal_error` exposes a blocked cleanup without storing raw responses.
Late webhooks cannot resurrect removed reports or revoked connections.

Local underwriting evidence remains subject to the funding file retention policy.
Revoking a bank does not delete retained signed agreements or underwriting records.
Remote report deletion and local evidence retention are separate operations.

Removed evidence Items whose provider call fails retain their encrypted token
only for cleanup. No refresh may use a removed or revoked Item. Abandoned payment
exchanges expire after 24 hours; expired/replaced payment Items are removed even
while new payment dispatch is disabled. Failed removal preserves the token for
retry. Environment-unknown legacy payment sources require reconnection.

## Transfer activation

Complete Plaid's Transfer application and production implementation tasks, confirm
the approved use case, funding account, ledger/reserve requirements and limits,
and obtain legal approval of the existing ACH authorization workflow. Product
activation may require a Plaid contract; an engineering release is not approval.

Configure the explicit `PAYMENTS_PLAID_CLIENT_ID`, `PAYMENTS_PLAID_SECRET`,
`PAYMENTS_PLAID_ENV=production`, webhook and OAuth return. These credentials may
belong to the same approved Plaid app, but are explicit: evidence credentials
never silently grant payment authority. `PAYMENTS_PLAID_RULESET_KEY` optionally
selects an approved custom Signal ruleset; otherwise Plaid's default applies.

Run Sandbox tests for approved/declined/incomplete-risk authorizations, timeout
recovery, user repair, signed webhook rejection, event replay, returns and refunds.
Verify signed proof and debit notices. Then conduct the approved controlled live
test and reconcile its provider events, bank statement and ledger. Keep
`PAYMENTS_ENABLED`, `PAYMENTS_ACH_LEGAL_APPROVED`, `PAYMENT_REFUNDS_ENABLED` and
`PRIVATE_FUNDING_PAYMENTS_ENABLED` false until their respective signoffs pass.

Only an approved authorization with a null rationale code is eligible for automatic
submission. Any incomplete-risk approval stops for review; a retry cannot bypass
the durable authorization policy. Existing provider transfers are still reconciled
even when new payment dispatch is off. Permission revocation stops unclaimed
intents and terminates mandates while retaining their proof for the required period.

## Release and monitoring

Run `python -m scripts.check_plaid_readiness` in the production container. It
returns configuration checks, Link-token probe results and aggregate lifecycle
counts; it never prints credentials, access tokens, Link tokens or customer IDs.
It creates no Items, reports, authorizations or transfers.

Run the full backend suite with UTF-8 enabled on Windows:
`python -X utf8 -m pytest -q`. Apply the Alembic migration before starting the new
backend. Push a tested backend commit to `origin/main` to use the existing deploy
workflow, then verify `/ready`, the readiness report, cleanup outcomes and archive
downloads. Watch `plaid_cleanup` logs and queued cleanup errors. A successful Link
token probe confirms credential/product/redirect acceptance; a real bank login
and money movement still require the controlled user validation above.

Primary references:

- https://plaid.com/docs/api/products/assets/
- https://plaid.com/docs/api/products/transfer/initiating-transfers/
- https://plaid.com/docs/transfer/application/
- https://plaid.com/docs/transfer/signal-rules/
