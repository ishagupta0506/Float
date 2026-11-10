import hashlib
import json
import secrets
import uuid
from dataclasses import asdict
from datetime import date, datetime
from pathlib import Path

from app.agent import LlmCall, parse_request
from app.catalog import load_catalog
from app.config import Settings
from app.db import LOCK, PLACEHOLDER_EMAIL, connect
from app.paypal_client import PayPalClient, PayPalError, capture_id, money as paypal_money, recipient_url
from app.policy import (
    PAID_STATUSES,
    CartLine,
    InvoiceFacts,
    ParsedRequest,
    build_cart,
    days_overdue,
    decide,
    money,
)


class FloatError(Exception):
    def __init__(self, message: str, status_code: int = 400):
        super().__init__(message)
        self.message = message
        self.status_code = status_code


def get_state(path: Path, settings: Settings, today: date | None = None) -> dict:
    today = today or date.today()
    with LOCK:
        conn = connect(path)
        try:
            warning = _refresh_locked(conn, settings)
            _reconcile_locked(conn, today)
            payload = _payload_locked(conn, settings, today, warning)
            conn.commit()
            return payload
        finally:
            conn.close()


def create_purchase(
    path: Path,
    settings: Settings,
    text: str,
    today: date | None = None,
    llm: LlmCall | None = None,
) -> dict:
    today = today or date.today()
    catalog = load_catalog()
    parsed = parse_request(text, settings, catalog, llm=llm)
    cart = build_cart(catalog, parsed)
    with LOCK:
        conn = connect(path)
        try:
            warning = _refresh_locked(conn, settings)
            state = conn.execute("SELECT * FROM app_state WHERE id = 1").fetchone()
            book = _book_cash(state)
            invoices = [_facts(row) for row in _invoice_rows(conn)]
            decision = decide(book, cart, invoices, today, parsed.ignored_model_price)
            if parsed.parser == "llm":
                decision.audits.append(
                    "The model read the request. Float checked each product against the catalog and priced the cart itself."
                )
            else:
                decision.audits.append("The mock reader parsed the request. No model was called.")
            decision.audits.extend(parsed.notes)
            if parsed.hold_until_invoice_paid:
                decision.audits.append(
                    "You asked to hold the supplier payment until an invoice is paid. "
                    "Float never pays the supplier without your approval of this exact cart."
                )

            shift_id = state["shift_id"]
            cursor = conn.execute(
                """
                INSERT INTO purchases (
                    shift_id, request_text, status, total_cents, budget_cents, supplier,
                    explanation, invoice_id, cart_hash, parser, parsed_json, issues_json
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    shift_id,
                    text.strip(),
                    decision.status,
                    cart.total_cents if cart.lines else None,
                    cart.budget_cents,
                    cart.supplier,
                    decision.explanation,
                    decision.invoice.id if decision.invoice else None,
                    _hash_cart(cart.lines, cart.total_cents, decision.invoice.id if decision.invoice else None),
                    parsed.parser,
                    json.dumps(_parsed_dict(parsed)),
                    json.dumps([asdict(issue) for issue in cart.issues]),
                ),
            )
            purchase_id = cursor.lastrowid
            for line in cart.lines:
                conn.execute(
                    """
                    INSERT INTO cart_lines (
                        purchase_id, sku, name, pack_count, pack_size, units_covered,
                        requested_units, unit_price_cents, line_total_cents, recycled, supplier
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        purchase_id,
                        line.sku,
                        line.name,
                        line.pack_count,
                        line.pack_size,
                        line.units_covered,
                        line.requested_units,
                        line.unit_price_cents,
                        line.line_total_cents,
                        1 if line.recycled else 0,
                        line.supplier,
                    ),
                )
            for detail in decision.audits:
                conn.execute("INSERT INTO audits (purchase_id, detail) VALUES (?, ?)", (purchase_id, detail))
            conn.commit()
            return _payload_locked(conn, settings, today, warning)
        finally:
            conn.close()


def send_invoice(path: Path, settings: Settings, invoice_id: int, today: date | None = None) -> dict:
    today = today or date.today()
    if not settings.cafe_ready:
        raise FloatError(
            "Set CAFE_PAYPAL_CLIENT_ID, CAFE_PAYPAL_CLIENT_SECRET, and CLIENT_PAYER_EMAIL in .env before sending an invoice.",
            400,
        )
    with LOCK:
        conn = connect(path)
        try:
            purchase = _current_purchase(conn)
            if purchase is None or purchase["invoice_id"] != invoice_id:
                raise FloatError("Send the invoice this cart is waiting on.", 409)
            if purchase["status"] == "PAID":
                raise FloatError("This cart is already paid.", 409)
            invoice = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
            if invoice is None:
                raise FloatError("Invoice not found.", 404)
            if invoice["status"] in PAID_STATUSES:
                raise FloatError("That invoice is already paid.", 409)
            if invoice["customer_email"].strip().lower() == PLACEHOLDER_EMAIL:
                raise FloatError("CLIENT_PAYER_EMAIL is not a sandbox buyer yet.", 400)

            cafe = _cafe(settings)
            note = _note(conn, purchase["id"])
            paypal_id = invoice["paypal_invoice_id"]
            if paypal_id and invoice["status"] == "DRAFT":
                try:
                    cafe.delete_invoice(paypal_id)
                except PayPalError:
                    pass
                paypal_id = None
                conn.execute(
                    "UPDATE invoices SET paypal_invoice_id = NULL, recipient_url = NULL WHERE id = ?",
                    (invoice_id,),
                )

            if not paypal_id:
                request_id = str(uuid.uuid4())
                payload = _invoice_payload(invoice, note, settings, include_dates=True)
                try:
                    paypal_id = cafe.create_invoice(payload, request_id)
                except PayPalError:
                    request_id = str(uuid.uuid4())
                    payload = _invoice_payload(invoice, note, settings, include_dates=False)
                    paypal_id = cafe.create_invoice(payload, request_id)
                conn.execute(
                    """
                    UPDATE invoices
                    SET paypal_invoice_id = ?, paypal_request_id = ?, note = ?, status = 'DRAFT'
                    WHERE id = ?
                    """,
                    (paypal_id, request_id, note, invoice_id),
                )
                conn.commit()

            current = conn.execute("SELECT * FROM invoices WHERE id = ?", (invoice_id,)).fetchone()
            if current["status"] == "DRAFT":
                cafe.send_invoice(paypal_id)
            remote = cafe.get_invoice(paypal_id)
            _apply_remote(conn, current, remote)
            conn.commit()
            warning = None
            _reconcile_locked(conn, today)
            payload = _payload_locked(conn, settings, today, warning)
            conn.commit()
            return payload
        except PayPalError as exc:
            conn.commit()
            raise FloatError(exc.message, 502) from exc
        finally:
            conn.close()


def approve_purchase(path: Path, settings: Settings, purchase_id: int, today: date | None = None) -> dict:
    today = today or date.today()
    with LOCK:
        conn = connect(path)
        try:
            warning = _refresh_locked(conn, settings)
            _reconcile_locked(conn, today)
            purchase = _purchase_by_id(conn, purchase_id)
            _require_current(conn, purchase)
            if purchase["status"] != "CLEAR":
                raise FloatError("Supplier checkout is still blocked. Pay the covering invoice first.", 409)
            token = secrets.token_urlsafe(24)
            conn.execute(
                "UPDATE purchases SET status = 'APPROVED', approval_token = ? WHERE id = ?",
                (token, purchase_id),
            )
            conn.execute(
                "INSERT INTO audits (purchase_id, detail) VALUES (?, ?)",
                (purchase_id, "The owner approved this exact cart total."),
            )
            conn.commit()
            return _payload_locked(conn, settings, today, warning)
        finally:
            conn.close()


def create_order(path: Path, settings: Settings, purchase_id: int, today: date | None = None) -> dict:
    today = today or date.today()
    with LOCK:
        conn = connect(path)
        try:
            _refresh_locked(conn, settings)
            _reconcile_locked(conn, today)
            purchase = _purchase_by_id(conn, purchase_id)
            _require_current(conn, purchase)
            if purchase["status"] != "APPROVED" or not purchase["approval_token"]:
                raise FloatError("Approve the cart before creating the PayPal order.", 409)
            _require_funds(conn, purchase)
            if not settings.supplier_ready:
                raise FloatError(
                    "Set SUPPLIER_PAYPAL_CLIENT_ID and SUPPLIER_PAYPAL_CLIENT_SECRET in .env before paying the supplier.",
                    400,
                )
            supplier = _supplier(settings)
            if purchase["paypal_order_id"] and not purchase["paypal_capture_id"]:
                existing = supplier.get_order(purchase["paypal_order_id"])
                if existing.get("status") in {"CREATED", "APPROVED", "PAYER_ACTION_REQUIRED"}:
                    conn.commit()
                    return {"paypal_order_id": existing["id"]}
                if existing.get("status") == "COMPLETED":
                    raise FloatError("This PayPal order is already captured.", 409)
            request_id = str(uuid.uuid4())
            order = supplier.create_order(_order_payload(conn, purchase), request_id)
            conn.execute(
                "UPDATE purchases SET paypal_order_id = ?, paypal_request_id = ? WHERE id = ?",
                (order["id"], request_id, purchase_id),
            )
            conn.commit()
            return {"paypal_order_id": order["id"]}
        except PayPalError as exc:
            conn.commit()
            raise FloatError(exc.message, 502) from exc
        finally:
            conn.close()


def capture_order(path: Path, settings: Settings, purchase_id: int, order_id: str, today: date | None = None) -> dict:
    today = today or date.today()
    if not settings.supplier_ready:
        raise FloatError("Supplier PayPal credentials are missing.", 400)
    with LOCK:
        conn = connect(path)
        try:
            purchase = _purchase_by_id(conn, purchase_id)
            _require_current(conn, purchase)
            if purchase["paypal_capture_id"]:
                return _payload_locked(conn, settings, today, None)
            if purchase["paypal_order_id"] != order_id:
                raise FloatError("That PayPal order does not belong to this cart.", 409)
            _require_funds(conn, purchase)
            supplier = _supplier(settings)
            request_id = str(uuid.uuid4())
            captured = supplier.capture_order(order_id, request_id)
            found = capture_id(captured)
            if not found:
                raise FloatError("PayPal captured the order but did not return a capture id.", 502)
            total = purchase["total_cents"] or 0
            book_after = _book_cash(conn.execute("SELECT * FROM app_state WHERE id = 1").fetchone()) - total
            conn.execute(
                "UPDATE purchases SET status = 'PAID', paypal_capture_id = ?, explanation = ? WHERE id = ?",
                (
                    found,
                    f"Paid {money(total)} to {purchase['supplier'].rstrip('.')}. Book cash is now {money(book_after)}.",
                    purchase_id,
                ),
            )
            conn.execute(
                "UPDATE app_state SET session_spent_cents = session_spent_cents + ? WHERE id = 1",
                (purchase["total_cents"],),
            )
            conn.execute(
                "INSERT INTO audits (purchase_id, detail) VALUES (?, ?)",
                (purchase_id, f"PayPal capture {found} paid the supplier. Book cash was reduced by the cart total."),
            )
            conn.commit()
            return _payload_locked(conn, settings, today, None)
        except PayPalError as exc:
            conn.commit()
            raise FloatError(exc.message, 502) from exc
        finally:
            conn.close()


def new_shift(path: Path, settings: Settings, today: date | None = None) -> dict:
    today = today or date.today()
    with LOCK:
        conn = connect(path)
        try:
            conn.execute(
                """
                UPDATE app_state
                SET session_collected_cents = 0,
                    session_spent_cents = 0,
                    shift_id = shift_id + 1
                WHERE id = 1
                """
            )
            unpaid_cover = conn.execute(
                "SELECT id FROM invoices WHERE role = 'covering' AND status NOT IN ('PAID', 'MARKED_AS_PAID') LIMIT 1"
            ).fetchone()
            if unpaid_cover is None:
                template = conn.execute(
                    "SELECT * FROM invoices WHERE role = 'covering' ORDER BY id DESC LIMIT 1"
                ).fetchone()
                stamp = datetime.utcnow().strftime("%H%M%S")
                email = settings.client_payer_email or template["customer_email"]
                conn.execute(
                    """
                    INSERT INTO invoices (
                        external_key, role, customer_name, customer_email, description,
                        amount_cents, due_date, status, counts_when_paid
                    ) VALUES (?, 'covering', ?, ?, ?, ?, ?, 'UNPAID', 1)
                    """,
                    (
                        f"northwind-{stamp}",
                        template["customer_name"],
                        email,
                        template["description"],
                        template["amount_cents"],
                        template["due_date"],
                    ),
                )
            conn.commit()
            return _payload_locked(conn, settings, today, None)
        finally:
            conn.close()


def _refresh_locked(conn, settings: Settings) -> str | None:
    if not settings.cafe_ready:
        return None
    rows = conn.execute(
        """
        SELECT * FROM invoices
        WHERE paypal_invoice_id IS NOT NULL
          AND status NOT IN ('PAID', 'MARKED_AS_PAID', 'CANCELLED', 'REFUNDED')
        """
    ).fetchall()
    if not rows:
        return None
    try:
        cafe = _cafe(settings)
        for row in rows:
            remote = cafe.get_invoice(row["paypal_invoice_id"])
            _apply_remote(conn, row, remote)
        return None
    except PayPalError as exc:
        return exc.message


def _apply_remote(conn, row, remote: dict) -> None:
    status = (remote.get("status") or "").upper()
    local = "PAID" if status in PAID_STATUSES else ("DRAFT" if status == "DRAFT" else "UNPAID")
    if status in {"CANCELLED", "REFUNDED"}:
        local = status
    url = recipient_url(remote)
    already = conn.execute("SELECT counted, counts_when_paid, amount_cents FROM invoices WHERE id = ?", (row["id"],)).fetchone()
    if local == "PAID" and already["counts_when_paid"] and not already["counted"]:
        conn.execute(
            "UPDATE app_state SET session_collected_cents = session_collected_cents + ? WHERE id = 1",
            (already["amount_cents"],),
        )
        conn.execute("UPDATE invoices SET counted = 1 WHERE id = ?", (row["id"],))
    conn.execute(
        "UPDATE invoices SET status = ?, recipient_url = COALESCE(?, recipient_url) WHERE id = ?",
        (local, url, row["id"]),
    )


def _reconcile_locked(conn, today: date) -> None:
    purchase = _current_purchase(conn)
    if purchase is None or purchase["status"] in {"PAID", "REJECTED", "OVER_BUDGET"}:
        return
    total = purchase["total_cents"] or 0
    if total <= 0:
        return
    state = conn.execute("SELECT * FROM app_state WHERE id = 1").fetchone()
    book = _book_cash(state)
    if book >= total and purchase["status"] == "BLOCKED":
        conn.execute(
            """
            UPDATE purchases
            SET status = 'CLEAR',
                explanation = ?
            WHERE id = ?
            """,
            (
                f"Book cash is {money(book)}. The cart is {money(total)}. You can approve the supplier payment.",
                purchase["id"],
            ),
        )
        conn.execute(
            "INSERT INTO audits (purchase_id, detail) VALUES (?, ?)",
            (purchase["id"], "PayPal reported the covering invoice paid, so the cash gate opened."),
        )
    elif book < total and purchase["status"] in {"CLEAR", "APPROVED"}:
        conn.execute(
            """
            UPDATE purchases
            SET status = 'BLOCKED', approval_token = NULL,
                explanation = ?
            WHERE id = ?
            """,
            (
                f"Book cash is {money(book)} and the cart is {money(total)}. Checkout is blocked again.",
                purchase["id"],
            ),
        )


def _payload_locked(conn, settings: Settings, today: date, warning: str | None) -> dict:
    state = conn.execute("SELECT * FROM app_state WHERE id = 1").fetchone()
    invoices = []
    for row in _invoice_rows(conn):
        overdue = days_overdue(date.fromisoformat(row["due_date"]), today)
        invoices.append(
            {
                "id": row["id"],
                "customer_name": row["customer_name"],
                "description": row["description"],
                "amount_cents": row["amount_cents"],
                "due_date": row["due_date"],
                "days_overdue": overdue,
                "status": row["status"],
                "role": row["role"],
                "paypal_invoice_id": row["paypal_invoice_id"],
                "recipient_url": row["recipient_url"],
                "note": row["note"],
            }
        )
    purchase = _current_purchase(conn)
    return {
        "business_name": "Harbor & Rye Café",
        "supplier_name": "Roaster's Supply Co.",
        "book_cash_cents": _book_cash(state),
        "starting_cash_cents": state["starting_cash_cents"],
        "session_collected_cents": state["session_collected_cents"],
        "session_spent_cents": state["session_spent_cents"],
        "shift_id": state["shift_id"],
        "paypal_ready": settings.paypal_ready,
        "cafe_ready": settings.cafe_ready,
        "supplier_ready": settings.supplier_ready,
        "supplier_client_id": settings.supplier_client_id or None,
        "llm_ready": settings.llm_ready,
        "ai_mode": settings.ai_mode,
        "ai_mode_warning": settings.ai_mode_warning,
        "warning": warning,
        "invoices": invoices,
        "catalog": [
            {
                "sku": item.sku,
                "name": item.name,
                "pack_size": item.pack_size,
                "price_cents": item.price_cents,
                "recycled": item.recycled,
            }
            for item in load_catalog()
        ],
        "purchase": _purchase_dict(conn, purchase) if purchase else None,
        "cash_label": "Book cash is the opening float, plus invoices PayPal marks paid this shift, minus supplier captures. It is not a PayPal balance.",
    }


def _purchase_dict(conn, purchase) -> dict:
    lines = conn.execute(
        "SELECT * FROM cart_lines WHERE purchase_id = ? ORDER BY id",
        (purchase["id"],),
    ).fetchall()
    audits = conn.execute(
        "SELECT detail FROM audits WHERE purchase_id = ? ORDER BY id",
        (purchase["id"],),
    ).fetchall()
    invoice = None
    if purchase["invoice_id"]:
        invoice = conn.execute("SELECT * FROM invoices WHERE id = ?", (purchase["invoice_id"],)).fetchone()
    return {
        "id": purchase["id"],
        "request_text": purchase["request_text"],
        "status": purchase["status"],
        "total_cents": purchase["total_cents"],
        "budget_cents": purchase["budget_cents"],
        "supplier": purchase["supplier"],
        "explanation": purchase["explanation"],
        "parser": purchase["parser"],
        "parsed": json.loads(purchase["parsed_json"]) if purchase["parsed_json"] else None,
        "issues": json.loads(purchase["issues_json"]) if purchase["issues_json"] else [],
        "paypal_order_id": purchase["paypal_order_id"],
        "paypal_capture_id": purchase["paypal_capture_id"],
        "lines": [
            {
                "sku": line["sku"],
                "name": line["name"],
                "pack_count": line["pack_count"],
                "pack_size": line["pack_size"],
                "units_covered": line["units_covered"],
                "requested_units": line["requested_units"],
                "unit_price_cents": line["unit_price_cents"],
                "line_total_cents": line["line_total_cents"],
                "recycled": bool(line["recycled"]),
            }
            for line in lines
        ],
        "invoice": None
        if invoice is None
        else {
            "id": invoice["id"],
            "customer_name": invoice["customer_name"],
            "amount_cents": invoice["amount_cents"],
            "status": invoice["status"],
            "paypal_invoice_id": invoice["paypal_invoice_id"],
            "recipient_url": invoice["recipient_url"],
        },
        "audits": [row["detail"] for row in audits],
    }


def _invoice_payload(invoice, note: str, settings: Settings, include_dates: bool) -> dict:
    detail = {"currency_code": "USD", "note": note}
    if include_dates:
        due = date.fromisoformat(invoice["due_date"])
        detail["payment_term"] = {"term_type": "DUE_ON_DATE_SPECIFIED", "due_date": due.isoformat()}
        issued = due if due <= date.today() else date.today()
        if due < date.today():
            issued = date.fromordinal(due.toordinal() - 7)
        detail["invoice_date"] = issued.isoformat()
    invoicer = {"business_name": "Harbor and Rye Cafe"}
    if settings.cafe_merchant_email:
        invoicer["email_address"] = settings.cafe_merchant_email
    return {
        "detail": detail,
        "invoicer": invoicer,
        "primary_recipients": [
            {
                "billing_info": {
                    "business_name": invoice["customer_name"],
                    "email_address": invoice["customer_email"],
                }
            }
        ],
        "items": [
            {
                "name": invoice["description"],
                "quantity": "1",
                "unit_amount": {"currency_code": "USD", "value": paypal_money(invoice["amount_cents"])},
            }
        ],
    }


def _order_payload(conn, purchase) -> dict:
    lines = conn.execute("SELECT * FROM cart_lines WHERE purchase_id = ?", (purchase["id"],)).fetchall()
    items = []
    for line in lines:
        items.append(
            {
                "name": line["name"][:127],
                "sku": line["sku"],
                "quantity": str(line["pack_count"]),
                "category": "PHYSICAL_GOODS",
                "unit_amount": {"currency_code": "USD", "value": paypal_money(line["unit_price_cents"])},
            }
        )
    total = paypal_money(purchase["total_cents"])
    return {
        "intent": "CAPTURE",
        "purchase_units": [
            {
                "custom_id": f"float-{purchase['id']}",
                "description": "Harbor and Rye supply order",
                "amount": {
                    "currency_code": "USD",
                    "value": total,
                    "breakdown": {"item_total": {"currency_code": "USD", "value": total}},
                },
                "items": items,
            }
        ],
    }


def _note(conn, purchase_id: int) -> str:
    lines = conn.execute("SELECT name, requested_units FROM cart_lines WHERE purchase_id = ?", (purchase_id,)).fetchall()
    summary = ", ".join(f"{line['requested_units']} {line['name']}" for line in lines)
    return (
        f"Harbor and Rye will not pay its supplier for {summary} until this invoice is paid."
    )[:1000]


def _parsed_dict(parsed: ParsedRequest) -> dict:
    return {
        "items": [
            {"product": item.product_query, "sku": item.sku, "quantity": item.quantity, "unit": item.unit}
            for item in parsed.items
        ],
        "budget_cents": parsed.budget_cents,
        "prefer_recycled": parsed.prefer_recycled,
        "hold_until_invoice_paid": parsed.hold_until_invoice_paid,
        "require_approval": True,
        "parser": parsed.parser,
    }


def _hash_cart(lines: list[CartLine], total: int, invoice_id: int | None) -> str:
    raw = "|".join(f"{line.sku}:{line.pack_count}:{line.unit_price_cents}" for line in lines)
    raw += f"|{total}|{invoice_id}"
    return hashlib.sha256(raw.encode()).hexdigest()


def _book_cash(state) -> int:
    return state["starting_cash_cents"] + state["session_collected_cents"] - state["session_spent_cents"]


def _facts(row) -> InvoiceFacts:
    return InvoiceFacts(
        id=row["id"],
        customer_name=row["customer_name"],
        description=row["description"],
        amount_cents=row["amount_cents"],
        due_date=date.fromisoformat(row["due_date"]),
        status=row["status"],
        role=row["role"],
    )


def _invoice_rows(conn):
    return conn.execute("SELECT * FROM invoices ORDER BY due_date, id").fetchall()


def _current_purchase(conn):
    state = conn.execute("SELECT shift_id FROM app_state WHERE id = 1").fetchone()
    return conn.execute(
        "SELECT * FROM purchases WHERE shift_id = ? ORDER BY id DESC LIMIT 1",
        (state["shift_id"],),
    ).fetchone()


def _purchase_by_id(conn, purchase_id: int):
    purchase = conn.execute("SELECT * FROM purchases WHERE id = ?", (purchase_id,)).fetchone()
    if purchase is None:
        raise FloatError("Purchase not found.", 404)
    return purchase


def _require_current(conn, purchase) -> None:
    state = conn.execute("SELECT shift_id FROM app_state WHERE id = 1").fetchone()
    if purchase["shift_id"] != state["shift_id"]:
        raise FloatError("That cart belongs to an earlier shift.", 409)


def _require_funds(conn, purchase) -> None:
    state = conn.execute("SELECT * FROM app_state WHERE id = 1").fetchone()
    book = _book_cash(state)
    total = purchase["total_cents"] or 0
    if book < total:
        raise FloatError(
            f"Book cash is {money(book)} and this cart is {money(total)}. PayPal will not be called.",
            409,
        )


def _cafe(settings: Settings) -> PayPalClient:
    return PayPalClient(settings.cafe_client_id, settings.cafe_client_secret, settings.paypal_base_url)


def _supplier(settings: Settings) -> PayPalClient:
    return PayPalClient(settings.supplier_client_id, settings.supplier_client_secret, settings.paypal_base_url)
