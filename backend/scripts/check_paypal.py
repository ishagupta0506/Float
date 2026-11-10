"""Confirms both sandbox apps can do the calls the demo depends on.

Creates a draft invoice on the café app and deletes it, then creates an
unpaid order on the supplier app. Nothing is sent and nothing is charged.
"""

import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.config import load_settings  # noqa: E402
from app.paypal_client import PayPalClient, PayPalError  # noqa: E402


def main() -> int:
    settings = load_settings()
    failed = False

    if not settings.cafe_ready:
        print("cafe app: missing CAFE_PAYPAL_CLIENT_ID, CAFE_PAYPAL_CLIENT_SECRET, or CLIENT_PAYER_EMAIL")
        failed = True
    else:
        cafe = PayPalClient(settings.cafe_client_id, settings.cafe_client_secret, settings.paypal_base_url)
        invoicer = {"business_name": "Harbor and Rye Cafe"}
        if settings.cafe_merchant_email:
            invoicer["email_address"] = settings.cafe_merchant_email
        try:
            invoice_id = cafe.create_invoice(
                {
                    "detail": {"currency_code": "USD", "note": "Float credential check"},
                    "invoicer": invoicer,
                    "primary_recipients": [{"billing_info": {"email_address": settings.client_payer_email}}],
                    "items": [
                        {
                            "name": "Credential check",
                            "quantity": "1",
                            "unit_amount": {"currency_code": "USD", "value": "1.00"},
                        }
                    ],
                },
                str(uuid.uuid4()),
            )
            cafe.delete_invoice(invoice_id)
            print(f"cafe app: ok, created and deleted draft invoice {invoice_id}")
        except PayPalError as exc:
            print(f"cafe app: failed ({exc.status_code}) {exc.message}")
            failed = True

    if not settings.supplier_ready:
        print("supplier app: missing SUPPLIER_PAYPAL_CLIENT_ID or SUPPLIER_PAYPAL_CLIENT_SECRET")
        failed = True
    else:
        supplier = PayPalClient(
            settings.supplier_client_id, settings.supplier_client_secret, settings.paypal_base_url
        )
        try:
            order = supplier.create_order(
                {
                    "intent": "CAPTURE",
                    "purchase_units": [{"amount": {"currency_code": "USD", "value": "1.00"}}],
                },
                str(uuid.uuid4()),
            )
            print(f"supplier app: ok, created unpaid order {order['id']} ({order.get('status')})")
        except PayPalError as exc:
            print(f"supplier app: failed ({exc.status_code}) {exc.message}")
            failed = True

    if settings.cafe_ready and settings.supplier_ready and settings.cafe_client_id == settings.supplier_client_id:
        print("warning: the cafe and supplier use the same app, so the supplier payment would land in the cafe account")
        failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
