from datetime import date

from app.catalog import load_catalog
from app.policy import InvoiceFacts, build_cart, decide
from app.agent import parse_rules

DEMO = (
    "I need 100 paper cups and 50 takeaway containers. My budget is $60. "
    "Prioritize environmentally friendly products and do not purchase anything without my approval."
)
TODAY = date(2026, 11, 2)


def sample_invoices():
    return [
        InvoiceFacts(1, "Northwind Catering", "Weekend catering bar", 8000, date(2026, 10, 24), "UNPAID", "covering"),
        InvoiceFacts(2, "Harbor Book Club", "Reading-night coffee", 1800, date(2026, 10, 31), "UNPAID", "small"),
        InvoiceFacts(3, "Maple Street Office", "Office breakfast", 4000, date(2026, 11, 14), "UNPAID", "future"),
        InvoiceFacts(4, "Sunday Market", "Stall fee", 2500, date(2026, 10, 18), "PAID", "history"),
    ]


def test_demo_request_parses():
    parsed = parse_rules(DEMO)
    assert [(item.quantity, item.product_query) for item in parsed.items] == [
        (100, "paper cups"),
        (50, "takeaway containers"),
    ]
    assert parsed.budget_cents == 6000
    assert parsed.prefer_recycled is True


def test_demo_cart_is_blocked_on_the_overdue_invoice():
    parsed = parse_rules(DEMO)
    cart = build_cart(load_catalog(), parsed)
    assert cart.total_cents == 5400
    assert [line.sku for line in cart.lines] == ["CUP-R-50", "BOX-R-25"]
    assert all(line.recycled for line in cart.lines)
    decision = decide(2000, cart, sample_invoices(), TODAY)
    assert decision.status == "BLOCKED"
    assert decision.gap_cents == 3400
    assert decision.invoice is not None
    assert decision.invoice.customer_name == "Northwind Catering"


def test_payment_opens_the_gate_for_the_same_cart():
    parsed = parse_rules(DEMO)
    cart = build_cart(load_catalog(), parsed)
    decision = decide(10000, cart, sample_invoices(), TODAY)
    assert decision.status == "CLEAR"
    assert decision.gap_cents == 0
    assert decision.invoice is None


def test_a_too_small_invoice_does_not_open_checkout():
    parsed = parse_rules(DEMO)
    cart = build_cart(load_catalog(), parsed)
    invoices = [invoice for invoice in sample_invoices() if invoice.customer_name != "Northwind Catering"]
    invoices = [invoice for invoice in invoices if invoice.customer_name != "Maple Street Office"]
    decision = decide(2000, cart, invoices, TODAY)
    assert decision.status == "BLOCKED"
    assert decision.invoice is None
    assert decision.gap_cents == 3400


def test_over_budget_cart_is_not_payable():
    text = "I need 100 paper cups and 50 takeaway containers. My budget is $30. Use recycled products."
    parsed = parse_rules(text)
    cart = build_cart(load_catalog(), parsed)
    decision = decide(2000, cart, sample_invoices(), TODAY)
    assert decision.status == "OVER_BUDGET"
    assert decision.invoice is None
    assert cart.total_cents == 5400


def test_plain_products_are_cheaper_when_recycled_is_not_requested():
    text = "I need 100 paper cups and 50 takeaway containers. My budget is $60."
    parsed = parse_rules(text)
    assert parsed.prefer_recycled is False
    cart = build_cart(load_catalog(), parsed)
    assert [line.sku for line in cart.lines] == ["CUP-P-50", "BOX-F-25"]
    assert cart.total_cents == 3400


def test_model_prices_are_not_part_of_the_cart():
    parsed = parse_rules(DEMO)
    cart = build_cart(load_catalog(), parsed)
    assert {line.unit_price_cents for line in cart.lines} == {1200, 1500}
