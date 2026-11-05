# Existing Credit Report Import

The owner credit step links a completed pull and imports its existing report.
It must not initiate a new inquiry to recover a missing local score.

## Automatic Operation

Manual linking, automatic matches, sweeps, and signed authorizations all import
through the shared linking flow. Dealer-owner pulls use the same report importer.
No individual client or report ID is hardcoded in the workflow.

A separate scheduler job runs every five minutes in both manual and automatic
matching modes. It backfills existing linked reports and retries provider failures
in batches of 20, with per-report delays increasing from five minutes to one hour.
Only completed pulls still linked to an owner or file credit record are eligible; it never creates a
new inquiry. A service-account login/terms block stops the current batch.
The UI automatically refreshes pending report imports and shows provider failures
without clearing the association, original pull date, or a known real score.

## Data Sources

- Full Feed JSON, when supplied by the provider, is mapped to the native report viewer.
- Otherwise the approved dashboard service account retrieves the existing report HTML.
- The report's parsed fields are stored on the pull; original HTML is archived using the existing KMS-encrypted report archive.
- FICO 8, FICO 4, FICO 2, and Vantage 4 are supported headline models. An intelligence-check proxy is never presented as a verified bureau score.
- Imported scores synchronize to every owner already linked to that pull. The latest pull updates its own subject's client record, never an email-matched client.
- File credit snapshots referencing that exact pull are refreshed too, preserving unrelated intake data.

## Configuration

The running service regenerates `/etc/qcbackend.env` from AWS Secrets Manager
`qcbackend/prod` at startup. Configure these fields in that source secret,
not only in a local `.env` or the generated server file:

- `ISOFTPULL_LOGIN_EMAIL`
- `ISOFTPULL_LOGIN_PASSWORD`
- `ISOFTPULL_DASHBOARD_URL`

Do not commit credentials. The service-account holder must complete any provider
Terms Acceptance screen themselves. A successful sign-in does not prove report
access: iSoftPull can return HTTP 403 with `terms_not_accepted` after login.

## Operator Routes

- `GET /api/v1/application-profiles/{profile_id}/owners/{owner_id}/credit-report`
- `POST /api/v1/application-profiles/{profile_id}/owners/{owner_id}/credit-report/import`

Both require an operator role plus access to the specific application profile.
The owner is resolved within that profile and only its linked pull is read.
Imports preserve the original pull date and do not invoke the bureau-pull API.
Provider failures preserve the saved association and report an actionable error.

## Display

A returned pull without imported contents is shown as import pending, not as
evidence that the bureau returned no score. Once an actual bureau score is
available, the ownership credit-requirement cell and credit card show the score
in green. This green status means the credit evidence is linked, not that the
application has been approved or meets any lending threshold.
