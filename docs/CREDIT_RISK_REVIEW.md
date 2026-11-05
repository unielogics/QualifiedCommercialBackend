# Report Review And Provider Catalog

Linked reports produce deterministic, evidence-backed review flags. They do not
approve, decline, price, or automatically change a credit application.

- Past-due balances and current delinquency statuses are reviewed separately from historical late payments.
- Recent means a dated delinquency within 183 days of the report's original pull date. Account update dates never establish a late-payment date.
- Collections, bankruptcy records, charge-offs, repossessions, foreclosures, and utilization of 80% or more receive review flags with source fields. The utilization threshold is advisory, not a lending rule.
- Paid collections and historical/discharged bankruptcy records are not described as necessarily outstanding or active.
- Missing tradelines or dated payment history produce limitations, not a claim of clean credit.
- Scores remain unchanged; review findings are distinct from the verified-score indicator.

The algorithm uses only report facts, not names, addresses, or demographic proxies
to classify risk. Staff must verify accuracy, disputes and dispositions before
any lending decision. References: [CFPB report review checklist](https://files.consumerfinance.gov/f/documents/201701_cfpb_credit-report-review-checklist.pdf),
[CFPB: Understand your credit report](https://files.consumerfinance.gov/f/documents/cfpb_understand_your_credit_report_handout.pdf).

## Existing Provider Reports

The internal-staff link drawer can search active/archived applicants and browse
existing soft/hard reports through the configured server-side iSoftPull session.
Credentials and provider cookies never reach the browser. Brokers, partners and
clients cannot browse this provider-wide catalog.

Import requires explicit confirmation of identity and authorized file use. The
backend rechecks applicant/report membership and profile access, preserves the
verified original pull date, reuses imported report IDs, archives HTML, links the
owner and produces review findings through the shared import workflow. It never
visits provider new-report or refresh-pull routes. Provider access failures return
an actionable error without creating a credit inquiry.

The catalog is a bridge integration against the approved provider dashboard.
Changes to provider HTML or account permissions may require parser updates.
