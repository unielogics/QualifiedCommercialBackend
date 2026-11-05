# MCA Intake Audit - November 5, 2026

Scope: the public `https://qualifiedcommercial.com/mca-refinance` CTA, its
`/mca-refinance-intake` destination, upload protocol, and staff AI intake detail.
No live client application, email, credit inquiry, or test document was created.

## Confirmed Findings And Fixes

- A network/5xx resume failure cleared a valid saved token. Only invalid/expired
  access now clears it; transient failures preserve access and offer reconnect.
- URL cleanup ran before language and attribution reads. The arrival locale,
  page/campaign/CTA and a credential-free entry URL are now captured first.
- Browser storage exceptions could interrupt otherwise valid applications.
  Session persistence now tolerates restricted/private browser storage.
- Session-cookie requests lacked cross-origin credentials and the session
  endpoint was unused. The room now attempts cookie-backed resume when no token
  is available, without replaying application creation.
- One upload disabled every document picker. Files can now queue across requests
  under one serialized worker, preserving the chosen document association.
- Failed files had no retry/remove controls. Storage retries are bounded, and a
  stored file can retry confirmation without another upload/init or duplicate.
- Completed file receipts disappeared if the follow-up refresh failed. The
  completion response is now rendered immediately, independently of refresh.
- Background document review/takeover updates never refreshed on an idle page.
  Visible rooms now poll, pausing during writes/uploads and when hidden.
- Saved years in business was omitted when restoring the qualification editor.
  It is now hydrated from the server instead of appearing unanswered again.
- Word agreements and supported image variants were excluded by the picker.
  DOCX/WebP/HEIF are available. Empty and over-100-MB files fail before transfer.
- Start fields did not support native form submission. The start form now
  supports Enter and semantic required fields; program/consent rules are unchanged.

## Source Links

Staff AI intake detail shows the recorded marketing source and sanitized original
entry link when present. Access tokens, fragments, contact query parameters and
credential-bearing room/form paths are excluded. Unknown historical attribution
is explicitly labeled unknown, with a general MCA application-page link rather
than a fabricated original URL. The source projection is staff-only.

## Verification And Limits

Browser regression cases use synthetic API responses/files across 1600, 1440,
1280 and 390-pixel viewports, including transient resume/confirmation failures,
multiple documents, locale, storage restrictions and background refresh.
Backend checks preserve authoritative upload inspection and MCA program policy.

The marketing CTA points to the correct MCA application with page/campaign/locale
parameters. A bounded production container-log sample supplied no usable MCA
request history; the host's conventional nginx access log was absent. Therefore
this audit does not attribute a particular HTTP error to the reporting client.
Provider/network/S3 behavior is simulated in browser tests, not a live upload into
a client's file. Real-world connection/file-specific failures remain possible.
