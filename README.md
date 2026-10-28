# sales-offers-api
The Offers cell: the priced catalogue of what Solo Empire sells (EOS domain 4, Offer & Product).
Its own API (Railway, serverless) and its own Neon database, made from `cell.yaml` on
2026-10-27 with the cellkit copy of finance-subscriptions-api. It reuses empire-platform for
identity, authorization, executions and audit over HTTP with the caller's token.

| | |
|---|---|
| API | Railway project `sales-offers-api`, service `offers-api` (Serverless) |
| Data | Neon project `sales-offers-api`, database `offers` |
| Registry | solo-empire `repository-catalog/registries/online-systems.yaml` (`offers`) |

## API

Every resource has list, stats, get, get-by-legacy-id, create, patch and delete
(see `cellkit/app.py`). Permissions are `offers.<resource>:read|write|delete`.

| Path | Resource | Filters |
|---|---|---|
| `/v1/offers` | offer: what a buyer can buy, per income stream | `status`, `stream`, `kind`, `owner_scope` |
| `/v1/offer-prices` | offer-price: one tier × currency × billing interval, with its payment link | `offer_id`, `currency`, `billing_interval`, `active` |
| `/v1/offer-listings` | offer-listing: where the offer is for sale | `offer_id`, `channel`, `status` |

An offer is **priced** when it has an active price and **listed** when it has a `live` listing;
the EOS value streams Idea-to-Launch ("List priced offer") and Order-to-Cash ("Priced offer")
read those two facts. Payment itself happens at the provider (Stripe Payment Links); the paid
event reaches `crm-income` through its Stripe webhook.

## Develop and operate

```bash
uv sync --frozen --all-extras          # Python 3.12 (.python-version): pgserver has no cp313 wheel yet
.venv/bin/pytest -q
.venv/bin/mypy . && uvx ruff@0.17.1 check . && uvx ruff@0.17.1 format --check .   # the same checks CI runs
NEON_API_KEY=... .venv/bin/offers-db provision
npm install && railway config apply   # service settings only; code deploys from GitHub (below)
railway scale --service offers-api asia-southeast1-eqsg3a=1 us-east4-eqdc4a=0   # next to Neon and the platform
```

Dependencies are locked in `uv.lock`; the image installs them with `uv sync --frozen`, so
production runs the versions CI tested. Change a dependency with `uv add`/`uv lock` and commit
the lock. Product scope and measures: [PRODUCT.md](PRODUCT.md).

Deploys: a merge to `main` deploys through the CI workflow's `deploy` job, which runs only after
the CI and security jobs pass: it uploads the checked-out commit with `railway up` (Railway CLI
5.71.0, project token `RAILWAY_TOKEN` scoped to `sales-offers-api`/production, kept in Infisical
`/railway` as `RAILWAY_TOKEN_SALES_OFFERS_API`) and smoke-tests `/health`. The job is on while the
repository variable `CD_DEPLOY` is `true`. Railway's own GitHub source is not connected: the
dashboard's repository list does not show this repository and the API refuses `serviceConnect`.
A failing check leaves production on the previous deployment. Roll back by redeploying an earlier
deployment in Railway.

Railway variables: `SENTRY_DSN` (error reporting; unset = off, see `src/cellkit/_sentry.py`),
`OFFERS_DB_MODE=hosted`, `OFFERS_DB_URL_API`, `OFFERS_JWT_PUBLIC_KEY_PEM`,
`OFFERS_JWT_ISSUER`, `EMPIRE_PLATFORM_URL`.
