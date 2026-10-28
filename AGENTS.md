# Agent Instructions

- Human-facing replies are in Thai by default; keep code, commands and identifiers as-is.
- Never print, commit or copy secrets: database URLs, tokens and keys live in Railway
  variables and `~/.config/solo-offers/` (owner-only).
- Tests use a throwaway local PostgreSQL and a fake platform; they must never reach
  the hosted database or the hosted platform.
- Run `.venv/bin/pytest -q`, `.venv/bin/mypy .` and `uvx ruff@0.17.1 check . && uvx ruff@0.17.1 format --check .` before committing. Migrations are append-only: never edit
  an applied file, add a new numbered one.
- `src/cellkit` is this repository's own service code (copied from finance-subscriptions-api on
  2026-10-27; the shared generator was retired on 2026-10-12). Change it here; there is nothing
  upstream to sync from. Cell-specific code lives in `src/solo_offers/`.
- This is the Offer & Product service (EOS domain 4): the priced catalogue of what Solo Empire
  sells. Ids that point into another service (`project_id`, `goal_id`) are plain numbers, not
  foreign keys; resolve them through that service's API. `stream` is an income stream id from
  business-catalog `ventures/income-streams.yaml`.
- `offers-mcp` (stdio, see `.mcp.json`) is this cell's MCP server: tools `offers.<resource>.list|get|create|update|delete`
  call the cell API with the caller's token (`EMPIRE_TOKEN`, `EMPIRE_TOKEN_FILE` or the `owner-mcp` automation token;
  URL from `EMPIRE_OFFERS_API`). Set `EMPIRE_MCP_READONLY=1` for list/get only. Never add actor, workspace or token arguments.
- Merging to `main` deploys production: the CI `deploy` job uploads the checked-out commit with
  `railway up` after the CI and security jobs pass (the Railway GitHub source could not be connected),
  then smoke-tests `/health`. Verify one authorized read afterwards. Do not `railway up` from a
  working tree.
- Keep the solo-empire registry entry (`offers` in
  `repository-catalog/registries/online-systems.yaml`) in sync.
