# Float

A café supply agent that prices a cart from its own catalog and refuses to create the PayPal supplier payment until the customer invoice that covers the cash gap is paid.

The owner asks for supplies in plain language. Float builds the cart from catalog prices, compares the total with book cash, and keeps supplier checkout off while there is a gap. It picks the unpaid invoice that closes the gap. When PayPal reports that invoice paid, the same cart unlocks, the owner approves the exact total, and PayPal captures the payment to the supplier.

## What is real and what is not

Real PayPal sandbox calls:

- Invoicing v2 on the café app: create, send, and read status. The client pays on PayPal's hosted invoice page.
- Orders v2 on the supplier app: create, buyer approval in the PayPal button, and capture. The capture id is shown in the app.

Not real, and labeled in the UI:

- **Book cash** is the opening float, plus invoices PayPal marks paid this shift, minus supplier captures. It is not a PayPal balance.
- The supplier catalog is a local CSV. Nothing is shipped.
- PayPal ignores custom text on reminder emails, so Float writes its note on the invoice itself.

## The purchase-request reader

The owner types a request in plain language, for example:

> I need 12 coffee bags and 6 milk cartons. Keep the total under $60 and don't let me pay the supplier until my catering invoice is settled.

The reader turns it into a structured request: products (words or catalog IDs), quantities, units or packs, the budget, a recycled preference, and whether the owner asked to wait for an invoice. The UI shows this under **How Float read it**.

### Modes

Set `AI_MODE` in `.env`:

| Mode | What reads the request |
|---|---|
| `auto` (default) | The model if `LLM_API_KEY` is set, otherwise the mock reader |
| `mock` | The deterministic mock reader. No network calls. The tests use this mode |
| `llm` | The model. If the key is empty, Float uses the mock reader and shows a warning |

The model is any OpenAI-compatible chat endpoint (`LLM_BASE_URL`, `LLM_MODEL`, `LLM_API_KEY`). Float requests JSON output with `response_format: {"type": "json_object"}`. Keys live only in `.env`, which git ignores. If the model call fails or returns something that is not a JSON object, Float uses the mock reader and records that in the cart's audit list.

### What the model can and cannot do

The model only proposes a structured request. Everything after that is Python:

- **Prices.** The model is sent product IDs, names, and pack sizes, never prices. Any price, total, or amount field in its output is ignored and noted in the audit list. The cart total is always computed from `backend/data/catalog.csv`.
- **Product IDs.** Every ID must exist in the catalog. An unknown ID, or an ID that contradicts the product words (for example "napkins" with the coffee ID), rejects the request.
- **Quantities.** Quantities must be whole numbers of at least 1, with a limit of 10,000 units or 500 packs per product. Anything else rejects the request with a message saying which item is wrong.
- **Ambiguity.** If a phrase fits more than one product ("10 bags" could be carrier bags, coffee, or tea), Float asks which one instead of guessing.
- **Budget.** A budget typed in the request wins over the model's reading of it. A budget in another currency is rejected rather than ignored. An over-budget cart keeps the requested quantities and products and shows how far over it is. Float never shrinks or swaps items to fit.
- **Payments and invoices.** The model's output has no way to approve a cart, create or capture a PayPal order, or change an invoice. Extra fields such as `approve`, `status`, or `mark_invoice_paid` are dropped and listed in the audit. Approval, order creation, and capture are separate server endpoints that check the cash gate and the owner's approval on every call.

`backend/tests/test_ai_bypass.py` feeds the service hostile model output and confirms that no order is created, nothing is captured, book cash is unchanged, and the Northwind invoice stays unpaid.

## Setup

You need two sandbox **business** accounts and two sandbox **personal** accounts from the [PayPal Developer Dashboard](https://developer.paypal.com/dashboard/accounts):

| Account | Type | Used for |
|---|---|---|
| Café | Business | Its REST app sends the catering invoice |
| Supplier | Business | Its REST app creates and captures the supply order |
| Catering client | Personal | Pays the invoice. Its email is `CLIENT_PAYER_EMAIL` |
| Café owner | Personal | Pays the supplier in the PayPal button |

Create one REST app for the café business account and one for the supplier business account. Copy each app's client id and secret into `.env`.

```bash
cp .env.example .env    # then fill it in
```

Backend:

```bash
cd backend
python -m venv .venv
.venv/Scripts/activate          # macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
python scripts/check_paypal.py  # confirms both sandbox apps work
python -m pytest -q
```

Frontend:

```bash
cd frontend
npm install
npm run build                   # writes into backend/app/static
```

Run:

```bash
cd backend
uvicorn app.main:app --port 8000
```

Open http://127.0.0.1:8000. For frontend development, run `npm run dev` in `frontend` and open http://localhost:5173.

## Demo path

1. Click **Price this cart** with the demo request. The cart is $54.00, book cash is $20.00, and supplier checkout is off.
2. Click **Send Northwind Catering invoice**, then **Open the PayPal invoice**.
3. In a second browser profile, log in as the catering client and pay the invoice.
4. Float polls PayPal. When the invoice reads PAID, book cash becomes $100.00 and **Approve $54.00** appears.
5. Approve, then pay with the PayPal button as the café owner. The capture id appears under the cart.

To run it again, click **Start another shift**. To start from scratch, stop the server and delete `backend/data/float.db`.

## License

MIT. See [LICENSE](LICENSE).
