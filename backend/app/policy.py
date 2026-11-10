import math
import re
from dataclasses import dataclass, field
from datetime import date

from app.catalog import CatalogItem

UNPAID_STATUSES = {"DRAFT", "SENT", "UNPAID", "SCHEDULED", "PARTIALLY_PAID", "PAYMENT_PENDING"}
PAID_STATUSES = {"PAID", "MARKED_AS_PAID"}

MAX_UNITS_PER_PRODUCT = 10_000
MAX_PACKS_PER_PRODUCT = 500

FILLER_WORDS = {
    "a", "an", "the", "of", "some", "more", "extra", "new", "x",
    "pack", "bundle", "case", "unit", "piece", "pc", "pcs",
}
PACKAGING_WORDS = {
    "bag", "carton", "box", "bottle", "jar", "tin", "can", "roll", "sack", "pouch", "packet", "tub",
    "kg", "kilo", "kilogram", "gram", "litre", "liter", "ml",
}
DESCRIPTOR_WORDS = {
    "recycled", "eco", "friendly", "environmentally", "compostable", "biodegradable", "sustainable",
    "house", "plain", "standard", "regular", "fresh",
}
CONFIDENT_SCORE = 2


@dataclass(frozen=True)
class ParsedItem:
    product_query: str
    quantity: int
    sku: str | None = None
    unit: str = "units"


@dataclass(frozen=True)
class RequestIssue:
    code: str
    message: str
    product: str | None = None


@dataclass(frozen=True)
class ParsedRequest:
    items: list[ParsedItem]
    budget_cents: int | None
    prefer_recycled: bool
    require_approval: bool
    parser: str
    ignored_model_price: bool = False
    hold_until_invoice_paid: bool = False
    issues: list[RequestIssue] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class CartLine:
    sku: str
    name: str
    pack_count: int
    pack_size: int
    units_covered: int
    unit_price_cents: int
    line_total_cents: int
    recycled: bool
    supplier: str
    requested_units: int


@dataclass
class Cart:
    lines: list[CartLine] = field(default_factory=list)
    total_cents: int = 0
    budget_cents: int | None = None
    supplier: str | None = None
    issues: list[RequestIssue] = field(default_factory=list)

    @property
    def problems(self) -> list[str]:
        return [issue.message for issue in self.issues]

    @property
    def over_budget(self) -> bool:
        return bool(self.lines) and self.budget_cents is not None and self.total_cents > self.budget_cents

    @property
    def ok(self) -> bool:
        return bool(self.lines) and not self.issues and not self.over_budget


@dataclass(frozen=True)
class InvoiceFacts:
    id: int | None
    customer_name: str
    description: str
    amount_cents: int
    due_date: date
    status: str
    role: str

    @property
    def unpaid(self) -> bool:
        return self.status in UNPAID_STATUSES


@dataclass
class Decision:
    status: str
    cart: Cart
    invoice: InvoiceFacts | None
    gap_cents: int
    book_cash_cents: int
    explanation: str
    audits: list[str]


def _singular(word: str) -> str:
    if word.endswith("ies") and len(word) > 4:
        return word[:-3] + "y"
    if word.endswith(("ches", "shes", "xes", "sses")):
        return word[:-2]
    if word.endswith("s") and not word.endswith("ss") and len(word) > 3:
        return word[:-1]
    return word


def terms(text: str) -> list[str]:
    words = [_singular(word) for word in re.findall(r"[a-z0-9]+", text.lower())]
    return [word for word in words if word not in FILLER_WORDS]


def family(sku: str) -> str:
    return sku.split("-", 1)[0].upper()


def match_key(query: str, item: CatalogItem) -> tuple[int, int]:
    if item.sku.upper() == query.strip().upper():
        return 6, 1
    query_terms = terms(query)
    if not query_terms:
        return 0, 0
    query_set = set(query_terms)
    head = query_terms[-1]
    best = (0, 0)
    for tag in item.tags:
        tag_set = set(terms(tag))
        if not tag_set:
            continue
        if query_set == tag_set:
            score = 5
        elif tag_set < query_set:
            # "bags of coffee beans" contains "coffee bean"; "coffee filters" only shares "coffee".
            leftover = query_set - tag_set
            score = 4 if leftover <= PACKAGING_WORDS | DESCRIPTOR_WORDS else 1
        elif query_set < tag_set and len(tag_set - query_set) == 1:
            score = 2
        elif query_set & tag_set:
            score = 1
        else:
            score = 0
        if score:
            best = max(best, (score, 1 if head in tag_set else 0))
    return best


def _suggestions(catalog: list[CatalogItem], query: str) -> str:
    query_set = set(terms(query))
    close = []
    for item in catalog:
        tag_terms = set()
        for tag in item.tags:
            tag_terms |= set(terms(tag))
        if query_set & tag_terms:
            close.append(item.name)
    if close:
        return "Closest matches: " + ", ".join(close[:3]) + "."
    return "Float stocks cups, takeaway containers, lids, napkins, straws, bags, sleeves, coffee, tea, oat milk, sugar, paper towels, and gloves."


def resolve_item(
    catalog: list[CatalogItem], item: ParsedItem, prefer_recycled: bool
) -> tuple[CatalogItem | None, RequestIssue | None]:
    label = item.product_query or item.sku or "this item"
    if item.sku:
        wanted = item.sku.strip().upper()
        by_sku = next((entry for entry in catalog if entry.sku.upper() == wanted), None)
        if by_sku is None:
            return None, RequestIssue(
                "unknown_sku",
                f"'{item.sku}' is not a product ID in the catalog. {_suggestions(catalog, item.product_query or '')}",
                label,
            )
        if item.product_query and item.product_query.strip().upper() != wanted:
            by_text, text_issue = resolve_item(catalog, ParsedItem(item.product_query, item.quantity), prefer_recycled)
            if text_issue is None and by_text is not None and family(by_text.sku) != family(by_sku.sku):
                return None, RequestIssue(
                    "sku_mismatch",
                    f"'{item.product_query}' does not match product ID {by_sku.sku} ({by_sku.name}). Say which one you want.",
                    label,
                )
        return by_sku, None

    query = item.product_query.strip()
    if not query:
        return None, RequestIssue("unknown_product", "A quantity was given without a product.", None)

    keyed = [(match_key(query, entry), entry) for entry in catalog]
    scored = [(key, entry) for key, entry in keyed if key[0] >= CONFIDENT_SCORE]
    if not scored:
        weak = sorted(
            ((key, entry) for key, entry in keyed if key[0] > 0),
            key=lambda pair: (pair[0], -pair[1].price_cents),
            reverse=True,
        )
        if weak:
            names = []
            for _, entry in weak:
                if entry.name not in names:
                    names.append(entry.name)
            return None, RequestIssue(
                "uncertain_match",
                f"Float isn't sure what '{query}' means. The closest catalog products are "
                f"{', '.join(names[:3])}, but they don't clearly match. Use the product's name or ID.",
                query,
            )
        return None, RequestIssue(
            "unknown_product",
            f"No catalog product matches '{query}'. {_suggestions(catalog, query)}",
            query,
        )

    best = max(key for key, _ in scored)
    top = [entry for key, entry in scored if key == best]
    families: dict[str, CatalogItem] = {}
    for entry in top:
        families.setdefault(family(entry.sku), entry)
    if len(families) > 1:
        options = [entry.name for entry in families.values()]
        listed = ", ".join(options[:-1]) + f", or {options[-1]}" if len(options) > 2 else " or ".join(options)
        example = next(iter(families.values())).sku
        return None, RequestIssue(
            "ambiguous_product",
            f"'{query}' could mean {listed}. Say which one, or use a product ID such as {example}.",
            query,
        )

    pool = top
    if prefer_recycled and any(entry.recycled for entry in pool):
        pool = [entry for entry in pool if entry.recycled]
    pool.sort(key=lambda entry: (entry.price_cents, entry.sku))
    return pool[0], None


def build_cart(catalog: list[CatalogItem], request: ParsedRequest) -> Cart:
    cart = Cart(budget_cents=request.budget_cents)
    cart.issues.extend(request.issues)
    if not request.items and not cart.issues:
        cart.issues.append(
            RequestIssue(
                "no_items",
                "Float could not find a quantity and a product. Try something like '100 paper cups and 50 takeaway containers'.",
            )
        )

    wanted_units: dict[str, int] = {}
    chosen_by_sku: dict[str, CatalogItem] = {}
    for item in request.items:
        label = item.product_query or item.sku or "this item"
        if isinstance(item.quantity, bool) or not isinstance(item.quantity, int) or item.quantity < 1:
            cart.issues.append(
                RequestIssue(
                    "invalid_quantity",
                    f"The quantity for {label} must be a whole number of at least 1.",
                    label,
                )
            )
            continue
        chosen, issue = resolve_item(catalog, item, request.prefer_recycled)
        if issue is not None:
            cart.issues.append(issue)
            continue
        units = item.quantity * chosen.pack_size if item.unit == "packs" else item.quantity
        chosen_by_sku[chosen.sku] = chosen
        wanted_units[chosen.sku] = wanted_units.get(chosen.sku, 0) + units

    suppliers: set[str] = set()
    for sku, units in wanted_units.items():
        chosen = chosen_by_sku[sku]
        packs = math.ceil(units / chosen.pack_size)
        if units > MAX_UNITS_PER_PRODUCT or packs > MAX_PACKS_PER_PRODUCT:
            cart.issues.append(
                RequestIssue(
                    "quantity_too_large",
                    f"{units:,} units of {chosen.name} is more than Float orders in one cart "
                    f"(limit {MAX_UNITS_PER_PRODUCT:,} units or {MAX_PACKS_PER_PRODUCT} packs per product).",
                    chosen.name,
                )
            )
            continue
        cart.lines.append(
            CartLine(
                sku=chosen.sku,
                name=chosen.name,
                pack_count=packs,
                pack_size=chosen.pack_size,
                units_covered=packs * chosen.pack_size,
                unit_price_cents=chosen.price_cents,
                line_total_cents=packs * chosen.price_cents,
                recycled=chosen.recycled,
                supplier=chosen.supplier,
                requested_units=units,
            )
        )
        suppliers.add(chosen.supplier)

    if cart.issues:
        cart.lines = []
        cart.total_cents = 0
        return cart

    if len(suppliers) > 1:
        cart.issues.append(
            RequestIssue(
                "multiple_suppliers",
                "This order would pay more than one supplier. Float pays one supplier per order.",
            )
        )
        cart.lines = []
        return cart

    cart.supplier = next(iter(suppliers)) if suppliers else None
    cart.total_cents = sum(line.line_total_cents for line in cart.lines)
    return cart


def days_overdue(due: date, today: date) -> int:
    return max(0, (today - due).days)


def choose_invoice(invoices: list[InvoiceFacts], gap_cents: int, today: date) -> InvoiceFacts | None:
    if gap_cents <= 0:
        return None
    eligible = [
        invoice
        for invoice in invoices
        if invoice.unpaid and invoice.amount_cents >= gap_cents
    ]
    eligible.sort(key=lambda invoice: (-days_overdue(invoice.due_date, today), invoice.amount_cents))
    return eligible[0] if eligible else None


def money(cents: int) -> str:
    sign = "-" if cents < 0 else ""
    cents = abs(cents)
    return f"{sign}${cents // 100}.{cents % 100:02d}"


def decide(
    book_cash_cents: int,
    cart: Cart,
    invoices: list[InvoiceFacts],
    today: date,
    ignored_model_price: bool = False,
) -> Decision:
    audits = [
        "Prices, pack sizes, and the cart total come from the café catalog.",
        "The server requires an explicit approval before it creates a PayPal order.",
    ]
    if ignored_model_price:
        audits.append("A price in the model output was ignored.")

    if not cart.lines:
        if cart.issues:
            count = len(cart.issues)
            reason = (
                "Float can't price this request yet. Fix the item below and try again."
                if count == 1
                else f"Float can't price this request yet. Fix the {count} items below and try again."
            )
        else:
            reason = "Nothing in the catalog matched the request."
        return Decision(
            status="REJECTED",
            cart=cart,
            invoice=None,
            gap_cents=0,
            book_cash_cents=book_cash_cents,
            explanation=reason,
            audits=audits,
        )

    if cart.over_budget:
        budget = cart.budget_cents or 0
        audits.append("Float kept your quantities and products. It did not shrink the order to fit the budget.")
        return Decision(
            status="OVER_BUDGET",
            cart=cart,
            invoice=None,
            gap_cents=0,
            book_cash_cents=book_cash_cents,
            explanation=(
                f"The cart is {money(cart.total_cents)} and your budget is {money(budget)}, "
                f"{money(cart.total_cents - budget)} over. Checkout stays off. "
                "Change the quantities or the budget and price it again."
            ),
            audits=audits,
        )

    gap = max(0, cart.total_cents - book_cash_cents)
    if gap == 0:
        audits.append("Book cash covers the cart, so no invoice has to be collected first.")
        return Decision(
            status="CLEAR",
            cart=cart,
            invoice=None,
            gap_cents=0,
            book_cash_cents=book_cash_cents,
            explanation=(
                f"Book cash is {money(book_cash_cents)}. The cart is {money(cart.total_cents)}. "
                "You can approve the supplier payment."
            ),
            audits=audits,
        )

    invoice = choose_invoice(invoices, gap, today)
    if invoice is None:
        audits.append("No unpaid invoice is large enough to close the gap.")
        return Decision(
            status="BLOCKED",
            cart=cart,
            invoice=None,
            gap_cents=gap,
            book_cash_cents=book_cash_cents,
            explanation=(
                f"Book cash is {money(book_cash_cents)} and the cart is {money(cart.total_cents)}. "
                f"The gap is {money(gap)}. No unpaid invoice covers it, so checkout stays off."
            ),
            audits=audits,
        )

    overdue = days_overdue(invoice.due_date, today)
    when = f"{overdue} days overdue" if overdue else "not overdue yet"
    audits.append(
        f"Chose {invoice.customer_name} because {money(invoice.amount_cents)} covers the "
        f"{money(gap)} gap and the invoice is {when}."
    )
    return Decision(
        status="BLOCKED",
        cart=cart,
        invoice=invoice,
        gap_cents=gap,
        book_cash_cents=book_cash_cents,
        explanation=(
            f"Book cash is {money(book_cash_cents)}. This cart is {money(cart.total_cents)}. "
            f"The gap is {money(gap)}. Send the {invoice.customer_name} invoice "
            f"for {money(invoice.amount_cents)}. Supplier checkout stays off until PayPal marks it paid."
        ),
        audits=audits,
    )
