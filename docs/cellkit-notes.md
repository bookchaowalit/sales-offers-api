# cellkit notes

`src/cellkit/` is this service's own framework code. It was copied on 2026-10-27 from
finance-subscriptions-api at `f92e1a0` (the Wave 2 copy: strict typing, `ranges`, column limits
and finite reals, Sentry capture on a failed inline publish). Nothing upstream is synced; other
cells keep their own copies (owner decision 2026-10-21).

`migrations/0001_offers.sql` and `db/roles.sql` were rendered from `cell.yaml` with
`cellkit.sqlgen.migration` / `cellkit.sqlgen.roles` before the first deploy.

## Fixes per need of this cell

| Fix | Does this cell need it? |
|---|---|
| Inline publish failure → Sentry | no: no resource has graph events, so the inline publisher never runs. |
| `ranges` rule | yes: `offer-price` valid_from..valid_to (`tests/test_offer_rules.py::test_a_price_cannot_stop_before_it_starts`) |
| Column limits / finite reals | yes: `offer.delivery_days`, `offer-price.amount` (contract suite) |

Change this code here, with a failing test first (ADR-0004).
