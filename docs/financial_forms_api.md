# Financial forms API

Four financial forms live on a file (`ApplicationProfile`): the personal
financial statement (`pfs`, SBA Form 413), the business debt schedule
(`debt_schedule`), the profit and loss statement (`p_and_l`) and the balance
sheet (`balance_sheet`). Everything below is plain JSON over the existing API,
so a console, a mobile app or another platform reads the same thing. Money is
carried as the string the person typed; the server parses it.

New staff integrations should use the financial workspace below. The existing
form, public-link, packet, and worksheet routes remain compatibility adapters:
draft writes stage changes; explicit save/submit publishes a version.

## Financial workspace and versions (staff)

Prefix: `/api/v1/application-profiles/{profile_id}/financial-workspace`.
`kind` is `pfs`, `debt_schedule`, `p_and_l`, or `balance_sheet`.

| Method and suffix | Request / result |
| --- | --- |
| `GET ?kind=…&family_id=…` | Shared body/schema, worksheet layout and values, draft revision/base version, current version, dated history, families, and evidence. `family_id` is optional. |
| `POST /families` | `{kind, label?, identity}` creates a separate person/statement or reporting period. Identity supports `subject`, PFS `owner_ids`, `as_of`, or business `period_start`, `period_end`, `as_of_date`. |
| `PUT /draft` | `{kind, family_id?, body, expected_revision}` stages the shared form/worksheet body and returns the updated workspace. |
| `POST /versions` | `{kind, family_id?, expected_revision, base_version_id, idempotency_key, submit:false}` saves the next immutable version, its dated PDF, and financial projections atomically. Returns the workspace and `saved_version`. |
| `GET /versions/{id}/pdf` | Returns that version's stored, hash-checked PDF. |
| `POST /versions/{id}/share` | `{ttl_days?:7}` creates a read-only preview/download link to only that version's PDF. Expiry is 1–30 days. Later saves do not change the shared file. |
| `POST /versions/{id}/restore` | `{expected_revision}` restores the selected body into the draft; Save version publishes the next numbered revision. |
| `POST /imports` | `{kind, file_id, family_id?}` preserves the uploaded original and proposes mapped fields with source file/hash/page/quote, confidence, and warnings. |
| `POST /imports/{id}/apply` | `{expected_revision, selected_keys?}` applies selected mappings to the draft; it does not publish. |

Working drafts do not change financial summaries, DSCR, or AI inputs. Save
version publishes the exact accepted body used for its PDF and projections.
For debt schedules, Save version also completes the checklist when every active
obligation has a named lender, current balance and known monthly payment. Native
payment terms can establish that monthly amount; zero is valid. Other fields may
remain unknown. A debt-free business must explicitly set `no_debt:true` rather
than leave an untouched empty schedule. The response includes `completion` for
the working body and `accepted_completion` for the saved version. Incomplete
first versions remain incomplete; an incomplete replacement of a completed
schedule is rejected while retaining the draft and prior version.

For the other forms, `submit:true` additionally files the PDF on the checklist.
A failed render/storage operation retains the draft and does not
advance the current version. Reuse the same idempotency key when retrying a save.
Revision/base-version conflicts return `409`; reload/reconcile before retrying.

Versions belong to a family, so different PFS owners and reporting periods stay
separate. Originals retain their bytes and upload dates; later edits create
dated v2, v3, and so on. AI/evidence readers select each family's current version;
historical originals and generated PDFs remain available through history. Public
form links bind to their financial family, retaining token/PIN scope.

A complete saved debt schedule is authoritative for financial debt totals and
DSCR inputs. Bank-observed vendors, pending replacement PDFs, and automated debt
drafts cannot add obligations or replace payments in it. Automation remains
available before completion. Existing complete saved versions are recognized
without changing their immutable history. Working edits must be saved as a new
complete version before replacing this authority.

Field Desk confirmation, application readiness, and lender-package preparation
also recognize that accepted version. They do not require a second debt-schedule
or monthly-payment confirmation. Existing annual-sales and cash-flow confirmation
rules remain in effect. A new incomplete reporting-period draft cannot overwrite
an accepted complete schedule's projections.

### Bank payee reconciliation

Payee matching lives in **Evidence & Banking**, separately from form editing.
`GET …/application-profiles/{profile_id}/banking/debt-payees` returns the scoped
active obligations and outgoing payees from the existing bank-ledger rollup.
`PUT …/banking/debt-payees/{row_id}` takes `{vendor_key:string|null}` to link or
unlink one observed payee. Cross-file rows, unobserved payees, deleted debts,
duplicates, and obligations outside a complete accepted schedule are rejected.
The association records evidence and its actor/date; it does not add a debt,
change financial figures, or modify an immutable version. Subsequent financial
saves retain the banking association.

Saved-PDF share links reuse the existing file-sharing service with only the
chosen immutable PDF selected. Creating a link does not send email. Staff can
also open or download a dated PDF directly from the saved-version receipt.

### Debt row controls

`POST …/financial-workspace/debts/actions` takes
`{family_id?, expected_revision, op, row_id, target_row_id?, file_id?, role?, payoff_amount?, payoff_date?}`
and returns the updated draft workspace.

`op` is `delete`, `restore`, `duplicate`, `link_document`, `unlink_document`, or
`select_payoff`; `role` is `agreement`, `payoff`, or `supporting`. A duplicate
points to another active row via `duplicate_of_id`. Removed/duplicate rows remain
in history and are excluded from published totals. Each row's `evidence_links`
identifies its documents and explicitly selected current payoff. These actions
stage changes until Save version. Missing payment/balance inputs stay unknown;
an incomplete total is not a reported zero.

### Contract terms and loan-specific documents

The debt schedule also carries `factor_rate`, `payment_amount`, and
`payment_frequency` (`daily`, `weekly`, `biweekly`, `monthly`). Factor rate is a
repayment multiplier, distinct from the annual interest percentage in `rate`.
Native payments produce a labelled monthly equivalent using the existing
business-day factors: 21, 4.33, 2.165, and 1 respectively, rounded to cents.
An unknown native amount or cadence does not establish a monthly payment.

`POST …/financial-workspace/debts/{row_id}/document-imports` takes
`{family_id?, expected_revision, file_id, role:"agreement"}` and returns a
reviewable field mapping scoped to that loan. `payoff` and `supporting` roles
are also supported. The source must be an uploaded PDF owned by the file or
explicitly selected through its supporting-file links. Unrelated clients'
documents are inaccessible.

The **From the file** view lists documents linked to the selected loan and
allows linking existing uploads, adding a PDF, previewing pages, and reading
contract terms. Previewing a mapping changes neither figures nor document
links. Applying selected mappings updates the draft and links the source to
that loan together; Save version remains the publication step. Existing values
and derived monthly-payment conflicts are shown for review. Missing terms
remain unknown, and a contract covering several indistinguishable loans is
not assigned arbitrarily. Stored version PDFs remain available separately.

## Batch file requests (staff and client To-do)

Prefix: `/api/v1/application-profiles/{profile_id}/file-requests`.

- `GET` lists request batches, canonical coverage, collection task state, each
  item's `next_reminder_at`, and email delivery receipts.
- `POST` takes `{request_id, notify:true, items:[{document_type, name, quantity,
  unit, subject, period, instructions}]}`. Unit is `files`, `months`, or `years`.
  Bounds are 100 files, 60 months, 10 years, and 30 items per batch.
- `POST /{request_id}/retry` takes `{retry_id}` and retries failed delivery.
- `POST /{request_id}/cancel` cancels the request while retaining uploaded files.

One batch creates shared requirements/tasks and optionally sends one combined
email. Requests commit before delivery, so a failed email does not erase client
To-dos. Canonical identities deduplicate equivalent subject/period requests;
accepted distinct coverage controls completion, not raw upload counts or old
versions. The secure room and signed-in client account read those same tasks.
Existing consent, communication-window, cadence, and retry limits apply.

## Deployment and legacy baseline adoption

Apply migrations after `0258_extra_requirements`: `0259_financial_workspace`
(file `0259_financial_workspace_versions.py`), then `0260_financial_debt_evidence`.
Deploy the updated backend before the workspace frontend. Saving requires an
application document room, the existing PDF rendering dependencies, and working
bucket/KMS storage permissions; PDF imports additionally require the configured
attachment/AI extraction provider. Baseline adoption itself uses no storage,
PDF rendering, email, or external AI calls.

From `qcbackend`, with the intended database configuration:

```bash
python -m scripts.backfill_financial_workspace --limit 100
python -m scripts.backfill_financial_workspace --profile-id <uuid>
python -m scripts.backfill_financial_workspace --apply --limit 100
python -m scripts.backfill_financial_workspace --apply --limit 100 --after-profile-id <next_cursor>
```

The default is read-only inventory. Review its JSON lines before using
`--apply`; no production backfill is run as part of deployment automatically.
Each applied profile commits independently through `resolve_family`; reruns
retain managed families. Eligible structured baselines and compatible analyzed
originals are adopted without fabricating PDFs or publishing draft edits.
Dealer-only debts acquire their existing profile anchor. Missing/ambiguous
owners or dates, distinct legacy owner/period identities, and original-only forms
that cannot be adopted safely report `requires_review`; map those originals into
explicit families in the workspace. Unmatched originals stay unchanged. The
summary's `next_cursor` resumes the next batch; failures return a nonzero exit.

## The templates (no login)

`GET /api/v1/public/financial-templates/{slug}.xlsx` —
`profit-and-loss` · `balance-sheet` · `business-debt-schedule` ·
`personal-financial-statement`. An Excel attachment generated from the same
schemas as the forms; `Cache-Control: public, max-age=86400`.

## Schemas (staff)

`GET /api/v1/application-profiles/financial-statements/schema` — the 413.

`GET /api/v1/application-profiles/financial-forms/schema/{kind}` — `p_and_l`
or `balance_sheet`:

```json
{
  "schema_version": "qc_pl.v1", "kind": "p_and_l",
  "header":   [{"key": "business_name", "label": "Business name", "input": "text"},
               {"key": "period_start", "label": "Period start", "input": "date"},
               {"key": "basis", "label": "Accounting basis", "input": "select", "options": ["cash", "accrual"]}],
  "sections": [{"key": "revenue", "label": "Revenue",
                "rows": [{"key": "gross_revenue", "label": "Gross revenue", "addback": false, "contra": false, "owner_comp": false, "hint": null}],
                "subtotal": {"key": "gross_profit", "label": "Gross profit"}}],
  "computed": [{"key": "net_income", "label": "Net income", "emphasis": true}],
  "collects_ssn": false
}
```

A body for either kind is
`{"schema_version", "header": {key: str|null}, "sections": {section_key: {row_key: str|null}}, "notes": ""}`.

## Where the four forms stand (staff)

`GET /api/v1/application-profiles/{profile_id}/financial-forms`

```json
{
  "forms": [
    {"kind": "pfs", "label": "Personal financial statement", "requested": true, "satisfied": true, "source": "filled", "net_worth": 280000.0, "figures_from": "form"},
    {"kind": "debt_schedule", "label": "Business debt schedule", "requested": true, "satisfied": false, "source": "none", "row_count": 0},
    {"kind": "p_and_l", "label": "Profit and loss statement", "requested": true, "satisfied": true, "source": "uploaded",
     "period_label": "Jan–Jun 2026", "net_income": 44600.0, "ebitda": 50600.0, "figures_from": "document"},
    {"kind": "balance_sheet", "label": "Balance sheet", "requested": true, "satisfied": false, "source": "none"}
  ],
  "packets": [
    {"packet_id": "…", "created_at": "…", "expires_at": "…", "completed_kinds": ["p_and_l", "pfs"], "revoked": false}
  ]
}
```

`source` is `filled` when someone typed it into our form, `uploaded` when the
analyzer recognised a document of that kind (for the two business statements:
recognised, never merely "the slot has a file" — Main Street asks for both on
one row), else `none`. `satisfied` is `source != "none"`.

## The two business statements (staff)

- `GET  …/{profile_id}/financial-forms/{kind}/body` → the body (business name
  seeded) plus `"status": "draft"|"submitted"|null` and `"statement_id"`.
- `PUT  …/{profile_id}/financial-forms/{kind}` `{body, submit}` →
  `{"statement_id", "status", "submitted": bool}`. With `submit`, the checklist
  row is ensured, the PDF is filed on it and the statement becomes
  `submitted`; later saves keep that status.
- `GET  …/{profile_id}/financial-forms/{kind}/pdf` — rendered from the latest
  body; 404 when nothing has been filled in.
- `POST …/{profile_id}/financial-forms/{kind}/request` — put the form on the
  checklist (idempotent).

The PFS keeps its by-id routes (`…/financial-statements/{statement_id}`) and
the debt schedule its `…/financial-forms/debt-schedule/body` and `PUT
…/financial-forms/debt-schedule`.

## Links (staff mint, borrower fills — no login)

`POST …/{profile_id}/financial-forms/{kind}/link` → `{"url", "expires_at"}`
for `pfs`, `debt_schedule`, `p_and_l`, `balance_sheet`. Tokens are hashed at
rest and expire in 30 days.

`POST …/{profile_id}/financial-forms/packet/link` →
`{"url": "{app}/forms/packet/{base}", "expires_at", "packet_id"}` — one link
that opens all four. It is four ordinary links whose tokens are
`{base}.pfs`, `{base}.debt_schedule`, `{base}.p_and_l`, `{base}.balance_sheet`,
sharing a `packet_id`. The four checklist rows are ensured silently at mint.
`POST …/{profile_id}/financial-forms/packets/{packet_id}/revoke` →
`{"revoked": true}` closes all four at once.

The borrower's endpoints, per token, identical for every kind:

- `GET  /api/v1/application-profiles/public/financial-forms/{token}` →
  `{"kind", "schema", "body", "completed", "business_name", "prefill"}`
- `POST …/public/financial-forms/{token}/draft`  `{body}` → `{"saved": true}`
- `POST …/public/financial-forms/{token}/submit` `{body}` → `{"completed": true}`

A closed, expired or unknown token answers 404 "This link is no longer
available" on every one of the three; a page holding a packet should treat any
non-ok save as that state.

## What a submit files

A submit renders the statement to PDF and stores it on the checklist row as a
`BucketFile` with a completed `BucketFileAnalysis` carrying `classification`
(`personal_financial_statement`, `debt_schedule`, `current_p_and_l`,
`balance_sheet`) and typed `key_facts` — the same shape the analyzer produces
for an upload, so every reader downstream treats a typed form and a read
document alike. One `document.received` timeline line per submit; one
`forms.packet_sent` line per packet minted.
