# Architecture

Locked-in constraints. Do not change without explicit user approval.

## Stack

| Surface | Stack |
|---|---|
| Backend | FastAPI + PostgreSQL 16 + pgvector |
| Desktop | Next.js 14 (App Router) + TypeScript + Tailwind |
| Mobile | Expo SDK 51+ + React Native + TypeScript + expo-router |

## Architecture constraints

1. **Vector store = Postgres + pgvector** (NOT Pinecone). Same `vector_store.log_event(deal_id, ...)` interface. Runs in the same Docker compose as the relational DB.
2. **AI orchestration = native Anthropic tool-use** (NOT LangChain). Plain Python functions registered with the SDK. RAG is a thin custom layer.
3. **Models = `claude-sonnet-4-6` (heavy) + `claude-haiku-4-5` (light)** with prompt caching enabled from day one. NO multi-provider router.
4. **Email = local fake inbox first**, real Gmail Pub/Sub last. Build deal-ID regex + parser + air-gap routing logic against a mock inbox.
5. **Auth = Clerk** (NOT roll-our-own JWT). Roles: `super_admin`, `broker`, `loan_exec`, `client`.
6. **Enums single source of truth = backend `enums.py`** → codegen TypeScript types via `datamodel-code-generator`. Both frontends import from generated `lib/enums.generated.ts`.
7. **Tooling = `uv` (Python) + `pnpm` (Node).** No poetry, no npm.
8. **Broker points = schema only this pass.** Award/clawback rules pending business clarification.
9. **Prod target = EC2 + Caddy (backend) + Amplify (desktop) + EAS (mobile).** Backend runs as a Docker container on a single t4g.medium EC2 with Caddy in front for auto-TLS via Let's Encrypt. Image lives in GHCR; deploys via GitHub Actions OIDC → SSM rolling restart. Desktop SSR Next.js on AWS Amplify Hosting wired to the QCDashboard repo.
10. **Repo strategy = three separate GitHub repos, deploy keys per repo.** `unielogics/QualifiedCommercialBackend`, `unielogics/QCDashboard`, `unielogics/QCMobile`. No monorepo / workspaces.

## Module → Implementation map

| Spec module | Backend home | Frontend home |
|---|---|---|
| **M1** Center of Truth + RAG | `services/ai/vector_store.py`, `services/ai/orchestrator.py` | AIRail (desktop), IntakeScreen (mobile) |
| **M2** Gmail Pub/Sub + air-gap | `services/email/`, `routers/webhooks.py` | Messages (desktop), LoanFile Chat tab (mobile) |
| **M3** Broker Desktop 3-col | — | `components/deal-control-room/` |
| **M4** Mobile app | — | All of `qcmobile/` |
| **M5** RBAC | `deps.py`, role decorators | Sidebar role gate, route guards |
| **M7** Dropdowns + limits | `enums.py`, `constants.py`, `services/lender_matrix.py` | `lib/enums.generated.ts` |
| **M8** Pricing slider | `services/math/pricing.py`, `/loans/{id}/recalc` | Simulator (mobile) + HUD sim (DCR) |
| **M9** Math engine | `services/math/{amortization,dscr,interest_only}.py` | called via `/recalc` |
| **M10** HUD-1 fee map | `services/hud_template.py` | HUD-1 tab in Loan Detail |

## Pipeline stages (canonical, 6)

`Prequalified → Collecting Docs → Lender Connected → Processing → Closing → Funded`

## Roles & visibility

- **super_admin** — everything, including Rewards leaderboard and Settings
- **broker** (AE) — operator screens for assigned clients/loans only
- **loan_exec** (UW) — operator screens, no Rewards
- **client** — Dashboard / My Loans / Documents / Messages / Calendar — strictly own data
