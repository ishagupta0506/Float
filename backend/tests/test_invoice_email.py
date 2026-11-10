from datetime import date
from pathlib import Path

import pytest

from app import service
from app.config import Settings
from app.db import PLACEHOLDER_EMAIL, connect, init_db
from app.service import FloatError, create_purchase, send_invoice

TODAY = date(2026, 11, 2)
SANDBOX_BUYER = "sb-buyer12345@personal.example.com"
DEMO = (
    "I need 100 paper cups and 50 takeaway containers. My budget is $60. "
    "Prioritize environmentally friendly products and do not purchase anything without my approval."
)


def settings(payer: str) -> Settings:
    return Settings(
        cafe_client_id="cafe-test-id",
        cafe_client_secret="cafe-test-secret",
        cafe_merchant_email="",
        supplier_client_id="supplier-test-id",
        supplier_client_secret="supplier-test-secret",
        client_payer_email=payer,
        llm_api_key="",
        llm_base_url="https://example.invalid",
        llm_model="test",
        starting_cash_cents=2000,
        paypal_base_url="https://paypal.invalid",
    )


class FakeCafe:
    def __init__(self):
        self.recipients: list[str] = []
        self.sent: list[str] = []

    def create_invoice(self, payload: dict, request_id: str) -> str:
        self.recipients.append(payload["primary_recipients"][0]["billing_info"]["email_address"])
        return "INV2-TEST-0001"

    def send_invoice(self, invoice_id: str) -> None:
        self.sent.append(invoice_id)

    def get_invoice(self, invoice_id: str) -> dict:
        return {"id": invoice_id, "status": "UNPAID"}

    def delete_invoice(self, invoice_id: str) -> None:
        pass


@pytest.fixture()
def cafe(monkeypatch) -> FakeCafe:
    fake = FakeCafe()
    monkeypatch.setattr(service, "_cafe", lambda _settings: fake)
    return fake


def blocked_purchase(path: Path, config: Settings) -> dict:
    purchase = create_purchase(path, config, DEMO, today=TODAY)["purchase"]
    assert purchase["status"] == "BLOCKED"
    return purchase


def test_sandbox_personal_email_can_receive_the_invoice(tmp_path: Path, cafe: FakeCafe):
    path = tmp_path / "float.db"
    config = settings(SANDBOX_BUYER)
    init_db(path, 2000, SANDBOX_BUYER)
    purchase = blocked_purchase(path, config)

    invoice_id = purchase["invoice"]["id"]
    state = send_invoice(path, config, invoice_id, today=TODAY)

    assert cafe.recipients == [SANDBOX_BUYER]
    assert cafe.sent == ["INV2-TEST-0001"]
    invoice = next(row for row in state["invoices"] if row["id"] == invoice_id)
    assert invoice["paypal_invoice_id"] == "INV2-TEST-0001"
    assert invoice["status"] == "UNPAID"
    assert state["purchase"]["status"] == "BLOCKED"


def test_placeholder_email_is_still_refused(tmp_path: Path, cafe: FakeCafe):
    path = tmp_path / "float.db"
    init_db(path, 2000, "")
    config = settings(SANDBOX_BUYER)
    purchase = blocked_purchase(path, config)

    with pytest.raises(FloatError) as raised:
        send_invoice(path, config, purchase["invoice"]["id"], today=TODAY)

    assert raised.value.status_code == 400
    assert cafe.recipients == []
    assert cafe.sent == []


def test_startup_replaces_only_the_placeholder_email(tmp_path: Path):
    path = tmp_path / "float.db"
    init_db(path, 2000, "")
    init_db(path, 2000, SANDBOX_BUYER)
    init_db(path, 2000, "someone-else@personal.example.com")

    conn = connect(path)
    try:
        emails = {row["customer_email"] for row in conn.execute("SELECT customer_email FROM invoices")}
    finally:
        conn.close()
    assert emails == {SANDBOX_BUYER}
    assert PLACEHOLDER_EMAIL not in emails
