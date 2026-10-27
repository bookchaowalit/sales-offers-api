"""What the offers cell keeps and refuses: offers, their prices and their listings."""

import uuid

import pytest
from offers_helpers import LISTINGS, OFFERS, PRICES, create, listing, price

MISSING_ID = 987654321


# --- offer ------------------------------------------------------------------------------------


def test_new_offer_is_a_draft_productized_service_for_the_empire(client, owner):
    record = create(client, owner, slug="seo-report", title="SEO report").json()
    assert record["slug"] == "seo-report"
    assert record["status"] == "draft"
    assert record["kind"] == "productized_service"
    assert record["owner_scope"] == "solo_empire"
    assert record["delivery_days"] is None


@pytest.mark.parametrize("missing", ["slug", "title", "stream"])
def test_an_offer_needs_a_slug_a_title_and_a_stream(client, owner, missing):
    body = {"slug": "dev-sprint", "title": "Dev sprint", "stream": "freelance-dev"}
    del body[missing]
    response = client.post(OFFERS, headers=owner, json=body)
    assert response.status_code == 422, response.text


def test_the_slug_is_unique_in_a_workspace(client, owner):
    assert create(client, owner, slug="seo-audit").status_code == 201
    again = create(client, owner, slug="seo-audit")
    assert again.status_code == 409, again.text


def test_slug_title_and_stream_have_length_limits(client, owner):
    assert create(client, owner, slug="s" * 81).status_code == 422
    assert create(client, owner, title="t" * 201).status_code == 422
    assert create(client, owner, stream="x" * 81).status_code == 422
    assert create(client, owner, slug="s" * 80, title="t" * 200).status_code == 201


def test_status_and_kind_must_be_known_values(client, owner):
    assert create(client, owner, status="sold").status_code == 422
    assert create(client, owner, kind="gadget").status_code == 422
    for state in ("draft", "priced", "listed", "paused", "retired"):
        assert create(client, owner, status=state).status_code == 201, state
    for kind in (
        "service",
        "productized_service",
        "digital_product",
        "subscription",
        "course",
    ):
        assert create(client, owner, kind=kind).status_code == 201, kind


def test_delivery_days_cannot_be_negative(client, owner):
    assert create(client, owner, delivery_days=-1).status_code == 422
    assert create(client, owner, delivery_days=0).status_code == 201


def test_list_filters_offers_by_stream_and_status(client, owner):
    create(client, owner, stream="freelance-seo", status="listed")
    create(client, owner, stream="freelance-seo", status="draft")
    create(client, owner, stream="digital-templates", status="listed")
    found = client.get(
        OFFERS, headers=owner, params={"stream": "freelance-seo", "status": "listed"}
    )
    assert found.status_code == 200, found.text
    items = found.json()["items"]
    assert [(o["stream"], o["status"]) for o in items] == [("freelance-seo", "listed")]


# --- offer-price ------------------------------------------------------------------------------


def test_new_price_is_an_active_one_time_thb_stripe_price(client, owner):
    oid = create(client, owner).json()["id"]
    record = price(client, owner, oid, tier="Focused start", amount=6900).json()
    assert record["amount"] == 6900
    assert record["currency"] == "THB"
    assert record["billing_interval"] == "one_time"
    assert record["provider"] == "stripe"
    assert record["active"] is True


@pytest.mark.parametrize("missing", ["offer_id", "tier", "amount"])
def test_a_price_needs_an_offer_a_tier_and_an_amount(client, owner, missing):
    oid = create(client, owner).json()["id"]
    body = {"offer_id": oid, "tier": "Monthly", "amount": 14900.0}
    del body[missing]
    assert client.post(PRICES, headers=owner, json=body).status_code == 422


def test_a_price_amount_cannot_be_negative(client, owner):
    oid = create(client, owner).json()["id"]
    assert price(client, owner, oid, amount=-1).status_code == 422
    assert price(client, owner, oid, amount=0).status_code == 201


def test_a_price_must_point_at_an_existing_offer(client, owner):
    refused = price(client, owner, MISSING_ID)
    assert refused.status_code == 422, refused.text
    assert refused.json()["detail"] == {"error": "reference_not_found"}


def test_a_price_cannot_point_at_another_workspaces_offer(
    client, owner, harness, platform
):
    foreign = create(client, owner).json()
    stranger = uuid.uuid4()
    platform.grants[stranger] = set(harness.cell.permissions())
    other = harness.token(stranger, uuid.uuid4())
    assert price(client, other, foreign["id"]).status_code == 422


def test_one_price_per_tier_currency_and_interval(client, owner):
    oid = create(client, owner).json()["id"]
    first = price(client, owner, oid, tier="Growth", billing_interval="monthly")
    assert first.status_code == 201, first.text
    again = price(client, owner, oid, tier="Growth", billing_interval="monthly")
    assert again.status_code == 409, again.text
    usd = price(
        client, owner, oid, tier="Growth", billing_interval="monthly", currency="USD"
    )
    assert usd.status_code == 201, usd.text
    one_time = price(client, owner, oid, tier="Growth")
    assert one_time.status_code == 201, one_time.text


def test_billing_interval_must_be_a_known_interval(client, owner):
    oid = create(client, owner).json()["id"]
    assert price(client, owner, oid, billing_interval="weekly").status_code == 422
    for interval in ("one_time", "monthly", "quarterly", "yearly"):
        assert price(client, owner, oid, billing_interval=interval).status_code == 201


def test_a_price_cannot_stop_before_it_starts(client, owner):
    oid = create(client, owner).json()["id"]
    backwards = price(
        client, owner, oid, valid_from="2026-11-01", valid_to="2026-10-31"
    )
    assert backwards.status_code == 422, backwards.text
    assert backwards.json()["detail"] == {
        "error": "invalid_value",
        "fields": ["valid_from", "valid_to"],
    }


def test_active_prices_of_one_offer_can_be_listed(client, owner):
    oid = create(client, owner).json()["id"]
    price(client, owner, oid, tier="Old", active=False)
    price(client, owner, oid, tier="Current")
    found = client.get(
        PRICES, headers=owner, params={"offer_id": oid, "active": "true"}
    )
    assert found.status_code == 200, found.text
    assert [p["tier"] for p in found.json()["items"]] == ["Current"]


def test_payment_link_is_at_most_500_characters(client, owner):
    oid = create(client, owner).json()["id"]
    long_link = "https://buy.stripe.com/" + "x" * 480
    assert price(client, owner, oid, payment_link_url=long_link).status_code == 422
    link = "https://buy.stripe.com/test_abc"
    record = price(client, owner, oid, payment_link_url=link).json()
    assert record["payment_link_url"] == link


# --- offer-listing ----------------------------------------------------------------------------


def test_new_listing_is_a_draft(client, owner):
    oid = create(client, owner).json()["id"]
    record = listing(client, owner, oid, channel="website").json()
    assert record["status"] == "draft"
    assert record["listed_on"] is None


def test_a_listing_needs_an_offer_and_a_channel(client, owner):
    oid = create(client, owner).json()["id"]
    assert (
        client.post(LISTINGS, headers=owner, json={"offer_id": oid}).status_code == 422
    )
    assert (
        client.post(LISTINGS, headers=owner, json={"channel": "website"}).status_code
        == 422
    )


def test_one_listing_per_offer_and_channel(client, owner):
    oid = create(client, owner).json()["id"]
    assert listing(client, owner, oid, channel="website").status_code == 201
    assert listing(client, owner, oid, channel="website").status_code == 409


def test_listing_status_must_be_a_known_state(client, owner):
    oid = create(client, owner).json()["id"]
    assert listing(client, owner, oid, status="sold").status_code == 422
    for state in ("draft", "live", "paused", "removed"):
        assert listing(client, owner, oid, status=state).status_code == 201, state


def test_an_offer_with_prices_or_listings_cannot_be_deleted(client, owner):
    oid = create(client, owner).json()["id"]
    p = price(client, owner, oid).json()
    refused = client.delete(f"{OFFERS}/{oid}", headers=owner)
    assert refused.status_code == 409, refused.text
    assert refused.json()["detail"] == {"error": "has_dependents"}
    assert client.delete(f"{PRICES}/{p['id']}", headers=owner).status_code == 204
    item = listing(client, owner, oid).json()
    assert client.delete(f"{OFFERS}/{oid}", headers=owner).status_code == 409
    assert client.delete(f"{LISTINGS}/{item['id']}", headers=owner).status_code == 204
    assert client.delete(f"{OFFERS}/{oid}", headers=owner).status_code == 204
