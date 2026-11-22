from copy import deepcopy
from uuid import UUID, uuid4

from world_engine.application.visibility import project_player_state


def _offer(listing_id, seller_id, account_id, *, active=True, price=125, minimum="1.5"):
    return {
        "listing_id": str(listing_id),
        "resource_id": "resource.wheat",
        "stock_account_id": str(account_id),
        "seller_id": str(seller_id),
        "unit_price_minor": price,
        "minimum_quantity": minimum,
        "active": active,
    }


def _state(*, observed_hidden_seller=False, hidden_market=False):
    actor_id = UUID(int=1)
    market_id = UUID(int=2)
    visible_seller_id = UUID(int=3)
    hidden_seller_id = UUID(int=4)
    inactive_seller_id = UUID(int=5)
    remote_seller_id = UUID(int=6)
    listing_z = UUID(int=90)
    listing_a = UUID(int=10)
    listing_inactive = UUID(int=20)
    listing_hidden = UUID(int=30)
    listing_dead_seller = UUID(int=40)
    listing_remote = UUID(int=50)

    entities = {
        str(actor_id): {
            "id": str(actor_id),
            "kind": "character",
            "name": "Player",
            "location_id": str(market_id),
            "status": "active",
            "components": {"needs": {"food": {"value": 80}}},
        },
        str(market_id): {
            "id": str(market_id),
            "kind": "place",
            "name": "Market",
            "status": "active",
            "components": {
                "hidden": hidden_market,
                "place": {"place_type": "settlement"},
                "market": {
                    "revision": 7,
                    "currency_resource_id": "core.coin",
                    "offers": [
                        _offer(listing_z, visible_seller_id, UUID(int=101), price=125, minimum="1.500"),
                        _offer(listing_a, visible_seller_id, UUID(int=102), price=200, minimum="2"),
                        _offer(listing_inactive, visible_seller_id, UUID(int=103), active=False),
                        _offer(listing_hidden, hidden_seller_id, UUID(int=104), price=999),
                        _offer(listing_dead_seller, inactive_seller_id, UUID(int=105), price=888),
                        _offer(listing_remote, remote_seller_id, UUID(int=106), price=777),
                    ],
                },
            },
        },
        str(visible_seller_id): {
            "id": str(visible_seller_id), "kind": "character", "name": "Merchant",
            "location_id": str(market_id), "status": "active", "components": {
                "hidden": False,
                "inventory": {"lots": [{"secret_stock_lot": "do not expose"}]},
                "public_role": "merchant",
            },
        },
        str(hidden_seller_id): {
            "id": str(hidden_seller_id), "kind": "character", "name": "Hidden merchant",
            "location_id": str(market_id), "status": "active", "components": {"hidden": True},
        },
        str(inactive_seller_id): {
            "id": str(inactive_seller_id), "kind": "character", "name": "Dead merchant",
            "location_id": str(market_id), "status": "dead", "components": {},
        },
        str(remote_seller_id): {
            "id": str(remote_seller_id), "kind": "character", "name": "Remote merchant",
            "location_id": str(UUID(int=7)), "status": "active", "components": {},
        },
    }
    knowledge = []
    if observed_hidden_seller:
        knowledge.append({
            "id": str(uuid4()),
            "owner_id": str(actor_id),
            "subject_id": str(hidden_seller_id),
            "kind": "observation",
            "claim": "I saw the merchant here.",
            "confidence": "1",
            "received_tick": 10,
        })
    state = {
        "current_tick": 10,
        "entities": entities,
        "resources": {
            "secret-account": {
                "resource_id": "resource.wheat", "balance": "0", "owner_id": str(visible_seller_id),
                "account_type": "holder",
            },
        },
        "knowledge": knowledge,
        "metadata": {
            "pack_id": "test.pack",
            "market_pricing": {"reference_price_minor": 20, "target_stock_quantity": "1000"},
            "pricing_audit": {"private": "do not expose"},
        },
        "calendar": {"day_seconds": 86400, "year_days": 360, "calendar_id": "core.standard"},
    }
    return state, actor_id, market_id, listing_a, listing_z, listing_hidden


def test_projection_exposes_only_local_posted_quotes_and_filters_hidden_sellers():
    state, actor_id, _market_id, listing_a, listing_z, listing_hidden = _state()
    before = deepcopy(state)

    view = project_player_state(state, str(actor_id))

    assert view["market_offers"] == {
        "currency_resource_id": "core.coin",
        "offers": [
            {
                "listing_id": str(listing_a),
                "resource_id": "resource.wheat",
                "unit_price_minor": "200",
                "minimum_quantity": "2",
            },
            {
                "listing_id": str(listing_z),
                "resource_id": "resource.wheat",
                "unit_price_minor": "125",
                "minimum_quantity": "1.5",
            },
        ],
    }
    assert all(set(offer) == {"listing_id", "resource_id", "unit_price_minor", "minimum_quantity"}
               for offer in view["market_offers"]["offers"])
    assert str(listing_hidden) not in {offer["listing_id"] for offer in view["market_offers"]["offers"]}
    rendered = str(view)
    for private_value in (
        "stock_account_id", "seller_id", "balance", "inventory", "secret_stock_lot",
        "market_pricing", "pricing_audit", "do not expose", "999", "888", "777",
    ):
        assert private_value not in rendered
    assert state == before
    assert "own_memberships" in view and "calendar" in view and "metadata" in view


def test_observed_hidden_seller_can_have_a_posted_quote_without_revealing_identity():
    state, actor_id, _market_id, _listing_a, _listing_z, listing_hidden = _state(
        observed_hidden_seller=True
    )

    quotes = project_player_state(state, str(actor_id))["market_offers"]

    hidden_quote = next(row for row in quotes["offers"] if row["listing_id"] == str(listing_hidden))
    assert hidden_quote == {
        "listing_id": str(listing_hidden),
        "resource_id": "resource.wheat",
        "unit_price_minor": "999",
        "minimum_quantity": "1.5",
    }
    assert "seller_id" not in hidden_quote


def test_no_valid_local_market_returns_empty_quotes_and_preserves_other_projection_fields():
    state, actor_id, market_id, *_ = _state()
    state["entities"][str(market_id)]["components"].pop("market")

    view = project_player_state(state, str(actor_id))

    assert view["market_offers"] == {"currency_resource_id": None, "offers": []}
    assert "entities" in view and "resources" in view and "calendar" in view


def test_hidden_unobserved_market_does_not_expose_its_quotes():
    state, actor_id, _market_id, *_ = _state(hidden_market=True)

    view = project_player_state(state, str(actor_id))

    assert view["market_offers"] == {"currency_resource_id": None, "offers": []}


def test_large_minor_unit_price_is_a_canonical_exact_integer_string():
    state, actor_id, market_id, _listing_a, _listing_z, _listing_hidden = _state()
    market = state["entities"][str(market_id)]["components"]["market"]
    large_price = 9_007_199_254_740_993
    market["offers"][0]["unit_price_minor"] = large_price

    quotes = project_player_state(state, str(actor_id))["market_offers"]["offers"]

    first_listing = min(quotes, key=lambda row: row["listing_id"])
    assert first_listing["unit_price_minor"] == "200"
    large_quote = next(row for row in quotes if row["unit_price_minor"] == str(large_price))
    assert large_quote["unit_price_minor"] == "9007199254740993"
