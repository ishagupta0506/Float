from pathlib import Path

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.config import load_settings
from app.db import DB_PATH, init_db
from app.paypal_client import PayPalError
from app.service import (
    FloatError,
    approve_purchase,
    capture_order,
    create_order,
    create_purchase,
    get_state,
    new_shift,
    send_invoice,
)

app = FastAPI(title="Float", version="0.1.0")
app.add_middleware(
    CORSMiddleware,
    allow_origins=["http://localhost:5173", "http://127.0.0.1:5173"],
    allow_methods=["*"],
    allow_headers=["*"],
)


class PurchaseIn(BaseModel):
    text: str


class CaptureIn(BaseModel):
    purchase_id: int


@app.on_event("startup")
def startup() -> None:
    settings = load_settings()
    init_db(DB_PATH, settings.starting_cash_cents, settings.client_payer_email)


@app.exception_handler(FloatError)
def float_error(_request, exc: FloatError):
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.message})


@app.exception_handler(PayPalError)
def paypal_error(_request, exc: PayPalError):
    return JSONResponse(status_code=502, content={"detail": exc.message})


@app.get("/api/health")
def health():
    return {"ok": True}


@app.get("/api/state")
def state():
    return get_state(DB_PATH, load_settings())


@app.post("/api/purchases")
def purchases(body: PurchaseIn):
    return create_purchase(DB_PATH, load_settings(), body.text)


@app.post("/api/invoices/{invoice_id}/send")
def invoices_send(invoice_id: int):
    return send_invoice(DB_PATH, load_settings(), invoice_id)


@app.post("/api/purchases/{purchase_id}/approve")
def purchases_approve(purchase_id: int):
    return approve_purchase(DB_PATH, load_settings(), purchase_id)


@app.post("/api/purchases/{purchase_id}/order")
def purchases_order(purchase_id: int):
    return create_order(DB_PATH, load_settings(), purchase_id)


@app.post("/api/orders/{order_id}/capture")
def orders_capture(order_id: str, body: CaptureIn):
    return capture_order(DB_PATH, load_settings(), body.purchase_id, order_id)


@app.post("/api/shift")
def shift():
    return new_shift(DB_PATH, load_settings())


static_dir = Path(__file__).resolve().parent / "static"
if static_dir.exists():
    app.mount("/", StaticFiles(directory=static_dir, html=True), name="static")
