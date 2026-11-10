import time
import uuid
from typing import Any

import httpx


UNREACHABLE = "PayPal didn't respond. Check the connection and try again."


class PayPalError(Exception):
    def __init__(self, message: str, status_code: int = 502):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


class PayPalClient:
    def __init__(self, client_id: str, client_secret: str, base_url: str):
        self.client_id = client_id
        self.client_secret = client_secret
        self.base_url = base_url.rstrip("/")
        self._token = ""
        self._expires_at = 0.0

    def _access_token(self) -> str:
        if self._token and time.time() < self._expires_at:
            return self._token
        try:
            response = httpx.post(
                f"{self.base_url}/v1/oauth2/token",
                auth=(self.client_id, self.client_secret),
                data={"grant_type": "client_credentials"},
                headers={"Accept": "application/json", "Accept-Language": "en_US"},
                timeout=30.0,
            )
        except httpx.TransportError as exc:
            raise PayPalError(UNREACHABLE, 503) from exc
        body = _json_or_text(response)
        if response.status_code >= 400:
            raise PayPalError(f"PayPal login failed. {_message(body)}", response.status_code)
        self._token = body["access_token"]
        self._expires_at = time.time() + max(30, int(body.get("expires_in", 300)) - 60)
        return self._token

    def request(self, method: str, path: str, json_body: dict | None = None, request_id: str | None = None) -> Any:
        headers = {
            "Authorization": f"Bearer {self._access_token()}",
            "Accept": "application/json",
            "Content-Type": "application/json",
        }
        if request_id:
            headers["PayPal-Request-Id"] = request_id
        try:
            response = httpx.request(
                method,
                f"{self.base_url}{path}",
                headers=headers,
                json=json_body,
                timeout=30.0,
            )
        except httpx.TransportError as exc:
            raise PayPalError(UNREACHABLE, 503) from exc
        body = _json_or_text(response)
        if response.status_code >= 400:
            raise PayPalError(_message(body), response.status_code)
        if body == "" or body is None:
            return {}
        return body

    def create_invoice(self, payload: dict, request_id: str) -> str:
        body = self.request("POST", "/v2/invoicing/invoices", payload, request_id)
        invoice_id = _invoice_id(body)
        if not invoice_id:
            raise PayPalError("PayPal created an invoice but did not return an id.")
        return invoice_id

    def send_invoice(self, invoice_id: str) -> None:
        self.request(
            "POST",
            f"/v2/invoicing/invoices/{invoice_id}/send",
            {"send_to_recipient": True, "send_to_invoicer": True},
            str(uuid.uuid4()),
        )

    def get_invoice(self, invoice_id: str) -> dict:
        body = self.request("GET", f"/v2/invoicing/invoices/{invoice_id}")
        if not isinstance(body, dict):
            raise PayPalError("PayPal returned an unreadable invoice.")
        return body

    def delete_invoice(self, invoice_id: str) -> None:
        self.request("DELETE", f"/v2/invoicing/invoices/{invoice_id}")

    def create_order(self, payload: dict, request_id: str) -> dict:
        body = self.request("POST", "/v2/checkout/orders", payload, request_id)
        if not isinstance(body, dict) or not body.get("id"):
            raise PayPalError("PayPal did not return an order id.")
        return body

    def get_order(self, order_id: str) -> dict:
        body = self.request("GET", f"/v2/checkout/orders/{order_id}")
        if not isinstance(body, dict):
            raise PayPalError("PayPal returned an unreadable order.")
        return body

    def capture_order(self, order_id: str, request_id: str) -> dict:
        body = self.request("POST", f"/v2/checkout/orders/{order_id}/capture", {}, request_id)
        if not isinstance(body, dict):
            raise PayPalError("PayPal returned an unreadable capture.")
        return body


def money(cents: int) -> str:
    return f"{cents // 100}.{cents % 100:02d}"


def recipient_url(invoice: dict) -> str | None:
    metadata = (invoice.get("detail") or {}).get("metadata") or {}
    if metadata.get("recipient_view_url"):
        return metadata["recipient_view_url"]
    for link in invoice.get("links") or []:
        rel = (link.get("rel") or "").lower()
        if rel in {"payer-view", "recipient-view", "self"} and link.get("href") and "invoice" in link["href"]:
            if rel != "self":
                return link["href"]
    invoice_id = invoice.get("id")
    if invoice_id:
        return f"https://www.sandbox.paypal.com/invoice/p/#{invoice_id}"
    return None


def capture_id(order: dict) -> str | None:
    units = order.get("purchase_units") or []
    if not units:
        return None
    captures = ((units[0].get("payments") or {}).get("captures")) or []
    if not captures:
        return None
    return captures[0].get("id")


def _invoice_id(body: Any) -> str | None:
    if isinstance(body, dict):
        if body.get("id"):
            return str(body["id"])
        href = body.get("href") or ""
        if "/invoices/" in href:
            return href.rstrip("/").split("/")[-1]
    return None


def _json_or_text(response: httpx.Response) -> Any:
    if not response.content:
        return {}
    try:
        return response.json()
    except ValueError:
        return {"message": response.text[:500]}


def _message(body: Any) -> str:
    if not isinstance(body, dict):
        return str(body)[:500]
    details = body.get("details") or []
    if details and isinstance(details, list):
        first = details[0]
        issue = first.get("issue") or ""
        description = first.get("description") or ""
        field = first.get("field") or ""
        parts = [part for part in (issue, field, description) if part]
        if parts:
            return " — ".join(parts)
    for key in ("message", "error_description", "error"):
        if body.get(key):
            return str(body[key])
    return "PayPal rejected the call."
