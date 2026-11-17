# Referral partner access

The former Auto Dealer Agent experience now serves cross-program referrals.
Stored role values are retained so existing users, signatures, company links,
and file ownership remain valid. This change does not migrate production data.

| Access role | New invitations | Program and record scope |
| --- | --- | --- |
| Referral Partner (`dealer_partner`) | Yes, default | Active catalog programs; own assigned intake files, clients derived from those files, rooms, and partner messages. |
| Legacy professional (`professional_referral_partner`) | No separate option | Same referral experience, retaining existing approved company visibility for foreclosure matters. Other program files remain individually owned. |
| Real Estate Originator (`broker`) | Yes | Existing assigned client/loan pipeline and property analysis tools, plus common referral intake. |
| Regional Manager | Yes | Existing assigned-agent portfolio; no Field Desk or global account administration. |
| Underwriter | Yes | Existing firm-wide funding and underwriting; no team or global bucket administration. |
| Field Representative | Yes | Existing owner-scoped Field Desk/Audit; additional Funding entry does not grant a general funding book. |
| Super Admin | Yes | Existing firm-wide administration. |
| Client, Dealer Client, Lender, Vendor, Capital Partner | Dedicated onboarding | Their existing product/package/room boundaries; no generic firm-wide client or loan visibility. |

## Fixed audit findings

- Removed dealer-variant predicates from partner intake list/detail, messaging,
  assignment, operator-file projection, and application-profile ownership checks.
- Reused the common file creation implementation for the correct application,
  checklist, secure PIN, and consent-aware invitation delivery.
- Replaced the static dealer program carousel with the active catalog and its
  configured routes. Saved the requested canonical program on the intake and
  room context, and show its name on the file board. This is a client request;
  it does not override underwriting's final program decision.
- Added an owner-scoped client directory without granting access to unrelated
  account-wide financial profiles, loans, or internal reporting.
- Removed another partner's file ID from duplicate-client errors. Partners
  cannot supply a different owner or force a duplicate through the shared API.
- Retained the separate foreclosure form for its legal/property details and
  contact suppression. New referral accounts see only their own rescues;
  legacy professional accounts retain their existing approved firm scope.
- Required signed individual and company agreements in the rescue gate.
- Made client/loan/analysis query defaults deny unrecognized and thin roles.
- Added inherited Funding console access for both referral role keys, without
  granting Field Desk, Audit, or internal underwriting privileges.
- Kept closed files visible on the referral board; explicit archives remain
  separate. The board and client directory load beyond the first 100 files.

Provider-specific `arf_*` channel configurations retain the existing hosted
intake authorization boundary and are not exposed as general partner products.
An active program with no configured intake route is shown with a configuration
notice rather than silently creating the wrong application type.

## Validation

- Backend regression tests cover cross-program creation, role and ownership
  restrictions, signature checks, route compatibility, regional access,
  invitations, required business details, and business-purpose safeguards.
- Frontend role tests cover the consolidated invitation choices, legacy role
  compatibility, default role, and the distinction between referral access and
  real-estate origination.
- The integrated release passed 344 targeted backend tests and changed-file
  Ruff checks. Dashboard CI passed its complete test, type, lint, and build gates.
- Backend production deployment is gated on the complete CI suite for the
  exact deployment commit, followed by the server readiness check.
