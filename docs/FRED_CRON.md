# FRED daily refresh — deployment notes

The dashboard "Today's Market Rates" widget reads from the `fred_observations`
table, which is populated by **`POST /api/v1/admin/fred/refresh`**. Run that
endpoint once each morning to pull the latest values from the St. Louis Fed
API into your RDS database.

## What gets pulled

The four "must-have commercial bases" defined in
`app/services/fred.py::SERIES_IDS`:

| Series | Description | Drives |
|---|---|---|
| `DGS10` | 10-Year Treasury | DSCR Rental (30-yr fixed) |
| `SOFR` | Secured Overnight Financing Rate | Bridge / floating |
| `DPRIME` | Bank Prime Loan Rate | Fix & Flip / Ground Up |
| `DGS5` | 5-Year Treasury | 5-year hybrid products |

Each pull asks for the last ~35 days of observations so we always have a
30-day window even after weekends or FRED holidays. Re-running on the same
day is **idempotent** — the upsert refreshes existing rows in place.

## Required configuration

Add to `qcbackend/.env`:

```
FRED_API_KEY=<your_key_from_https://fred.stlouisfed.org/docs/api/api_key.html>
```

Without a key, the refresh endpoint will 200 with an `errors` map for every
series; the dashboard will keep rendering whatever's already in the table.

## Wiring the daily cron

### Option A — AWS EventBridge (recommended for the hosted deploy)

```yaml
ScheduleExpression: cron(15 11 ? * MON-FRI *)   # 6:15 AM ET on weekdays
Target:
  HttpEndpoint: https://api.your-domain.com/api/v1/admin/fred/refresh
  HttpMethod: POST
  Headers:
    Authorization: Bearer <super_admin clerk JWT>
```

(The endpoint is gated by `require_role(Role.SUPER_ADMIN)`. Issue a
long-lived service-account JWT in Clerk and use it as the bearer token.)

### Option B — system cron on the API host

```cron
# m h dom mon dow  command
15 6 * * 1-5  curl -fsS -X POST -H "Authorization: Bearer $QC_FRED_TOKEN" \
              https://api.your-domain.com/api/v1/admin/fred/refresh
```

### Option C — manual trigger

Any super-admin can hit the endpoint from a shell:

```bash
curl -X POST http://localhost:8000/api/v1/admin/fred/refresh \
     -H "X-Dev-User: super@qc.com"
```

(`X-Dev-User` is the local-dev fallback when `CLERK_SECRET_KEY` is unset.)

## Verifying the pull worked

```bash
curl http://localhost:8000/api/v1/fred/series \
     -H "X-Dev-User: super@qc.com" | jq '.[0]'
```

You should see `current_value`, `current_date`, `history_7d`, `history_30d`
populated. The response includes the active `spread_bps` from the
`lender_spreads` table and the computed `estimated_rate` =
`current_value + spread_bps / 100`.

## Lender spreads

Default spreads are seeded by migration `0006_fred_observations.py` (DGS10:
215 bps, SOFR: 350 bps, DPRIME: 200 bps, DGS5: 250 bps). To change a
spread, super-admins click any rate card on the dashboard → "Edit spread"
in the modal. Each save inserts a new row in `lender_spreads`; the
most-recent row per `series_id` is the active spread. Historical rows are
the audit trail and are exposed at
`GET /api/v1/lender-spreads/{series_id}/history`.

## Tracking what's in the database

```sql
-- Latest observation per series
SELECT series_id, MAX(observation_date) AS latest, COUNT(*) AS rows
FROM fred_observations
GROUP BY series_id;

-- Active spread per series
SELECT DISTINCT ON (series_id) series_id, spread_bps, created_at, notes
FROM lender_spreads
ORDER BY series_id, created_at DESC;
```
