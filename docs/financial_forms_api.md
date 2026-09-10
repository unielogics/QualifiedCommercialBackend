# Financial forms API

Four financial forms live on a file (`ApplicationProfile`): the personal
financial statement (`pfs`, SBA Form 413), the business debt schedule
(`debt_schedule`), the profit and loss statement (`p_and_l`) and the balance
sheet (`balance_sheet`). Everything below is plain JSON over the existing API,
so a console, a mobile app or another platform reads the same thing. Money is
carried as the string the person typed; the server parses it.

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
