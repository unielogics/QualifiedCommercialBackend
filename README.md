# qcbackend

FastAPI backend for Qualified Commercial. The Center of Truth.

See `../docs/ARCHITECTURE.md` for locked-in constraints.

## Local dev

```bash
python -m uv sync                          # install deps from pyproject.toml
docker compose up -d                       # postgres + pgvector
python -m uv run alembic upgrade head      # apply migrations
python -m uv run python -m app.seed        # load design seed data
python -m uv run pytest                    # math engine tests
python -m uv run uvicorn app.main:app --reload
```

API docs at http://localhost:8000/docs

## Layout

```
app/
├── main.py              # FastAPI app
├── config.py            # pydantic-settings
├── db.py                # async SQLAlchemy session
├── deps.py              # auth / role gate
├── enums.py             # Module 7 dropdowns (single source of truth)
├── constants.py         # Module 7 limits (DSCR_MAX_LTV_PURCHASE, etc.)
├── models/              # SQLAlchemy ORM
├── schemas/             # Pydantic request/response
├── routers/             # /api/v1/*
├── services/
│   ├── math/            # Module 9 — amortization, DSCR, IO, pricing
│   ├── hud_template.py  # Module 10
│   ├── lender_matrix.py # Module 7 validators
│   ├── ai/              # Module 1 — orchestrator, tools, pgvector store
│   ├── email/           # Module 2 — fake inbox + Gmail Pub/Sub
│   ├── softpull/        # iSoftpull
│   ├── property/        # RentCast
│   ├── storage/         # S3
│   └── ocr/             # HUD/lease/LLC parser
├── ws.py                # WebSocket per-deal channels
└── tests/
```
