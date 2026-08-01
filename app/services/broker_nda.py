"""Dealer-partner ("broker") non-disclosure / non-solicitation agreement.

Signed once per broker account, hard-blocking platform access until done
(see `_require_nda_signed` in dealer_ai_intake.py). Reuses the exact
evidentiary primitives already proven out for client e-signatures —
`document_hash` / `render_signature_certificate_pdf` from
document_signature.py, and the S3/decode helpers from
payment_authorization.py — rather than duplicating them. The only new
pieces here are the agreement text/version and the S3 key convention for a
user-scoped (not bucket-scoped) signature.

LEGAL NOTE: The agreement text below was drafted by an engineer from the
business's plain-English requirements, not by counsel. It is flagged
TODO(legal-review) and must be reviewed by an attorney before being relied
on in an actual dispute. Bump BROKER_NDA_DOCUMENT_VERSION whenever the text
changes so every signature stays tied to the exact version the signer saw.
"""

from __future__ import annotations

from app.services.document_signature import document_hash, render_signature_certificate_pdf  # noqa: F401 re-exported for callers
from app.services.payment_authorization import (  # noqa: F401 re-exported for callers
    client_ip,
    decode_signature_data_url,
    presign_private_s3_object,
    put_private_s3_object,
)

BROKER_NDA_DOCUMENT_VERSION = "2026-07-31-1"

COMPANY_LEGAL_NAME = "Qualified Commercial LLC"


# TODO(legal-review): drafted from business requirements, not by counsel.
# Must be reviewed by an attorney before being relied on in a dispute.
def broker_nda_document_text() -> str:
    return f"""
{COMPANY_LEGAL_NAME} Dealer Partner Non-Disclosure and Non-Solicitation Agreement

This Agreement is entered into between {COMPANY_LEGAL_NAME} ("QC", "Company") and the
individual or entity identified below ("Partner", "you") in connection with Partner's
access to the QC platform as a dealer partner / broker.

1. Confidential Information. Partner acknowledges that in the course of using the QC
platform, Partner will have access to QC's proprietary business model, underwriting
processes, technology, pricing, and its relationships with banks, lenders, and other
capital sources (collectively, "Confidential Information"). Partner agrees to hold all
Confidential Information in strict confidence and not to disclose it to any third party,
except as required by law.

2. Non-Solicitation and Non-Circumvention. Partner agrees not to use Confidential
Information to build, operate, or assist a competing brokerage, lending, or
underwriting business modeled on QC's business, processes, or technology. Any
transaction, communication, or relationship Partner has with a bank, lender, or capital
source that Partner is introduced to, or otherwise engages through, the QC platform must
be processed exclusively through QC's brokerage/fintech entity or its designated
executives. Partner will not contact, negotiate with, or transact directly with any such
bank, lender, or capital source outside of the QC platform in connection with any deal
originated on or through the platform.

3. Prior Relationships Disclosure. Partner may disclose, at the time of signing this
Agreement, any pre-existing relationships with lenders, dealers, or other parties that
Partner wishes to exclude from the scope of Section 2. Any relationship not disclosed at
signing is presumed to be within the scope of this Agreement. QC reserves the right to
dispute the scope or validity of any disclosed relationship.

4. Term and Survival. This Agreement is effective immediately upon signature and remains
in effect for the duration of Partner's use of the QC platform. The obligations in
Sections 1 and 2 survive for two (2) years following the termination of Partner's access
to the QC platform or the end of Partner's relationship with QC, whichever occurs later.

5. Remedies. Partner acknowledges that a breach of this Agreement may cause QC
irreparable harm for which monetary damages alone may be an inadequate remedy, and that
QC is entitled to seek injunctive relief in addition to any other remedies available at
law or in equity.

6. Electronic Signature. Partner consents to use electronic records and electronic
signatures under the U.S. E-SIGN Act and UETA. Partner understands that their typed
legal name, checkbox acknowledgment, drawn signature, any prior-relationships disclosure
submitted, timestamp, IP address, and device/browser information will be retained by QC
as evidence of this Agreement and may be used in connection with any dispute arising
from it. Partner may request a copy of this signed record at any time.
""".strip()
