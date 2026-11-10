from datetime import date
from pathlib import Path

from app.config import Settings
from app.db import init_db
from app.service import FloatError, approve_purchase, create_order, create_purchase, get_state


def settings() -> Settings:
    return Settings(
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


DEMO = (
    "I need 100 paper cups and 50 takeaway containers. My budget is $60. "
    "Prioritize environmentally friendly products and do not purchase anything without my approval."
)


def test_cash_gate_blocks_the_order_call(tmp_path: Path):
    database = tmp_path / "float.db"
    config = settings()
    init_db(database, 2000, "")
    state = create_purchase(database, config, DEMO, today=date(2026, 11, 2))
    purchase = state["purchase"]
    assert purchase["status"] == "BLOCKED"
    assert purchase["total_cents"] == 5400
    assert purchase["invoice"]["customer_name"] == "Northwind Catering"
    try:
        approve_purchase(database, config, purchase["id"], today=date(2026, 11, 2))
        raised = False
    except FloatError as exc:
        raised = True
        assert exc.status_code == 409
    assert raised

    try:
        create_order(database, config, purchase["id"], today=date(2026, 11, 2))
        ordered = False
    except FloatError as exc:
        ordered = True
        assert exc.status_code == 409
    assert ordered
    assert get_state(database, config, today=date(2026, 11, 2))["book_cash_cents"] == 2000
