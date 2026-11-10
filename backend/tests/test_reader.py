from datetime import date

import pytest

from app.agent import parse_mock, parse_request
from app.catalog import load_catalog
from app.config import Settings
from app.policy import build_cart, decide

TODAY = date(2026, 11, 2)
USER_EXAMPLE = (
    "I need 12 coffee bags and 6 milk cartons. Keep the total under $60 and don't let me pay "
    "the supplier until my catering invoice is settled."
)


def settings(**overrides) -> Settings:
    values = dict(
        cafe_client_id="",
        cafe_client_secret="",
        cafe_merchant_email="",
        supplier_client_id="",
        supplier_client_secret="",
        client_payer_email="",
        llm_api_key="",
        llm_base_url="https://example.invalid",
        llm_model="test",
        starting_cash_cents=2000,
        paypal_base_url="https://api-m.sandbox.paypal.com",
    )
    values.update(overrides)
    return Settings(**values)


def cart_for(text: str):
    parsed = parse_mock(text)
    return parsed, build_cart(load_catalog(), parsed)


def codes(cart) -> list[str]:
    return [issue.code for issue in cart.issues]


def test_user_example_extracts_items_budget_and_constraints():
    parsed = parse_mock(USER_EXAMPLE)
    assert [(item.quantity, item.product_query) for item in parsed.items] == [
        (12, "coffee bags"),
        (6, "milk cartons"),
    ]
    assert parsed.budget_cents == 6000
    assert parsed.hold_until_invoice_paid is True
    assert parsed.require_approval is True
    assert parsed.issues == []


def test_user_example_is_priced_from_the_catalog_and_kept_as_asked():
    _, cart = cart_for(USER_EXAMPLE)
    assert [
        (line.sku, line.pack_count, line.requested_units, line.unit_price_cents) for line in cart.lines
    ] == [("COF-1KG", 12, 12, 1800), ("MLK-12", 1, 6, 2200)]
    assert cart.total_cents == 23800

    decision = decide(2000, cart, [], TODAY)
    assert decision.status == "OVER_BUDGET"
    assert "$238.00" in decision.explanation
    assert "$60.00" in decision.explanation
    assert "$178.00 over" in decision.explanation
    assert [line.requested_units for line in decision.cart.lines] == [12, 6]


def test_ambiguous_product_asks_which_one():
    _, cart = cart_for("I need 10 bags")
    assert codes(cart) == ["ambiguous_product"]
    message = cart.issues[0].message
    for option in ("Recycled carrier bags", "House coffee beans", "Tea bags"):
        assert option in message
    assert cart.lines == []
    assert decide(2000, cart, [], TODAY).status == "REJECTED"


def test_request_without_a_quantity_is_not_guessed():
    _, cart = cart_for("I need some cups for the weekend")
    assert codes(cart) == ["no_items"]
    assert decide(2000, cart, [], TODAY).status == "REJECTED"


def test_unknown_product_names_the_problem():
    _, cart = cart_for("Please order 50 croissants")
    assert codes(cart) == ["unknown_product"]
    assert "croissants" in cart.issues[0].message


@pytest.mark.parametrize(
    "text, sku",
    [
        ("2 bags of coffee beans", "COF-1KG"),
        ("3 cartons of oat milk", "MLK-12"),
        ("2 bags of sugar", "SUG-1KG"),
        ("100 recycled paper cups", "CUP-R-50"),
    ],
)
def test_packaging_words_do_not_pick_the_product(text, sku):
    _, cart = cart_for(text)
    assert codes(cart) == []
    assert [line.sku for line in cart.lines] == [sku]


@pytest.mark.parametrize("text", ["4 coffee filters", "3 milk frothers", "2 cup holders", "5 gold cups"])
def test_one_shared_word_is_not_a_match(text):
    _, cart = cart_for(text)
    assert codes(cart) == ["uncertain_match"]
    assert cart.lines == []
    assert "isn't sure" in cart.issues[0].message


@pytest.mark.parametrize("text", ["-5 paper cups", "2.5 paper cups", "0 paper cups"])
def test_invalid_quantities_are_rejected(text):
    _, cart = cart_for(text)
    assert codes(cart) == ["invalid_quantity"]
    assert cart.lines == []


def test_oversized_quantity_is_rejected():
    _, cart = cart_for("20000 napkins")
    assert codes(cart) == ["quantity_too_large"]


def test_unknown_product_id_is_rejected():
    _, cart = cart_for("3 XYZ-999")
    assert codes(cart) == ["unknown_sku"]


def test_known_product_id_is_accepted():
    _, cart = cart_for("3 LID-R-50")
    assert [(line.sku, line.pack_count) for line in cart.lines] == [("LID-R-50", 1)]


def test_packs_are_counted_as_packs():
    _, cart = cart_for("2 packs of paper cups")
    assert [(line.sku, line.pack_count, line.requested_units) for line in cart.lines] == [("CUP-P-50", 2, 100)]


def test_repeated_products_are_merged():
    _, cart = cart_for("60 paper cups and 60 paper cups")
    assert [(line.sku, line.requested_units, line.pack_count) for line in cart.lines] == [("CUP-P-50", 120, 3)]


def test_compound_names_resolve_to_one_product():
    _, cart = cart_for("50 cup lids and 2 tea bags and 1 sugar")
    assert [line.sku for line in cart.lines] == ["LID-R-50", "TEA-50", "SUG-1KG"]


def test_non_dollar_budget_is_not_silently_dropped():
    parsed, cart = cart_for("100 paper cups, budget ₹5,000")
    assert parsed.budget_cents is None
    assert codes(cart) == ["unsupported_currency"]
    assert decide(2000, cart, [], TODAY).status == "REJECTED"


def test_budget_number_is_not_read_as_a_product():
    parsed, cart = cart_for("100 paper cups, under 60 dollars")
    assert parsed.budget_cents == 6000
    assert [line.sku for line in cart.lines] == ["CUP-P-50"]


def test_empty_request_explains_what_to_type():
    parsed = parse_request("   ", settings(), load_catalog())
    assert [issue.code for issue in parsed.issues] == ["empty_request"]


def test_ai_mode_follows_configuration():
    assert settings().ai_mode == "mock"
    assert settings(llm_api_key="key").ai_mode == "llm"
    assert settings(llm_api_key="key", ai_mode_setting="mock").ai_mode == "mock"
    forced = settings(ai_mode_setting="llm")
    assert forced.ai_mode == "mock"
    assert forced.ai_mode_warning
