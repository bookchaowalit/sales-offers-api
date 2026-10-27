"""Shared helpers for the offers domain tests."""

import itertools
from typing import Any

OFFERS = "/v1/offers"
PRICES = "/v1/offer-prices"
LISTINGS = "/v1/offer-listings"

_names = itertools.count(1)


def create(client: Any, owner: dict[str, str], **fields: Any) -> Any:
    """An offer; the slug is unique per call unless one is given."""
    n = next(_names)
    body = {
        "slug": f"offer-{n}",
        "title": f"Offer {n}",
        "stream": "freelance-seo",
        **fields,
    }
    return client.post(OFFERS, headers=owner, json=body)


def price(client: Any, owner: dict[str, str], offer_id: int, **fields: Any) -> Any:
    body = {
        "offer_id": offer_id,
        "tier": f"Tier {next(_names)}",
        "amount": 6900.0,
        **fields,
    }
    return client.post(PRICES, headers=owner, json=body)


def listing(client: Any, owner: dict[str, str], offer_id: int, **fields: Any) -> Any:
    body = {"offer_id": offer_id, "channel": f"channel-{next(_names)}", **fields}
    return client.post(LISTINGS, headers=owner, json=body)
