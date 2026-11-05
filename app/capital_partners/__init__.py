"""Capital Partners — isolated backend package (capital.qualifiedcommercial.com).

The loan-participation program for accredited private investors and family
offices, offered privately under SEC Rule 506(b). Everything for it lives
under app/capital_partners/: models (cp_* tables), enums, schemas, deps,
routers (/api/v1/capital/*), services. The touches outside this package are
deliberately few: the router includes in app/main.py, one model import in
app/models/__init__.py, the role and console plumbing that every external
console needs (Role.CAPITAL_PARTNER in app/enums.py, the ``capital`` console
in app/services/user_access.py, the API boundary in app/deps.py, the deny
list in app/scoping.py, the invite landing in app/routers/users.py, the URLs
in app/config.py) and alembic/versions/02xx_cp_*.py.

Two rules the code enforces rather than the UI:

* Nothing about an offering is visible before the relationship exists. A
  listing, its documents and the subscription form are served only to a
  member whose status is ``approved`` and whose waiting period has ended
  (``deps.require_approved_member``).
* The Form D clock starts when the first subscription becomes binding
  (``signed``), which is the SEC's "irrevocably committed" test, not when
  funds arrive (``services/filings.py``).
"""
