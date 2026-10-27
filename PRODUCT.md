# Product

<!-- impeccable:product-schema 1 -->

## Platform

api

## Users

- The Solo Empire owner, through the cell's own MCP server (`offers-mcp`) and the hosted tool
  hub (`platform-mcp-remote-worker`).
- Automations with an `empire-platform` token, within the permissions
  `offers.offer:read|write|delete`, `offers.offer-price:read|write|delete` and
  `offers.offer-listing:read|write|delete`.
- `crm-income` deals and invoices name an offer by its `slug` (text, not a foreign key).

## Product Purpose

The one place that says what Solo Empire sells, at what price, how a buyer pays, and where it is
for sale. It closes the EOS gap "Offer & Product" (no system) and the broken value-stream steps
"Priced offer" (Order-to-Cash) and "List priced offer" (Idea-to-Launch).

Success: every offer shown to a buyer has an active price with a payment link and at least one
live listing; nothing is sold from a price that is not in this catalogue.

## Positioning

One capability, one service: its own API (Railway), its own database (Neon) with row-level
security per workspace, and authorization, executions and audit through `empire-platform`.

## Operating Context

- Production: Railway `offers-api` (`/health`), deployed by merging to `main` after CI.
- Monitored by `platform-uptime-worker`; errors go to Sentry when `SENTRY_DSN` is set.

## Capabilities and Constraints

- Offer: `slug` (unique, at most 80), `title` (at most 200) and `stream` (at most 80) are
  required; `kind` is service, productized_service, digital_product, subscription or course
  (default productized_service); `status` is draft, priced, listed, paused or retired (default
  draft); `delivery_days` is not negative.
- Price: `offer_id` (an offer of the same workspace), `tier` and `amount` (not negative) are
  required; one price per offer, tier, currency and billing interval (409); currency defaults to
  THB, `billing_interval` one_time, `provider` stripe, `active` true; `valid_to` cannot be
  before `valid_from`.
- Listing: `offer_id` and `channel` are required; one listing per offer and channel (409);
  `status` is draft, live, paused or removed (default draft).
- An offer that still has prices or listings cannot be deleted (409 `has_dependents`).
- Out of scope: taking payment, issuing invoices and receipts (provider and `crm-income`).

## Measures

- Offers with status listed but no active price (target: 0).
- Active prices without a payment link (target: 0).
- Offers per income stream with a live listing.
- 5xx rate and p95 latency on `/v1/offers` (uptime worker and Sentry).
