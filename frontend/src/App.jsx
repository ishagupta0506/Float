import { useEffect, useState } from "react";

const DEMO =
  "I need 100 paper cups and 50 takeaway containers. My budget is $60. Prioritize environmentally friendly products and do not purchase anything without my approval.";

const POLL_MS = 4000;
const GATED = new Set(["BLOCKED", "CLEAR", "APPROVED"]);

function money(cents) {
  const sign = cents < 0 ? "-" : "";
  const abs = Math.abs(cents);
  return `${sign}$${Math.floor(abs / 100)}.${String(abs % 100).padStart(2, "0")}`;
}

async function api(path, options) {
  const response = await fetch(path, {
    headers: { "Content-Type": "application/json" },
    ...options,
  });
  const body = await response.json().catch(() => ({}));
  if (!response.ok) {
    throw new Error(body.detail || "The request failed.");
  }
  return body;
}

export default function App() {
  const [state, setState] = useState(null);
  const [text, setText] = useState(DEMO);
  const [loadError, setLoadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [pollError, setPollError] = useState("");
  const [busyAction, setBusyAction] = useState("");
  const [checkout, setCheckout] = useState("idle");
  const busy = busyAction !== "";

  async function refresh() {
    const next = await api("/api/state");
    setState(next);
    return next;
  }

  async function load() {
    setLoadError("");
    try {
      await refresh();
    } catch (err) {
      setLoadError(err.message);
    }
  }

  useEffect(() => {
    load();
  }, []);

  const purchase = state?.purchase;
  const invoice = purchase?.invoice;
  const waitingOnPayPal = Boolean(purchase?.status === "BLOCKED" && invoice?.paypal_invoice_id);
  const awaitingPayment = waitingOnPayPal && invoice.status !== "PAID" && invoice.status !== "DRAFT";

  useEffect(() => {
    if (!waitingOnPayPal) {
      setPollError("");
      return undefined;
    }
    const timer = setInterval(() => {
      refresh()
        .then(() => setPollError(""))
        .catch((err) => setPollError(err.message));
    }, POLL_MS);
    return () => clearInterval(timer);
  }, [waitingOnPayPal, purchase?.id]);

  useEffect(() => {
    if (!state?.supplier_client_id || purchase?.status !== "APPROVED" || purchase.paypal_capture_id) {
      setCheckout("idle");
      return undefined;
    }
    let cancelled = false;
    let buttons;
    setCheckout("loading");
    loadPayPal(state.supplier_client_id)
      .then(() => {
        if (cancelled || !window.paypal) return undefined;
        const slot = document.getElementById("paypal-button");
        if (!slot) return undefined;
        slot.innerHTML = "";
        buttons = window.paypal.Buttons({
          createOrder: async () => {
            setCheckout("ready");
            setActionError("");
            const order = await api(`/api/purchases/${purchase.id}/order`, { method: "POST" });
            return order.paypal_order_id;
          },
          onApprove: async (data) => {
            setBusyAction("capture");
            setCheckout("capturing");
            try {
              const next = await api(`/api/orders/${data.orderID}/capture`, {
                method: "POST",
                body: JSON.stringify({ purchase_id: purchase.id }),
              });
              setState(next);
              setActionError("");
            } catch (err) {
              setActionError(err.message);
              setCheckout("ready");
            } finally {
              setBusyAction("");
            }
          },
          onCancel: () => {
            if (!cancelled) setCheckout("cancelled");
          },
          onError: (err) => {
            setActionError(err?.message || "PayPal checkout failed.");
            setCheckout("ready");
          },
        });
        return buttons.render(slot).then(() => {
          if (!cancelled) setCheckout((current) => (current === "loading" ? "ready" : current));
        });
      })
      .catch((err) => {
        if (cancelled) return;
        setActionError(err?.message || "PayPal checkout did not load.");
        setCheckout("failed");
      });
    return () => {
      cancelled = true;
      if (buttons) buttons.close().catch(() => {});
    };
  }, [state?.supplier_client_id, purchase?.id, purchase?.status, purchase?.paypal_capture_id]);

  async function run(key, action) {
    setBusyAction(key);
    setActionError("");
    try {
      setState(await action());
      setPollError("");
    } catch (err) {
      setActionError(err.message);
    } finally {
      setBusyAction("");
    }
  }

  async function checkNow() {
    setBusyAction("check");
    try {
      await refresh();
      setPollError("");
    } catch (err) {
      setPollError(err.message);
    } finally {
      setBusyAction("");
    }
  }

  if (!state) {
    return (
      <main className="app boot">
        <p className="eyebrow">Float</p>
        {loadError ? (
          <div className="boot-card">
            <h1>Float can't reach its server</h1>
            <div className="error" role="alert">
              The café ledger did not load ({loadError}). Check that the backend is running, then try again.
            </div>
            <button className="primary" type="button" onClick={load}>Try again</button>
          </div>
        ) : (
          <p className="waiting" role="status"><span className="spinner" aria-hidden="true" />Loading the café ledger…</p>
        )}
      </main>
    );
  }

  const gated = purchase && GATED.has(purchase.status) && purchase.total_cents > 0;
  const shortfall = gated ? Math.max(0, purchase.total_cents - state.book_cash_cents) : 0;
  const invoiceSent = Boolean(invoice?.paypal_invoice_id && invoice.status !== "DRAFT");
  const canOpenInvoice = Boolean(
    purchase?.status === "BLOCKED" && invoiceSent && invoice.recipient_url && invoice.status !== "PAID",
  );
  const canSend = Boolean(purchase?.status === "BLOCKED" && invoice && invoice.status !== "PAID" && !canOpenInvoice);
  const paid = Boolean(purchase?.paypal_capture_id);
  const actionable = purchase && (GATED.has(purchase.status) || paid);

  const feedback = (
    <>
      {actionError && <div className="error" role="alert">{actionError}</div>}
      {pollError && (
        <div className="warning" role="status">
          Float couldn't reach PayPal just now ({pollError}). It will keep checking.
        </div>
      )}
      {state.warning && <div className="warning" role="status">{state.warning}</div>}
    </>
  );

  return (
    <main className="app">
      <header>
        <div>
          <p className="eyebrow">Float</p>
          <h1>{state.business_name}</h1>
        </div>
        <div className="cash">
          <span>Book cash</span>
          <strong>{money(state.book_cash_cents)}</strong>
          <small>
            Opening {money(state.starting_cash_cents)} · Collected {money(state.session_collected_cents)} · Spent {money(state.session_spent_cents)}
          </small>
          {gated && (
            <div className={`gate ${shortfall > 0 ? "short" : "covered"}`}>
              {shortfall > 0
                ? <>This cart needs {money(purchase.total_cents)} · short {money(shortfall)}</>
                : <>Covers this {money(purchase.total_cents)} cart</>}
            </div>
          )}
        </div>
      </header>
      <p className="note">{state.cash_label}</p>

      <section className="layout">
        <aside className="panel invoices-panel">
          <h2>Invoices</h2>
          {state.invoices.map((row) => {
            const status = invoiceStatus(row);
            const unlocks = invoice && invoice.id === row.id && purchase && (GATED.has(purchase.status) || paid);
            return (
              <article className={`invoice${unlocks ? " unlocks" : ""}`} key={row.id}>
                <div>
                  <strong>{row.customer_name}</strong>
                  {unlocks && <span className="tag">{paid ? "Unlocked this cart" : "Unlocks this cart"}</span>}
                  <div className="muted">
                    {row.description} · due {row.due_date}
                    {row.days_overdue > 0 && row.status !== "PAID" ? ` · ${row.days_overdue} days overdue` : ""}
                  </div>
                  {row.paypal_invoice_id && <div className="muted mono">{row.paypal_invoice_id}</div>}
                </div>
                <div className="invoice-side">
                  <strong>{money(row.amount_cents)}</strong>
                  <div className={`pill ${status.tone}`}>{status.label}</div>
                </div>
              </article>
            );
          })}
        </aside>

        <section className="panel request-panel">
          <h2>Supply request</h2>
          <textarea value={text} onChange={(event) => setText(event.target.value)} aria-label="Supply request" />
          <div className="row">
            <button
              className={actionable && !paid ? "secondary" : "primary"}
              disabled={busy || !text.trim()}
              onClick={() => run("price", () => api("/api/purchases", { method: "POST", body: JSON.stringify({ text }) }))}
            >
              {busyAction === "price" ? <><span className="spinner" aria-hidden="true" />Pricing…</> : "Price this cart"}
            </button>
            <button className="quiet" type="button" disabled={busy} onClick={() => setText(DEMO)}>Use the demo request</button>
          </div>

          {!purchase && feedback}

          {purchase && (
            <div>
              {paid ? (
                <Receipt purchase={purchase} state={state} />
              ) : (
                <div className={`banner ${purchase.status.toLowerCase()}`}>
                  <strong>{labelFor(purchase.status)}</strong>
                  <p>{purchase.explanation}</p>
                  {purchase.issues?.length > 0 && (
                    <ul className="issues">
                      {purchase.issues.map((issue, index) => (
                        <li key={`${issue.code}-${index}`}>{issue.message}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}

              <div className="next">
                {awaitingPayment && (
                  <p className="waiting" role="status">
                    <span className="pulse" aria-hidden="true" />
                    Waiting for PayPal to confirm {invoice.customer_name}'s payment. Float checks every few seconds.
                  </p>
                )}

                {(canSend || canOpenInvoice || purchase.status === "CLEAR") && (
                  <div className="actions">
                    {canSend && (
                      <button
                        className="primary"
                        disabled={busy || !state.cafe_ready}
                        onClick={() => run("send", () => api(`/api/invoices/${invoice.id}/send`, { method: "POST" }))}
                      >
                        {busyAction === "send"
                          ? <><span className="spinner" aria-hidden="true" />Sending to PayPal…</>
                          : `Send ${invoice.customer_name} invoice`}
                      </button>
                    )}
                    {canOpenInvoice && (
                      <a className="button primary" href={invoice.recipient_url} target="_blank" rel="noreferrer">
                        Open the PayPal invoice
                      </a>
                    )}
                    {canOpenInvoice && (
                      <button className="quiet" type="button" disabled={busy} onClick={checkNow}>
                        {busyAction === "check" ? "Checking…" : "Check payment now"}
                      </button>
                    )}
                    {purchase.status === "CLEAR" && (
                      <button
                        className="primary"
                        disabled={busy}
                        onClick={() => run("approve", () => api(`/api/purchases/${purchase.id}/approve`, { method: "POST" }))}
                      >
                        {busyAction === "approve"
                          ? <><span className="spinner" aria-hidden="true" />Approving…</>
                          : `Approve ${money(purchase.total_cents)}`}
                      </button>
                    )}
                  </div>
                )}

                {canSend && !state.cafe_ready && (
                  <p className="muted small">Café invoicing isn't configured, so the invoice can't be sent yet.</p>
                )}

                {purchase.status === "APPROVED" && !paid && (
                  <div className="checkout">
                    <p className="muted small">
                      Pay {state.supplier_name} with the café owner’s sandbox personal account. The catering client’s account is the one that pays the invoice, not this order.
                    </p>
                    {checkout === "loading" && (
                      <p className="waiting" role="status"><span className="spinner" aria-hidden="true" />Loading PayPal checkout…</p>
                    )}
                    {checkout === "capturing" && (
                      <p className="waiting" role="status">
                        <span className="pulse" aria-hidden="true" />
                        Capturing {money(purchase.total_cents)} for {purchase.supplier || state.supplier_name}…
                      </p>
                    )}
                    {checkout === "cancelled" && (
                      <div className="warning" role="status">
                        PayPal checkout was closed before approval. Nothing was paid. Click the PayPal button to try again.
                      </div>
                    )}
                    <div id="paypal-button" />
                  </div>
                )}

                {feedback}
              </div>

              {purchase.parsed && <ParsedRequest parsed={purchase.parsed} />}

              {purchase.lines.length > 0 && (
                <div className="cart">
                  {purchase.lines.map((line) => (
                    <div className="line" key={line.sku}>
                      <div>
                        <strong>{line.name}</strong>
                        <div className="muted">
                          {line.pack_count} packs of {line.pack_size} cover {line.requested_units} · {line.recycled ? "recycled" : "standard"} · {line.sku}
                        </div>
                      </div>
                      <strong>{money(line.line_total_cents)}</strong>
                    </div>
                  ))}
                  <div className="line total">
                    <strong>Supplier total</strong>
                    <strong>{money(purchase.total_cents)}</strong>
                  </div>
                </div>
              )}

              {purchase.audits.length > 0 && (
                <div className="audit-block">
                  <h3>What Float checked</h3>
                  <ul className="audits">
                    {purchase.audits.map((item, index) => <li key={index}>{item}</li>)}
                  </ul>
                </div>
              )}
            </div>
          )}

          <div className="setup">
            {!state.cafe_ready && <p>Café invoicing is not configured. Add the café sandbox app and CLIENT_PAYER_EMAIL to .env.</p>}
            {!state.supplier_ready && <p>Supplier checkout is not configured. Add the supplier sandbox app to .env. The cart can still be priced.</p>}
            <p>
              {state.ai_mode === "llm"
                ? "Requests are read by the configured model. The catalog sets every price."
                : "Mock reader in use: requests are parsed by fixed rules and no model is called."}
            </p>
            {state.ai_mode_warning && <p className="warning">{state.ai_mode_warning}</p>}
            <button className="quiet" type="button" disabled={busy} onClick={() => run("shift", () => api("/api/shift", { method: "POST" }))}>
              {busyAction === "shift" ? "Starting a new shift…" : "Start another shift"}
            </button>
          </div>
        </section>
      </section>

      <details className="catalog">
        <summary>Catalog prices used for this cart</summary>
        <ul>
          {state.catalog.map((item) => (
            <li key={item.sku}>
              {item.name} · pack of {item.pack_size} · {money(item.price_cents)} · {item.recycled ? "recycled" : "standard"} · {item.sku}
            </li>
          ))}
        </ul>
      </details>
    </main>
  );
}

function labelFor(status) {
  if (status === "BLOCKED") return "Supplier checkout is off";
  if (status === "CLEAR") return "The cash gate is open";
  if (status === "APPROVED") return "Approved. PayPal can collect for the supplier";
  if (status === "PAID") return "Supplier paid";
  if (status === "OVER_BUDGET") return "Over your budget";
  if (status === "REJECTED") return "Float needs a clearer request";
  return "No payable cart";
}

function invoiceStatus(invoice) {
  if (invoice.status === "PAID" || invoice.status === "MARKED_AS_PAID") return { label: "Paid", tone: "paid" };
  if (invoice.status === "DRAFT") return { label: "Draft", tone: "draft" };
  if (invoice.status === "CANCELLED" || invoice.status === "REFUNDED") {
    return { label: invoice.status.toLowerCase(), tone: "draft" };
  }
  if (invoice.paypal_invoice_id) return { label: "Sent · unpaid", tone: "sent" };
  return { label: "Unpaid", tone: "unpaid" };
}

function Receipt({ purchase, state }) {
  const supplier = purchase.supplier || state.supplier_name;
  return (
    <div className="receipt" role="status">
      <div className="receipt-head">
        <span className="receipt-mark" aria-hidden="true">✓</span>
        <div>
          <strong>Paid {money(purchase.total_cents)} to {supplier}</strong>
          <div className="muted">Book cash is now {money(state.book_cash_cents)}.</div>
        </div>
      </div>
      <dl>
        <div>
          <dt>PayPal capture</dt>
          <dd className="mono">{purchase.paypal_capture_id}</dd>
        </div>
        {purchase.paypal_order_id && (
          <div>
            <dt>PayPal order</dt>
            <dd className="mono">{purchase.paypal_order_id}</dd>
          </div>
        )}
        {purchase.invoice && (
          <div>
            <dt>Unlocked by</dt>
            <dd>
              {purchase.invoice.customer_name} invoice
              {purchase.invoice.paypal_invoice_id && <span className="mono"> {purchase.invoice.paypal_invoice_id}</span>}
            </dd>
          </div>
        )}
      </dl>
    </div>
  );
}

function ParsedRequest({ parsed }) {
  const constraints = [];
  if (parsed.prefer_recycled) constraints.push("Recycled products preferred");
  if (parsed.hold_until_invoice_paid) constraints.push("Hold until an invoice is paid");
  constraints.push("Your approval required");
  return (
    <div className="parsed">
      <div className="parsed-head">
        <strong>How Float read it</strong>
        <span className="muted">{parsed.parser === "llm" ? "Model" : "Mock reader"}</span>
      </div>
      {parsed.items.length > 0 ? (
        <ul>
          {parsed.items.map((item, index) => (
            <li key={index}>
              {item.quantity} {item.unit === "packs" ? "packs of " : ""}
              {item.product}
              {item.sku && item.sku !== item.product ? ` · ${item.sku}` : ""}
            </li>
          ))}
        </ul>
      ) : (
        <p className="muted">No products with quantities.</p>
      )}
      <p className="muted">
        Budget: {parsed.budget_cents == null ? "none stated" : money(parsed.budget_cents)}
      </p>
      <div className="chips">
        {constraints.map((label) => <span className="chip" key={label}>{label}</span>)}
      </div>
    </div>
  );
}

function loadPayPal(clientId) {
  if (window.paypal) return Promise.resolve();
  return new Promise((resolve, reject) => {
    const script = document.createElement("script");
    script.src = `https://www.paypal.com/sdk/js?client-id=${encodeURIComponent(clientId)}&currency=USD&intent=capture&components=buttons`;
    script.onload = () => resolve();
    script.onerror = () => reject(new Error("The PayPal checkout script did not load."));
    document.body.appendChild(script);
  });
}
