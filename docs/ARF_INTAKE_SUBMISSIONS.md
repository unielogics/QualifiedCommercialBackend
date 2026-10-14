# ARF intake and institution submission workflow

Implemented across `qcbackend`, `qcdesktop`, and `qcweb`. Release through the existing migration-first backend deployment and Amplify pipelines. Publishing the software does not approve a lender package or authorize new financial intake.

## Public confidentiality

The marketing experience uses Qualified Commercial branding and descriptive product names. `/public/business-capital/catalog` exposes only educational titles, descriptions, features and industry examples. It deliberately omits counterparty names, proprietary product names, numeric source terms, internal program/channel identifiers, source versions and provenance. The legacy source-specific public router is not mounted; the general public funding catalog excludes source-specific products. Production API documentation/schema routes are disabled.

`/public/business-capital/referrals` accepts descriptive product/industry slugs and contact-consent information only. The server privately maps those choices to the existing channel record. This QC contact path does not publish a lender rule pack, enable financial collection, perform a credit pull, or send a lender submission. Borrower requirement correlations use stable public aliases instead of source-specific keys. Mandatory privacy and transaction-specific sharing disclosures remain intact.

## Operator workflow

1. Open **Settings → Funding programs → ARF channel** (`/admin/arf`). The four products import as draft source snapshots. Record independent written approvals and their permitted field/document scope before publishing or enabling hosted collection.
2. In **Lenders**, edit an institution and open **Configure lender requirements and forms**. Add a confirmed, source-referenced checklist, field/document/form/consent actions, fit rules, and any official forms. Saving creates a new offering version. An unconfirmed checklist can be previewed but cannot be sent.
3. Open an application’s evidence workspace and **Choose lenders**. Every active institution is available. Fit suggestions explain the published criteria; a nonrecommended institution can be included after acknowledging its warning.
4. **Collect missing for selected lenders** creates the combined task list. Matching fields/documents share a collection action; each institution keeps its own policy and sharing consent. Borrowers answer questions, complete existing financial forms, upload files, or accept a versioned disclosure in their existing PIN-protected room.
5. **Prepare packages** creates a separate manifest and document selection for each institution. Missing forms, consent, or explicit nonwaivable lender requirements remain send blockers. ZIP previews are marked internal when configuration is incomplete.
6. Review and **Approve and send ready packages**. Only Super Admin and Underwriter/LOAN_EXEC can approve. Each recipient receives its own expiring link and ZIP download; no shared CC list is used.

Preparation refreshes previously selected, pending packages after material facts/evidence change. New package versions never inherit send approval. Previous batches remain in history. Staff can pause or take over collection tasks, retry a known failed send, and revoke one recipient without affecting the others.

## File behavior

Usability (`processing`, `usable`, `usable_with_warning`, `needs_review`, `needs_replacement`, `unsafe`) is independent from requirement satisfaction and lender fit. Parsing uses content signatures, PDF text/vision, spreadsheet data, DOCX text/tables, and HEIC conversion. Filename hints do not establish period coverage or acceptance. A document may support multiple requirements, and targeted uploads stay linked when AI disagrees with their type.

Low confidence, duplicate content, unsupported parsing, unreadability, and password protection retain the file for review or replacement. File size, transport integrity, and quarantined/security failures remain ingestion controls. This feature does not add a malware scanning provider.

`EVIDENCE_DECISION_MODE=shadow` is the default: immutable evidence-decision rows record legacy/new comparisons without extra AI calls. Both modes retain uncertain files. `active` disables comparison auditing; review and retention behavior stays enabled. Existing cached analyses do not trigger a bulk paid reanalysis.

Lender document configuration supports `evidence_classes`, `required_months` (1–60), `required_years` (1–10), `minimum_count` (1–100), `expiration_days` (1–3650), subject and period. Use either month coverage or year coverage. Field types and validation, form kind/schema, and consent disclosure/version live in the same versioned configuration.

## Delivery and auditing

The database stores `SubmissionBatch`, one `LenderSubmission` per institution, and append-only delivery/access events. Preparation and approval are idempotent. The scheduler claims queued deliveries every 30 seconds and rechecks current approval, account standing, recipient, permissions, consent, and source fingerprints before sending. ZIP and document downloads verify the approved bytes and source version.

Provider acceptance records `sent`; it does not claim `delivered`. Viewing/downloading records the corresponding access event. Underwriters can reconcile a confirmed delivery with its provider reference. Timeouts or worker interruption produce `delivery_unknown`; these cannot be retried until an operator reconciles them as not sent. Successful recipients are never resent by batch retry. Pending package refresh and delivery are independent, so a refresh failure does not prevent queue draining.

## Migration and activation

- Apply Alembic revision `0237_arf_intake_submissions` before starting the updated backend (`alembic upgrade head`). It adds mixed requirements, structured responses/tasks, expanded analysis/audit columns, ARF configuration/referrals/assessment snapshots, lender offerings, batches, child submissions, and events.
- Backend startup idempotently imports exactly four draft ARF products into the existing catalog. It never automatically publishes or changes an already imported product version.
- Install the updated locked dependencies (`uv sync --locked`); DOCX extraction uses the standard library and HEIC conversion uses Pillow/pillow-heif.
- Deploy the backend, desktop, and marketing changes together. The public lender link route is anonymous but token scoped; it has no authenticated console shell.
- Public copy/rule publication, hosted intake scope, document sharing, credit pulls, geography and submission route each require their own evidence in `/admin/arf`. Referral-only remains the default. Publishing copy does not enable financial intake.
- Confirm official institution checklists and forms through the offering editor. The full industry taxonomy and supplied package’s referenced acceptance-test data are still missing source artifacts. The implementation does not fabricate them or an approved term resolution.
- The April 24-month snapshots, March 36-month observation, monthly/annual revenue discrepancy, IO-Bankroll conflict, skilled-trade product mapping, and Tier 1 expansion remain explicit confirmation items. Loan Stars stays separate from borrower products.

## Validation performed

- Full backend regression suite: 3,081 passing tests, 3 skipped. Includes public confidentiality, collection, outreach, ARF rules, evidence, file parsing, program editor, readiness, packages and lender products. Windows uses `python -X utf8 -m pytest`.
- Real isolated SQLite persistence workflow: shared question, two separate consents, scoped lender criteria, versioned package preparation, automatic refresh, fresh human approval and repeat-approval idempotency. PostgreSQL-specific DDL compiled with Alembic offline; no local PostgreSQL server was available for migration execution.
- Desktop: 17 targeted tests, TypeScript check and production build passed. Marketing: TypeScript, link audit and production build passed.
- New routers mount successfully and the complete OpenAPI schema generates. Changed module lint and whitespace checks passed. Existing deprecation, browser-data and unrelated UI hook warnings remain.

Live lender delivery, provider delivery notifications, production access flows and a production PostgreSQL upgrade have not been exercised. Initial institution delivery is secure-link/ZIP; direct institution portal/API integrations are outside this release.
