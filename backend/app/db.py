import sqlite3
import threading
from pathlib import Path

from app.config import BACKEND_ROOT

DB_PATH = BACKEND_ROOT / "data" / "float.db"
LOCK = threading.Lock()
PLACEHOLDER_EMAIL = "buyer-not-configured@example.com"

SCHEMA = """
CREATE TABLE IF NOT EXISTS app_state (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    starting_cash_cents INTEGER NOT NULL,
    session_collected_cents INTEGER NOT NULL DEFAULT 0,
    session_spent_cents INTEGER NOT NULL DEFAULT 0,
    shift_id INTEGER NOT NULL DEFAULT 1
);

CREATE TABLE IF NOT EXISTS invoices (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    external_key TEXT UNIQUE NOT NULL,
    role TEXT NOT NULL,
    customer_name TEXT NOT NULL,
    customer_email TEXT NOT NULL,
    description TEXT NOT NULL,
    amount_cents INTEGER NOT NULL,
    due_date TEXT NOT NULL,
    status TEXT NOT NULL,
    counts_when_paid INTEGER NOT NULL,
    counted INTEGER NOT NULL DEFAULT 0,
    paypal_invoice_id TEXT,
    paypal_request_id TEXT,
    recipient_url TEXT,
    note TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS purchases (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    shift_id INTEGER NOT NULL,
    request_text TEXT NOT NULL,
    status TEXT NOT NULL,
    total_cents INTEGER,
    budget_cents INTEGER,
    supplier TEXT,
    explanation TEXT NOT NULL,
    invoice_id INTEGER,
    cart_hash TEXT,
    approval_token TEXT,
    paypal_order_id TEXT,
    paypal_capture_id TEXT,
    paypal_request_id TEXT,
    parser TEXT NOT NULL,
    parsed_json TEXT,
    issues_json TEXT,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (invoice_id) REFERENCES invoices(id)
);

CREATE TABLE IF NOT EXISTS cart_lines (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_id INTEGER NOT NULL,
    sku TEXT NOT NULL,
    name TEXT NOT NULL,
    pack_count INTEGER NOT NULL,
    pack_size INTEGER NOT NULL,
    units_covered INTEGER NOT NULL,
    requested_units INTEGER NOT NULL,
    unit_price_cents INTEGER NOT NULL,
    line_total_cents INTEGER NOT NULL,
    recycled INTEGER NOT NULL,
    supplier TEXT NOT NULL,
    FOREIGN KEY (purchase_id) REFERENCES purchases(id)
);

CREATE TABLE IF NOT EXISTS audits (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    purchase_id INTEGER NOT NULL,
    detail TEXT NOT NULL,
    created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (purchase_id) REFERENCES purchases(id)
);
"""

SEEDS = (
    {
        "external_key": "northwind",
        "role": "covering",
        "customer_name": "Northwind Catering",
        "description": "Weekend catering bar",
        "amount_cents": 8000,
        "due_date": "2026-10-24",
        "status": "UNPAID",
        "counts_when_paid": 1,
    },
    {
        "external_key": "harbor-book-club",
        "role": "small",
        "customer_name": "Harbor Book Club",
        "description": "Reading-night coffee",
        "amount_cents": 1800,
        "due_date": "2026-10-31",
        "status": "UNPAID",
        "counts_when_paid": 1,
    },
    {
        "external_key": "maple-street",
        "role": "future",
        "customer_name": "Maple Street Office",
        "description": "Office breakfast",
        "amount_cents": 4000,
        "due_date": "2026-11-14",
        "status": "UNPAID",
        "counts_when_paid": 1,
    },
    {
        "external_key": "sunday-market",
        "role": "history",
        "customer_name": "Sunday Market",
        "description": "Stall fee",
        "amount_cents": 2500,
        "due_date": "2026-10-18",
        "status": "PAID",
        "counts_when_paid": 0,
    },
)


def connect(path: Path | None = None) -> sqlite3.Connection:
    database = path or DB_PATH
    database.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(database, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(path: Path | None = None, starting_cash_cents: int = 2000, payer_email: str = "") -> None:
    email = payer_email or PLACEHOLDER_EMAIL
    with LOCK:
        conn = connect(path)
        try:
            conn.executescript(SCHEMA)
            columns = {row["name"] for row in conn.execute("PRAGMA table_info(purchases)")}
            for column in ("parsed_json", "issues_json"):
                if column not in columns:
                    conn.execute(f"ALTER TABLE purchases ADD COLUMN {column} TEXT")
            conn.execute(
                """
                INSERT INTO app_state (id, starting_cash_cents, session_collected_cents, session_spent_cents, shift_id)
                VALUES (1, ?, 0, 0, 1)
                ON CONFLICT(id) DO NOTHING
                """,
                (starting_cash_cents,),
            )
            for seed in SEEDS:
                conn.execute(
                    """
                    INSERT INTO invoices (
                        external_key, role, customer_name, customer_email, description,
                        amount_cents, due_date, status, counts_when_paid
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                    ON CONFLICT(external_key) DO NOTHING
                    """,
                    (
                        seed["external_key"],
                        seed["role"],
                        seed["customer_name"],
                        email,
                        seed["description"],
                        seed["amount_cents"],
                        seed["due_date"],
                        seed["status"],
                        seed["counts_when_paid"],
                    ),
                )
            if payer_email:
                conn.execute(
                    """
                    UPDATE invoices
                    SET customer_email = ?
                    WHERE paypal_invoice_id IS NULL
                      AND customer_email = ?
                    """,
                    (payer_email, PLACEHOLDER_EMAIL),
                )
            conn.commit()
        finally:
            conn.close()
