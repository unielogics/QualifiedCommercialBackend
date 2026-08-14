"""Dealer Capital OS — isolated backend package (audit.qualifiedcommercial.com).

Everything for this product lives under app/dealer_os/: models (dos_* tables),
schemas, deps, router (/api/v1/dealer-os/*), services, providers. The ONLY
touches outside this package are the router include in app/main.py, one model
import in app/models/__init__.py, and alembic/versions/01xx_dos_*.py files.
See the v5 build plan's Backend Isolation Contract.
"""
