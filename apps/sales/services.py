"""
The money engine.

Every cedi that moves through this shop moves through this module. The browser
is treated as hostile: it may suggest a price, but the server decides what a
sale is worth, whether the stock exists, and whether the drawer balances.

Invariants enforced here:
  1. Totals are computed from products and batches, never trusted from the client.
  2. Stock cannot go negative without an explicit, logged correction.
  3. A pair price splits across pieces with zero rounding loss.
  4. Nothing sells below cost + the shop's minimum margin without a manager
     authorising it, and every authorisation is recorded on the line.
  5. Credit never exceeds the customer's available credit.
  6. Change can only be given out of cash that was actually tendered.
  7. Every payment row is bound to the drawer that received it.
"""
from dataclasses import dataclass, field
from decimal import Decimal

from django.db import connection, transaction
from django.db.models import F, Sum
from django.utils import timezone

from apps.core.money import D, ZERO, allocate, q2
from apps.inventory.models import StockAdjustment, StockBatch
from apps.products.models import Product, resolve_promotions

from .models import (
    RegisterSession, Sale, SaleItem, SaleItemBatch, SalePayment,
)

MANAGER_ROLES = ('OWNER', 'MANAGER')
# Tolerance when comparing the browser's arithmetic to ours: half a pesewa.
TOTAL_TOLERANCE = Decimal('0.005')


class SaleError(Exception):
    """A refusal the cashier needs to see and act on."""

    def __init__(self, message, code='REJECTED', detail=None):
        super().__init__(message)
        self.message = message
        self.code = code
        self.detail = detail or {}


# ---------------------------------------------------------------------------
# CONTEXT
# ---------------------------------------------------------------------------
def resolve_location(user):
    """
    Where is this user working right now?

    Roaming staff get `current_session_location`; everyone else falls back to
    their permanent posting.
    """
    return user.current_session_location or user.assigned_location


def get_open_session(user, location=None):
    location = location or resolve_location(user)
    if location is None:
        return None
    return RegisterSession.objects.filter(
        user=user, location=location, status=RegisterSession.Status.OPEN
    ).first()


def _lock(queryset):
    """
    select_for_update where the database supports it.

    Postgres (production) gets real row locks so two tills cannot sell the same
    last headlight. SQLite (local dev) has no row locking; the surrounding
    atomic block plus its write lock is as good as it gets there.
    """
    if connection.features.has_select_for_update:
        return queryset.select_for_update()
    return queryset


# ---------------------------------------------------------------------------
# PRICING / QUOTING
# ---------------------------------------------------------------------------
@dataclass
class QuotedLine:
    product: Product
    quantity: int
    sell_mode: str
    pieces_per_unit: int
    list_price: Decimal
    normal_price: Decimal
    unit_price: Decimal
    unit_cost: Decimal
    floor: Decimal
    below_floor: bool
    promotion: object
    note: str
    line_total: Decimal
    pieces_needed: int

    @property
    def discount_amount(self):
        """How far below the NORMAL price this line went, promotion included."""
        gap = q2(self.normal_price - self.unit_price) * self.quantity
        return q2(max(gap, ZERO))


@dataclass
class Quote:
    lines: list = field(default_factory=list)
    subtotal: Decimal = ZERO
    tax: Decimal = ZERO
    total: Decimal = ZERO

    @property
    def has_override(self):
        """True when a line is under the normal margin floor -- only promotions can do that."""
        return any(line.below_floor for line in self.lines)

    @property
    def promotions(self):
        seen = {}
        for line in self.lines:
            if line.promotion is not None:
                seen[line.promotion.id] = line.promotion
        return list(seen.values())


def _normalise_sell_mode(product, raw_mode):
    """A non-paired part is always sold by the piece, whatever the client claims."""
    mode = (raw_mode or SaleItem.SellMode.PIECE).upper()
    if not product.is_sold_in_pairs:
        return SaleItem.SellMode.PIECE
    if mode not in (SaleItem.SellMode.PAIR, SaleItem.SellMode.SINGLE):
        return SaleItem.SellMode.PAIR
    return mode


def quote_cart(cart, location, user, wholesale=False):
    """
    Turn a raw cart payload into a priced, validated quote.

    Read-only: touches no rows, so it is safe to call from a preview endpoint.
    Raises SaleError on anything the cashier must fix.
    """
    if not cart:
        raise SaleError("The cart is empty.", code='EMPTY_CART')

    product_ids = []
    for raw in cart:
        try:
            product_ids.append(int(raw.get('id')))
        except (TypeError, ValueError):
            raise SaleError("The cart contains an unrecognised item. Clear it and start again.",
                            code='BAD_CART')

    products = {p.id: p for p in Product.objects.filter(id__in=product_ids)}

    # One promotion lookup for the whole cart, not one per line.
    promo_map, _suspended = resolve_promotions(products.values(), location)

    quote = Quote()
    for raw in cart:
        product = products.get(int(raw['id']))
        if product is None:
            raise SaleError("One of the parts in the cart no longer exists. Clear it and start again.",
                            code='UNKNOWN_PRODUCT')
        if not product.is_active:
            raise SaleError(f"'{product.name}' has been deactivated and cannot be sold.",
                            code='INACTIVE_PRODUCT')

        try:
            quantity = int(raw.get('qty', 0))
        except (TypeError, ValueError):
            quantity = 0
        if quantity <= 0:
            raise SaleError(f"Quantity for '{product.name}' must be at least 1.", code='BAD_QTY')

        sell_mode = _normalise_sell_mode(product, raw.get('sellMode'))
        pieces_per_unit = product.pieces_per_unit(sell_mode)

        promotion = (promo_map.get(product.id) or {}).get('promotion')
        normal_price = product.base_price(sell_mode, wholesale=wholesale)
        list_price = product.base_price(sell_mode, wholesale=wholesale, promotion=promotion)
        unit_cost = product.unit_cost(sell_mode)
        floor = product.price_floor(sell_mode, location, promotion=promotion, wholesale=wholesale)

        # The client may only propose a price. We validate it.
        raw_price = raw.get('price', None)
        try:
            unit_price = q2(raw_price) if raw_price not in (None, '') else list_price
        except ValueError:
            raise SaleError(f"The price entered for '{product.name}' is not a valid amount.",
                            code='BAD_PRICE')

        if unit_price <= 0:
            raise SaleError(f"'{product.name}' cannot be sold for nothing. Enter a price.",
                            code='ZERO_PRICE')

        # THE FLOOR IS ABSOLUTE. There is no counter-level override for any
        # role: the only thing that lowers it is a running Promotion, declared
        # in advance by a manager.
        cost_floor = product.price_floor(sell_mode, location)

        if unit_price < floor:
            if promotion is not None and floor < cost_floor:
                # The floor here IS the promotional price -- a deliberate
                # loss-leader that cannot be cut any further.
                raise SaleError(
                    f"PRICE TOO LOW: '{product.name}' is on promotion "
                    f"({promotion.name} — {promotion.discount_label}) at ₵{floor:,.2f}, "
                    f"which is already below cost. It cannot be discounted further.",
                    code='BELOW_FLOOR',
                    detail={'product_id': product.id, 'floor': str(floor),
                            'requested': str(unit_price), 'promotion': promotion.name},
                )
            raise SaleError(
                f"PRICE TOO LOW: '{product.name}' cannot go below ₵{floor:,.2f} "
                f"(landed cost ₵{unit_cost:,.2f} plus the shop's "
                f"{product.effective_min_margin(location)}% minimum margin). "
                f"Nobody can authorise this at the till — if the price must drop "
                f"further, a manager has to set up a promotion.",
                code='BELOW_FLOOR',
                detail={'product_id': product.id, 'floor': str(floor), 'requested': str(unit_price)},
            )

        # `below_floor` means "sold under the normal margin floor", which can
        # only happen through an authorised below-cost promotion.
        line_total = q2(unit_price * quantity)

        quote.lines.append(QuotedLine(
            product=product,
            quantity=quantity,
            sell_mode=sell_mode,
            pieces_per_unit=pieces_per_unit,
            list_price=list_price,
            normal_price=normal_price,
            unit_price=unit_price,
            unit_cost=unit_cost,
            floor=floor,
            below_floor=unit_price < cost_floor,
            promotion=promotion,
            note=(raw.get('note') or '').strip()[:255],
            line_total=line_total,
            pieces_needed=quantity * pieces_per_unit,
        ))

    quote.subtotal = q2(sum((line.line_total for line in quote.lines), ZERO))
    # Tax is configured but not charged at the till today. When it is, it lands
    # here and in SaleTax -- nowhere else.
    quote.tax = ZERO
    quote.total = q2(quote.subtotal + quote.tax)
    return quote


# ---------------------------------------------------------------------------
# STOCK
# ---------------------------------------------------------------------------
def available_pieces(product, location):
    total = StockBatch.objects.filter(
        product=product, location=location, quantity__gt=0
    ).aggregate(total=Sum('quantity'))['total']
    return total or 0


def _check_and_lock_stock(quote, location, user, allow_stock_correction=False):
    """
    Verify every part in the cart is really on the shelf, then hand back the
    locked batches in consumption order.

    Quantities are checked per PRODUCT, not per line, so two lines of the same
    headlight cannot each claim the same last piece.
    """
    needed_by_product = {}
    for line in quote.lines:
        needed_by_product.setdefault(line.product.id, 0)
        needed_by_product[line.product.id] += line.pieces_needed

    batches_by_product = {}
    for line in quote.lines:
        pid = line.product.id
        if pid in batches_by_product:
            continue

        batches = list(_lock(
            StockBatch.objects.filter(product_id=pid, location=location, quantity__gt=0)
        ).order_by('expiry_date', 'received_date', 'id'))
        on_hand = sum(b.quantity for b in batches)
        needed = needed_by_product[pid]

        if on_hand < needed:
            shortfall = needed - on_hand
            if not (allow_stock_correction and getattr(user, 'role', None) in MANAGER_ROLES):
                raise SaleError(
                    f"NOT ENOUGH STOCK: '{line.product.name}' has {on_hand} piece(s) at "
                    f"{location.name}, but this sale needs {needed}. "
                    f"Do a stock count correction first, or reduce the quantity.",
                    code='INSUFFICIENT_STOCK',
                    detail={'product_id': pid, 'on_hand': on_hand, 'needed': needed},
                )
            # Manager override: make the missing stock explicit rather than
            # letting the sale quietly invent it.
            correction = StockBatch.objects.create(
                product=line.product,
                location=location,
                quantity=shortfall,
                initial_quantity=shortfall,
                cost_price=line.product.cost_price,
                batch_number='STOCK-CORRECTION',
                notes=f"Found stock recorded during sale by {user.username}",
            )
            StockAdjustment.objects.create(
                location=location,
                batch=correction,
                adjusted_quantity=shortfall,
                reason=StockAdjustment.Reason.COUNT_CORRECTION,
                notes=(f"Uncounted stock of {line.product.sku} recorded at point of sale "
                       f"by {user.username}. Physical count was {shortfall} higher than the system."),
                performed_by=user,
            )
            batches.append(correction)

        batches_by_product[pid] = batches

    return batches_by_product


def _consume(batches, pieces_needed):
    """
    Pull `pieces_needed` off the front of the batch list (FEFO, then FIFO).

    Returns [(batch, pieces)] and mutates the in-memory quantities so a second
    line for the same product sees what the first one took.
    """
    taken = []
    remaining = pieces_needed
    for batch in batches:
        if remaining <= 0:
            break
        if batch.quantity <= 0:
            continue
        take = min(batch.quantity, remaining)
        batch.quantity -= take
        remaining -= take
        taken.append((batch, take))
    if remaining > 0:  # pragma: no cover - _check_and_lock_stock guarantees this
        raise SaleError("Stock changed while the sale was being processed. Please try again.",
                        code='STOCK_RACE')
    return taken


# ---------------------------------------------------------------------------
# PAYMENTS
def _validate_payments(payments, total):
    """
    Normalise the payment payload.

    Rejects unknown methods, non-amounts and CREDIT (a shortfall becomes debt
    on its own; an explicit "pay with credit" row would double-count it).
    """
    cleaned = []
    settled = ZERO
    cash_tendered = ZERO

    for raw in payments or []:
        method = (raw.get('method') or '').upper()
        if method not in SalePayment.PaymentMethod.values:
            raise SaleError(f"Unknown payment method '{method}'.", code='BAD_METHOD')
        if method == SalePayment.PaymentMethod.CREDIT:
            raise SaleError(
                "Do not tender 'CREDIT' as a payment. Leave the balance short and it is "
                "recorded as debt against the mechanic's account.",
                code='CREDIT_AS_PAYMENT',
            )
        try:
            amount = q2(raw.get('amount'))
        except ValueError:
            raise SaleError("One of the payment amounts is not a valid number.", code='BAD_AMOUNT')
        if amount < 0:
            raise SaleError("Payment amounts cannot be negative.", code='NEGATIVE_AMOUNT')
        if amount == 0:
            continue

        reference = (raw.get('reference') or '').strip()[:100]
        cleaned.append({'method': method, 'amount': amount, 'reference': reference})
        settled += amount
        if method == SalePayment.PaymentMethod.CASH:
            cash_tendered += amount

    settled = q2(settled)
    cash_tendered = q2(cash_tendered)
    change = q2(max(settled - q2(total), ZERO))

    if change > cash_tendered:
        raise SaleError(
            f"You cannot hand back ₵{change:,.2f} in change out of ₵{cash_tendered:,.2f} cash. "
            f"Charge the exact amount to the card/MoMo, or take the difference in cash.",
            code='CHANGE_EXCEEDS_CASH',
        )

    return cleaned, settled, change


def _check_credit(customer, shortfall):
    if shortfall <= 0:
        return
    if customer is None:
        raise SaleError(
            "SECURITY HALT: a credit sale needs a mechanic/customer on the invoice. "
            "Select or register one before handing over goods unpaid.",
            code='CREDIT_NEEDS_CUSTOMER',
        )
    available = customer.available_credit
    if shortfall > available:
        raise SaleError(
            f"CREDIT REJECTED: {customer.display_name} has a limit of ₵{D(customer.credit_limit):,.2f} "
            f"and already owes ₵{customer.current_debt:,.2f}, leaving ₵{available:,.2f} available. "
            f"This sale needs ₵{shortfall:,.2f} on credit. Please collect arrears first.",
            code='CREDIT_LIMIT',
            detail={'available': str(available), 'requested': str(shortfall)},
        )


# ---------------------------------------------------------------------------
# THE SALE
# ---------------------------------------------------------------------------
@transaction.atomic
def create_sale(*, user, cart, payments, customer=None, wholesale=False,
                expected_total=None, notes='', order_type=Sale.OrderType.WALK_IN,
                allow_stock_correction=False, salesperson=None):
    """
    Record a completed sale. All-or-nothing.

    `expected_total` is what the cashier was shown. If our arithmetic disagrees
    the sale is refused rather than quietly charging a different number.
    """
    location = resolve_location(user)
    if location is None:
        raise SaleError("Your account is not assigned to a shop. Ask the owner to set your location.",
                        code='NO_LOCATION')

    session = get_open_session(user, location)
    if session is None:
        raise SaleError("No open register session. Open your register before selling.",
                        code='NO_SESSION')

    quote = quote_cart(cart, location, user, wholesale=wholesale)

    if expected_total is not None:
        try:
            shown = q2(expected_total)
        except ValueError:
            raise SaleError("The total sent from the terminal is not a valid amount.", code='BAD_TOTAL')
        if abs(shown - quote.total) > TOTAL_TOLERANCE:
            raise SaleError(
                f"TOTAL MISMATCH: the terminal shows ₵{shown:,.2f} but the system prices this "
                f"cart at ₵{quote.total:,.2f}. Refresh the POS and rebuild the cart -- a price "
                f"may have changed while it was open.",
                code='TOTAL_MISMATCH',
                detail={'shown': str(shown), 'computed': str(quote.total)},
            )

    # Stock is checked before money: "that part is not on the shelf" is the more
    # fundamental problem, and the cashier should hear it first.
    batches_by_product = _check_and_lock_stock(
        quote, location, user, allow_stock_correction=allow_stock_correction
    )

    cleaned_payments, settled, change = _validate_payments(payments, quote.total)
    shortfall = q2(max(quote.total - settled, ZERO))
    _check_credit(customer, shortfall)

    sale = Sale.objects.create(
        location=location,
        cashier=user,
        salesperson=salesperson,
        register_session=session,
        customer=customer,
        subtotal=quote.subtotal,
        total_tax=quote.tax,
        total_amount=quote.total,
        discount_amount=q2(sum((line.discount_amount for line in quote.lines), ZERO)),
        status=Sale.Status.COMPLETED,
        order_type=order_type,
        notes=notes[:2000],
        has_price_override=quote.has_override,
        amount_paid=ZERO,
    )

    touched_products = []
    for line in quote.lines:
        taken = _consume(batches_by_product[line.product.id], line.pieces_needed)

        # Exact COGS: every piece is costed at the batch it came from.
        line_cost = q2(sum((D(batch.cost_price) * pieces for batch, pieces in taken), ZERO))

        item = SaleItem.objects.create(
            sale=sale,
            product=line.product,
            source_batch=taken[0][0] if taken else None,
            quantity=line.quantity,
            sell_mode=line.sell_mode,
            pieces_per_unit=line.pieces_per_unit,
            list_price=line.normal_price,
            unit_price=line.unit_price,
            unit_cost=q2(line_cost / line.quantity) if line.quantity else ZERO,
            total_cost=line_cost,
            discount_amount=line.discount_amount,
            total_price=line.line_total,
            below_floor=line.below_floor,
            promotion=line.promotion,
            override_reason=(f"Promotion: {line.promotion.name}" if line.promotion else ''),
            note=line.note,
        )

        for batch, pieces in taken:
            SaleItemBatch.objects.create(
                sale_item=item, batch=batch, pieces=pieces, unit_cost=q2(batch.cost_price)
            )
            batch.save(update_fields=['quantity'])

        touched_products.append(line.product)

    for raw in cleaned_payments:
        SalePayment.objects.create(
            sale=sale,
            register_session=session,
            amount=raw['amount'],
            payment_method=raw['method'],
            entry_type=SalePayment.EntryType.SALE,
            reference_id=raw['reference'],
            processed_by=user,
        )

    if change > 0:
        SalePayment.objects.create(
            sale=sale,
            register_session=session,
            amount=-change,
            payment_method=SalePayment.PaymentMethod.CASH,
            entry_type=SalePayment.EntryType.CHANGE,
            reference_id='CHANGE GIVEN',
            processed_by=user,
        )

    sale.recalculate_totals()
    sale.refresh_payment_totals()
    session.recalculate()

    # Stock just moved, so the weighted-average cost moved with it.
    for product in {p.id: p for p in touched_products}.values():
        product.resync_cost_price(changed_by=user, note=f"After sale {sale.invoice_number}")

    if customer is not None:
        customer.recalculate_lifetime_stats()

    return sale


# ---------------------------------------------------------------------------
# DEBT SETTLEMENT
# ---------------------------------------------------------------------------
@transaction.atomic
def settle_debt(*, sale, user, amount, method, reference=''):
    """Take a payment against an existing invoice, into the CURRENT drawer."""
    location = resolve_location(user)
    session = get_open_session(user, location)
    if session is None:
        raise SaleError("You must have an open register to accept a payment.", code='NO_SESSION')

    if method not in SalePayment.PaymentMethod.values or method == SalePayment.PaymentMethod.CREDIT:
        raise SaleError("Choose a real payment method (Cash, MoMo, Card, Bank or Cheque).",
                        code='BAD_METHOD')

    try:
        amount = q2(amount)
    except ValueError:
        raise SaleError("That is not a valid amount.", code='BAD_AMOUNT')
    if amount <= 0:
        raise SaleError("Enter an amount greater than zero.", code='BAD_AMOUNT')

    if sale.status in (Sale.Status.CANCELLED, Sale.Status.REFUNDED):
        raise SaleError(f"Invoice {sale.invoice_number} is {sale.get_status_display().lower()} "
                        f"and cannot take a payment.", code='CLOSED_SALE')

    outstanding = sale.balance_remaining
    if outstanding <= 0:
        raise SaleError(f"Invoice {sale.invoice_number} is already fully paid.", code='ALREADY_PAID')
    if amount > outstanding:
        raise SaleError(
            f"That is more than is owed. Invoice {sale.invoice_number} has ₵{outstanding:,.2f} "
            f"outstanding -- collect that amount, or record the extra as a separate deposit.",
            code='OVERPAYMENT',
            detail={'outstanding': str(outstanding)},
        )

    payment = SalePayment.objects.create(
        sale=sale,
        register_session=session,
        amount=amount,
        payment_method=method,
        entry_type=SalePayment.EntryType.DEBT,
        reference_id=(reference or '')[:100],
        processed_by=user,
    )

    sale.refresh_payment_totals()
    session.recalculate()
    if sale.customer_id:
        sale.customer.recalculate_lifetime_stats()

    return payment


# ---------------------------------------------------------------------------
# REFUNDS
# ---------------------------------------------------------------------------
@transaction.atomic
def refund_sale(*, sale, user, lines, reason='Customer return', refund_method='CASH', restock=True):
    """
    Refund whole units off an invoice and put the pieces back where they came from.

    `lines` is [{'item_id': int, 'quantity': int}]. Money goes back in this order:
      1. against any outstanding debt on the invoice (nothing leaves the drawer),
      2. the remainder in cash/MoMo out of the current drawer.
    """
    if getattr(user, 'role', None) not in MANAGER_ROLES:
        raise SaleError("Only a manager or the owner can process a refund.", code='FORBIDDEN')

    if sale.status in (Sale.Status.CANCELLED, Sale.Status.REFUNDED):
        raise SaleError(f"Invoice {sale.invoice_number} has already been fully refunded.",
                        code='ALREADY_REFUNDED')

    location = resolve_location(user)
    session = get_open_session(user, location)

    requested = {}
    for raw in lines or []:
        try:
            item_id = int(raw['item_id'])
            quantity = int(raw['quantity'])
        except (KeyError, TypeError, ValueError):
            raise SaleError("The refund selection could not be read.", code='BAD_REFUND')
        if quantity > 0:
            requested[item_id] = requested.get(item_id, 0) + quantity

    if not requested:
        raise SaleError("Select at least one item and quantity to refund.", code='NOTHING_SELECTED')

    items = {i.id: i for i in sale.items.select_related('product').filter(id__in=requested.keys())}
    if len(items) != len(requested):
        raise SaleError("One of the selected lines is not on this invoice.", code='BAD_REFUND')

    refund_value = ZERO
    touched_products = []

    for item_id, quantity in requested.items():
        item = items[item_id]
        if quantity > item.quantity_active:
            raise SaleError(
                f"'{item.product.name}': only {item.quantity_active} of {item.quantity} "
                f"unit(s) are still refundable.",
                code='REFUND_TOO_MANY',
            )
        if not item.product.is_returnable:
            raise SaleError(f"'{item.product.name}' is marked as non-returnable.", code='NOT_RETURNABLE')

        # Value returned: the line's charged value spread evenly over its units,
        # so refunding 1 of 3 units never over- or under-pays by a pesewa.
        per_unit = allocate(item.total_price, [1] * item.quantity)
        already = item.quantity_refunded
        line_refund = q2(sum(per_unit[already:already + quantity], ZERO))

        pieces_to_return = quantity * item.pieces_per_unit
        cost_returned = ZERO

        if restock:
            for allocation in item.batch_lines.select_related('batch').order_by('-id'):
                if pieces_to_return <= 0:
                    break
                returnable = allocation.pieces - allocation.pieces_restocked
                if returnable <= 0:
                    continue
                give_back = min(returnable, pieces_to_return)

                batch = allocation.batch
                StockBatch.objects.filter(pk=batch.pk).update(quantity=F('quantity') + give_back)
                StockAdjustment.objects.create(
                    location=sale.location,
                    batch=batch,
                    adjusted_quantity=give_back,
                    reason=StockAdjustment.Reason.RETURN,
                    notes=f"Refund on invoice {sale.invoice_number}: {reason}",
                    performed_by=user,
                )
                allocation.pieces_restocked += give_back
                allocation.save(update_fields=['pieces_restocked'])

                cost_returned += q2(D(allocation.unit_cost) * give_back)
                pieces_to_return -= give_back

        item.quantity_refunded = already + quantity
        item.refunded_amount = q2(D(item.refunded_amount) + line_refund)
        item.is_refunded = item.quantity_refunded >= item.quantity
        # COGS must shrink by exactly what went back on the shelf.
        item.total_cost = q2(max(D(item.total_cost) - cost_returned, ZERO))
        item.save(update_fields=['quantity_refunded', 'refunded_amount', 'is_refunded', 'total_cost'])

        refund_value += line_refund
        touched_products.append(item.product)

    refund_value = q2(refund_value)

    # Refund first offsets debt, then leaves the drawer.
    outstanding_before = sale.balance_remaining
    offset_against_debt = min(refund_value, outstanding_before)
    cash_back = q2(refund_value - offset_against_debt)

    if cash_back > 0:
        if refund_method not in SalePayment.PaymentMethod.values or \
                refund_method == SalePayment.PaymentMethod.CREDIT:
            raise SaleError("Choose how the money is going back (Cash, MoMo, Card, Bank).",
                            code='BAD_METHOD')
        if refund_method == SalePayment.PaymentMethod.CASH and session is None:
            raise SaleError("Open your register before paying a cash refund out of the drawer.",
                            code='NO_SESSION')
        SalePayment.objects.create(
            sale=sale,
            register_session=session,
            amount=-cash_back,
            payment_method=refund_method,
            entry_type=SalePayment.EntryType.REFUND,
            reference_id=f"REFUND: {reason}"[:100],
            processed_by=user,
        )

    sale.recalculate_totals()
    sale.refresh_payment_totals()

    all_refunded = not sale.items.filter(quantity_refunded__lt=F('quantity')).exists()
    sale.status = Sale.Status.REFUNDED if all_refunded else Sale.Status.PARTIAL_REFUND
    sale.save(update_fields=['status', 'updated_at'])

    if session is not None:
        session.recalculate()
    for product in {p.id: p for p in touched_products}.values():
        product.resync_cost_price(changed_by=user, note=f"After refund on {sale.invoice_number}")
    if sale.customer_id:
        sale.customer.recalculate_lifetime_stats()

    return {
        'refund_value': refund_value,
        'offset_against_debt': offset_against_debt,
        'cash_back': cash_back,
        'status': sale.status,
    }


# ---------------------------------------------------------------------------
# SHIFT CLOSE
# ---------------------------------------------------------------------------
@transaction.atomic
def close_session(*, session, user, actual_cash, notes=''):
    """Count the drawer, freeze the expected figure, flag any variance."""
    if session.status != RegisterSession.Status.OPEN:
        raise SaleError("This register session is already closed.", code='ALREADY_CLOSED')

    try:
        actual_cash = q2(actual_cash)
    except ValueError:
        raise SaleError("Enter the counted cash as a number.", code='BAD_AMOUNT')
    if actual_cash < 0:
        raise SaleError("Counted cash cannot be negative.", code='BAD_AMOUNT')

    session.recalculate(save=False)
    session.closing_balance_expected = session.expected_cash
    session.closing_balance_actual = actual_cash
    session.end_time = timezone.now()
    session.closed_by = user
    session.notes = notes
    session.status = (RegisterSession.Status.CLOSED
                      if actual_cash == session.closing_balance_expected
                      else RegisterSession.Status.DISCREPANCY)
    session.save()
    return session
