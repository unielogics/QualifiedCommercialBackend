# ChatGPT product discovery feed

The feed is managed in **Settings → ChatGPT product feed** by an active super
admin. The initial database configuration is paused. Installing this feature
does not register a merchant with OpenAI, enable checkout, create an advertising
campaign, or upload customer underwriting files.

## First-time setup

1. Configure `provider_secrets_kms_key_id` or a dedicated
   `provider_secrets_encryption_key` in the server environment. The feed will
   not create an SSH key using the provider service's legacy local fallback.
2. Choose **Create dedicated key**. Paste the displayed **public key** into
   OpenAI's Configure SFTP screen. The private key is encrypted in the existing
   provider secret store and is never included in an API response, log or file
   download. Do not paste it into ChatGPT or put it in source control.
3. Save the host, port, username and SHA256 server fingerprint supplied by
   OpenAI. Verify the fingerprint through a trusted OpenAI channel rather than
   accepting a key observed on a first unauthenticated connection.
4. Configure a separate writable staging directory excluded from ingestion.
   If the server uses a chrooted import root, obtain confirmation from OpenAI
   that the selected staging folder is not imported. The connection test
   checks write access and atomic overwrite without publishing a catalog.
5. Preview the catalog and review each selected offer. Confirm its canonical
   public product page, PNG/JPEG image, description, actual purchase price and
   current published source. Exclude unfinished offers explicitly.
6. Record OpenAI's confirmation that financing and consulting offers are
   accepted, including the agreed purchase-price representation. Then save,
   preview the saved settings, download a small valid snapshot and use
   **Sync now**. Verify processing in OpenAI before enabling daily delivery.

OpenAI's standard discovery schema requires a positive purchase price. A loan
principal, APR, interest rate, factor, percentage fee, illustrative payment or
quote-only service starting price is not automatically that price. The system
does not populate a placeholder or derive one from loan terms. Capital OS
references the existing quoted service at `/audit`; its website's demonstration
pricing is not promoted into a separately purchasable fixed-price offer.

## Catalog behavior

- Only the canonical public funding catalog and its published commercial terms
  feed the exporter. Draft terms, private counterparties, applications, client
  evidence and the legacy/static catalog are not exported.
- Priority: new MCA working capital; EZ Term and 10-year term financing;
  existing Capital OS consulting; SBA; remaining approved public programs.
  MCA funding and MCA refinancing retain different stable identifiers.
- Descriptions require staff approval. A changed public source revision
  invalidates its previous approval. The website's presentation manifest and
  images supply public assets, not availability, retail pricing or approval.
- Selected invalid offers block a replacement snapshot. An export failure
  cannot silently remove unfinished offers from the last working catalog.
- Published retirements and explicit exclusions use the previous valid record
  with `is_eligible_search=false`; removal by omission alone is delayed on the
  OpenAI side. Empty replacement files are not published.
- Product and image URLs must be public HTTPS URLs. Before delivery the server
  checks public DNS/IP destinations, TLS, HTTP status and content type without
  following unvalidated redirects. Use final canonical URLs.

## Delivery and operations

The output is one UTF-8 gzip-compressed CSV named
`qualified-commercial.csv.gz`, with stable item IDs and checkout disabled.
Daily delivery time is configured in UTC; Settings displays run timestamps in
the viewer's local timezone. The worker also checks approved catalog changes.
This application schedule is separate from personal Codex automations.

Database-backed run claims prevent simultaneous delivery across application
workers. A completed file is staged, its length checked, and atomically renamed
over the stable remote filename. Host keys are pinned, network operations are
bounded and errors are sanitized. Interrupted transfers remain outside the
imported files. Retries use the same durable run and are bounded to three
attempts. Settings changes invalidate queued work rather than sending a
superseded configuration. An upload already in progress must finish before
catalog, approval or connection settings can be changed; the editor explains
this conflict and preserves the working edits. After the first successful
delivery, changing the destination or filename requires a controlled migration
and removal of the old imported file with OpenAI, so competing catalogs cannot
be created accidentally.

**Uploaded** means SFTP transfer completed. It does not mean OpenAI processed,
accepted, ranked or displayed the offers. Processing remains **not verified**
until separately checked in the OpenAI account. Run history records timestamps,
attempts, record counts and snapshot hashes without credentials.

Dedicated super-admin endpoints under `/api/v1/admin/chatgpt-product-feed`:

| Method | Path | Purpose |
| --- | --- | --- |
| GET / PUT | `/settings` | Private configuration with revision conflict checks |
| POST | `/key-pair` | Idempotent encrypted-key setup; explicit rotation supported |
| POST | `/preview` | Validate saved settings or a supplied working draft |
| GET | `/download` | Download an accepted, valid saved snapshot |
| POST | `/test-connection` | Verify saved server identity and completed-file delivery |
| POST | `/sync` | Queue a validated upload with an idempotency key |
| GET | `/runs` | View the most recent delivery attempts |

Do not store feed credentials in the generic settings JSON or generic activity
logs. Run the database migration before starting the new worker. Migration
`0279_chatgpt_product_feed` seeds the paused singleton; it contains no keys,
borrower data or assumed merchant approval.

## References

- [OpenAI product schema](https://developers.openai.com/commerce/specs/file-upload/products)
- [OpenAI file delivery](https://developers.openai.com/commerce/specs/file-upload/overview)
- [OpenAI onboarding](https://developers.openai.com/commerce/guides/get-started)
