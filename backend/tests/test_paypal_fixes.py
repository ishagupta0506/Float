import os
from datetime import date
from pathlib import Path

import httpx
import pytest

from app import service
from app.config import Settings, load_env_files
from app.db import init_db
from app.paypal_client import UNREACHABLE, PayPalClient, PayPalError
from app.service import approve_purchase, capture_order, create_order, create_purchase, get_state

TODAY = date(2026, 11, 2)
DEMO = (
    "I need 100 paper cups and 50 takeaway containers. My budget is $60. "
    "Prioritize environmentally friendly products and do not purchase anything without my approval."
)


def settings() -> Settings:
    return Settings(
        cafe_client_id="",
        cafe_client_secret="",
        cafe_merchant_email="",
        supplier_client_id="supplier-test-id",
        supplier_client_secret="supplier-test-secret",
        client_payer_email="",
        llm_api_key="",
        llm_base_url="https://example.invalid",
        llm_model="test",
        starting_cash_cents=10000,
        paypal_base_url="https://paypal.invalid",
    )


class FakeSupplier:
    def create_order(self, payload: dict, request_id: str) -> dict:
        return {"id": "ORDER-TEST-1", "status": "CREATED"}

    def get_order(self, order_id: str) -> dict:
        return {"id": order_id, "status": "CREATED"}

    def capture_order(self, order_id: str, request_id: str) -> dict:
        return {"id": order_id, "purchase_units": [{"payments": {"captures": [{"id": "CAPTURE-TEST-1"}]}}]}


def test_paid_cart_explains_the_payment_not_the_approval(tmp_path: Path, monkeypatch):
    monkeypatch.setattr(service, "_supplier", lambda _settings: FakeSupplier())
    path = tmp_path / "float.db"
    config = settings()
    init_db(path, 10000, "")
    purchase = create_purchase(path, config, DEMO, today=TODAY)["purchase"]
    assert purchase["status"] == "CLEAR"
    approve_purchase(path, config, purchase["id"], today=TODAY)
    order_id = create_order(path, config, purchase["id"], today=TODAY)["paypal_order_id"]

    state = capture_order(path, config, purchase["id"], order_id, today=TODAY)

    paid = state["purchase"]
    assert paid["status"] == "PAID"
    assert paid["paypal_capture_id"] == "CAPTURE-TEST-1"
    assert state["book_cash_cents"] == 4600
    assert paid["explanation"] == "Paid $54.00 to Roaster's Supply Co. Book cash is now $46.00."
    assert "approve" not in paid["explanation"].lower()
    assert get_state(path, config, today=TODAY)["purchase"]["explanation"] == paid["explanation"]


def test_env_file_beats_a_value_left_in_the_terminal(tmp_path: Path, monkeypatch):
    env_file = tmp_path / ".env"
    env_file.write_text("CAFE_MERCHANT_EMAIL=sb-new@business.example.com\n", encoding="utf-8")
    monkeypatch.setenv("CAFE_MERCHANT_EMAIL", "stale@gmail.com")

    load_env_files([env_file])

    assert os.environ["CAFE_MERCHANT_EMAIL"] == "sb-new@business.example.com"


def test_later_env_file_wins(tmp_path: Path, monkeypatch):
    backend_env = tmp_path / "backend.env"
    root_env = tmp_path / "root.env"
    backend_env.write_text("CAFE_MERCHANT_EMAIL=backend@business.example.com\n", encoding="utf-8")
    root_env.write_text("CAFE_MERCHANT_EMAIL=root@business.example.com\n", encoding="utf-8")
    monkeypatch.setenv("CAFE_MERCHANT_EMAIL", "stale@gmail.com")

    load_env_files([backend_env, root_env])

    assert os.environ["CAFE_MERCHANT_EMAIL"] == "root@business.example.com"


@pytest.mark.parametrize("failing", ["post", "request"])
def test_unreachable_paypal_is_a_clean_error(monkeypatch, failing):
    def drop(*_args, **_kwargs):
        raise httpx.ConnectError("[SSL: UNEXPECTED_EOF_WHILE_READING] EOF occurred in violation of protocol")

    client = PayPalClient("id", "secret", "https://paypal.invalid")
    if failing == "post":
        monkeypatch.setattr(httpx, "post", drop)
    else:
        client._token = "cached"
        client._expires_at = float("inf")
        monkeypatch.setattr(httpx, "request", drop)

    with pytest.raises(PayPalError) as raised:
        client.get_invoice("INV2-TEST")

    assert raised.value.message == UNREACHABLE
    assert raised.value.status_code == 503
