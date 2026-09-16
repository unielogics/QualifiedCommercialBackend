# Dealer Prospect Pipeline operations

The Dealer Prospect Pipeline is a Field Desk feature for controlled dealer
outreach. It is deliberately disabled by default. Agent pipeline surfaces
return `404` unless both `DEALER_PROSPECT_PIPELINE_ENABLED=true` and that
operator's per-user entitlement are enabled. The administrative access-control
and collateral endpoints remain available so the pilot can be configured
before the master switch opens.

## Production prerequisites

Complete every item before enabling the feature:

1. Apply Alembic revisions `0219_dealer_prospect_pipeline`,
   `0220_prospect_outreach`, and `0221_dealer_prospect_user_access`.
2. Enable Amazon Nova Micro (`amazon.nova-micro-v1:0`) in the deployment
   region. The instance role already permits Amazon Nova foundation models.
3. Verify `qualifiedcommercial.com`, including
   `no-reply@qualifiedcommercial.com`, for SES sending. Keep the SES policy's
   allowed From addresses restricted to the configured company senders.
4. Ensure `support@qualifiedcommercial.com` accepts plus-addressed replies, for
   example `support+<thread-token>@qualifiedcommercial.com`. Route those
   messages into the real mailbox configured by `GMAIL_DELEGATED_USER`; this
   may be Support's mailbox or the existing audited shared inbox. The locked
   footer also names `franco@qualifiedcommercial.com` as an alternate contact,
   but it is deliberately not a second Reply-To recipient.
5. Configure domain-wide delegation for that inbox and set
   `USE_FAKE_INBOX=false`, `GMAIL_SERVICE_ACCOUNT_PATH`,
   `GMAIL_DELEGATED_USER`, and `USER_INBOX_SYNC_ENABLED=true` only after a
   reply-correlation smoke test succeeds. Do not replace an existing delegated
   inbox without confirming its current lender/client reply routes continue to
   arrive there.
6. Confirm SPF, DKIM, and DMARC alignment for the Dealer Desk sender. Provision
   SES/SNS feedback in the two phases below: first create the topic and inject
   its ARN with `ses_feedback_subscription_enabled=false`, restart the API so
   it trusts that exact ARN, then set the flag to `true` and apply again to
   create the HTTPS subscription and event destination. Confirm the signed
   `/api/v1/webhooks/ses` subscription is active; bounce and complaint
   suppression depends on that event path.
7. Provision a private ClamAV daemon reachable only from the API runtime and
   set `PROSPECT_COLLATERAL_CLAMD_HOST`. Uploads fail closed when the scanner is
   absent, unavailable, detects malware, or returns an indeterminate result.
   Then upload, validate, approve, order, and activate the current Dealer
   Outreach PDFs. Borrower evidence and application documents must never be
   uploaded to this library. Static integrity, encryption, active-content, and
   EICAR checks run before the managed antivirus scan.
8. Confirm the locked company mailing address and agent Field Desk profile
   signatures are correct.
9. Give only the pilot agents Field Desk access, then enable their individual
   Pipeline controls with `PATCH /api/v1/dealer-os/admin/prospect-access/{user_id}`.
   The list endpoint is `GET /api/v1/dealer-os/admin/prospect-access`. Both
   admin endpoints remain available while the global switch is off. Finally,
   set `DEALER_PROSPECT_PIPELINE_ENABLED=true`.

Required runtime settings:

```dotenv
DEALER_PROSPECT_PIPELINE_ENABLED=true
PROSPECT_FROM_EMAIL=no-reply@qualifiedcommercial.com
PROSPECT_FROM_NAME=Qualified Commercial Dealer Desk
PROSPECT_REPLY_TO_EMAIL=support@qualifiedcommercial.com
PROSPECT_ALTERNATE_CONTACT_EMAIL=franco@qualifiedcommercial.com
PROSPECT_MAILING_ADDRESS=14 53rd St #408N, Brooklyn, NY 11232
PROSPECT_EMAIL_REVIEW_SECONDS=60
PROSPECT_EMAIL_MAX_ATTACHMENT_BYTES=7000000
PROSPECT_SECURE_BUNDLE_DAYS=7
PROSPECT_COLLATERAL_CLAMD_HOST=clamd.internal
PROSPECT_COLLATERAL_CLAMD_PORT=3310
PROSPECT_COLLATERAL_CLAMD_TIMEOUT_SECONDS=15
USER_INBOX_SYNC_ENABLED=true
USE_FAKE_INBOX=false
GMAIL_SERVICE_ACCOUNT_PATH=/etc/qcbackend/gmail-sa.json
GMAIL_DELEGATED_USER=<real Workspace mailbox receiving support+ aliases>
SES_FEEDBACK_TOPIC_ARN=arn:aws:sns:us-east-1:<account-id>:qualified-commercial-ses-delivery-events
```

The application temporarily normalizes the retired
`dealers@qualifiedcommercial.com` sender and Reply-To values to the settings
above so an older production secret cannot reactivate obsolete routing. Update
the secret to the current values during the next credential maintenance window.

Terraform injects `SES_FEEDBACK_TOPIC_ARN` into the production Secrets Manager
payload from the topic it creates. The literal ARN above documents the expected
shape for non-Terraform environments; do not copy an ARN from another account.

### SES feedback two-phase apply

The webhook deliberately rejects every SNS topic except the provisioned one.
That means the running API must reload the new ARN before SNS sends its HTTPS
confirmation request.

1. Keep `ses_feedback_subscription_enabled = false` and apply Terraform. This
   creates the SNS topic/policy and writes its ARN into the runtime secret, but
   does not create the subscription or SES event destination.
2. Restart or redeploy `qcbackend` and verify its refreshed environment contains
   the Terraform `ses_feedback_topic_arn` output value.
3. Set `ses_feedback_subscription_enabled = true` and apply Terraform again.
   Verify the HTTPS subscription reports `Confirmed` before enabling outreach.

If the confirmation callback cannot follow Amazon's signed `SubscribeURL`, it
returns a retryable `503`; do not work around this by weakening the TopicArn or
SNS signature checks.

## Delivery invariants

- A generated draft is scheduled on the server. Closing the browser does not
  stop the 60-second review window.
- Editing stops automatic delivery permanently; the edited revision requires
  explicit approval.
- Approval, cancellation, editing, and scheduled dispatch use record versions
  and database locks. Once a dispatcher claims a draft as `sending`, it is not
  automatically retried, preventing duplicate delivery after an ambiguous SES
  response.
- Recipient validity, user authorization, do-not-contact state, global
  suppression, and the exact collateral snapshots are checked immediately
  before send.
- The attachment bundle is all-or-none. When it exceeds the configured MIME
  safety limit, automatic sending is blocked until an operator explicitly
  chooses the expiring secure-bundle link.
- Unsubscribe, complaint, bounce, bad-address, and administrative suppression
  apply globally before every send.
- AI instructions never contain the private internal note. Product claims and
  URLs come from the approved catalog and locked footer; invalid AI output
  falls back to the deterministic approved template.

## Shared mailbox verification

Before the pilot, send one message to a controlled external address and reply
to it. Verify all of the following:

- The From header is `Qualified Commercial Dealer Desk
  <no-reply@qualifiedcommercial.com>`.
- Reply-To contains the draft's `support+<thread-token>@qualifiedcommercial.com`
  address, and the locked reply note names Support and Franco exactly once.
- The message includes List-Unsubscribe and one-click unsubscribe headers.
- The exact approved PDF versions are attached, or the explicitly selected
  secure-bundle link is present.
- The reply is correlated to the prospect, appears in its activity view, and
  creates a notification for the assigned agent.
- Unsubscribing adds a global suppression and prevents a subsequent draft from
  sending.

## Monitoring and recovery

Monitor scheduler logs for `prospect_email_dispatch`, SES configuration-set
events, suppression growth, drafts stuck in `sending`, and shared-inbox sync
errors. A draft left in `sending` after an ambiguous provider response needs a
manual provider-led reconciliation; do not reset it to pending automatically.

To stop new use, set `DEALER_PROSPECT_PIPELINE_ENABLED=false`. Public links
already included in sent mail must remain usable according to their normal
expiry and suppression semantics. The flag does not cancel already queued
drafts—the 60-second promise remains durable—so cancel pending drafts first
when an incident requires an immediate delivery stop. Disabling the feature
does not undo sent email or delete pipeline data.

Disabling an individual user's entitlement takes effect immediately for
interactive requests and causes that user's unsent drafts to fail the
authorization recheck at dispatch. The access mutation is recorded in the
append-only `user_access_events` trail.

Before a database downgrade, disable the feature, drain or cancel pending
drafts, take an RDS snapshot, and export the outbox/audit records. Downgrading
the two migrations deletes pipeline and outreach data and is not a normal
rollback mechanism.
