from decimal import Decimal

from django.db import models, transaction
from django.conf import settings
from django.db.models import Sum
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseRetailModel, TimeStampedModel
from apps.core.money import D, ZERO, q2


class RegisterSession(BaseRetailModel):
    """
    CRITICAL FOR AUDITS: Tracks a Cashier's Shift.
    You cannot run a secure shop without knowing how much cash
    started in the drawer and how much ended in the drawer.

    The stored totals are a CACHE. `recalculate()` rebuilds them from the
    payment rows, which are the only source of truth. Nothing in this system
    should ever do `session.total_cash_sales += x` -- two cashiers on two
    tabs would lose one of the updates.
    """

    class Status(models.TextChoices):
        OPEN = 'OPEN', _('Open')
        CLOSED = 'CLOSED', _('Closed')
        DISCREPANCY = 'DISCREPANCY', _('Closed with Discrepancy')

    location = models.ForeignKey('location.Location', on_delete=models.PROTECT, related_name='register_sessions')
    user = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='sessions')

    opening_balance = models.DecimalField(max_digits=12, decimal_places=2, help_text="Cash amount in drawer at start")
    closing_balance_expected = models.DecimalField(max_digits=12, decimal_places=2, default=0.00,
                                                   help_text="System calculated expected cash")
    closing_balance_actual = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                                 help_text="Actual cash counted by cashier")

    # Financial Summary of the Shift (cache -- rebuilt by recalculate())
    total_cash_sales = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_momo_sales = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_card_sales = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_bank_sales = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_cheque_sales = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_credit_extended = models.DecimalField(max_digits=12, decimal_places=2, default=0.00,
                                                help_text="Value handed over unpaid during this shift")
    total_cash_refunds = models.DecimalField(max_digits=12, decimal_places=2, default=0.00,
                                             help_text="Cash paid back out of the drawer")
    total_till_expenses = models.DecimalField(max_digits=12, decimal_places=2, default=0.00,
                                              help_text="Cash taken out of the drawer for expenses")

    start_time = models.DateTimeField(auto_now_add=True)
    end_time = models.DateTimeField(null=True, blank=True)

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.OPEN)
    closed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='closed_sessions')
    notes = models.TextField(blank=True, help_text="Explanation for discrepancies")

    class Meta:
        ordering = ['-start_time']
        indexes = [models.Index(fields=['location', 'status'])]

    def __str__(self):
        return f"Session: {self.user.username} @ {self.location.name} ({self.start_time.date()})"

    # ---------------------------------------------------------
    # DERIVED MONEY -- always computed from payment rows
    # ---------------------------------------------------------
    def _payment_totals(self):
        """Signed sums per method. CHANGE and REFUND rows are already negative."""
        rows = SalePayment.objects.filter(register_session=self).values('payment_method').annotate(
            total=Sum('amount')
        )
        return {row['payment_method']: q2(row['total'] or 0) for row in rows}

    def till_expense_total(self):
        """Approved expenses paid out of this drawer."""
        from apps.finance.models import Expense
        total = Expense.objects.filter(
            register_session=self,
            is_paid_from_till=True,
        ).exclude(status=Expense.Status.REJECTED).aggregate(total=Sum('amount'))['total']
        return q2(total or 0)

    @property
    def cash_movement(self):
        """Net cash that passed through the drawer: sales + debt settlements - change - refunds."""
        return self._payment_totals().get(SalePayment.PaymentMethod.CASH, ZERO)

    @property
    def expected_cash(self):
        """
        What should physically be in the drawer right now.

        opening float
          + every cedi of cash taken in (sales and debt settlements)
          - change handed back
          - cash refunds paid out
          - petty cash spent from the till
        """
        return q2(D(self.opening_balance) + self.cash_movement - self.till_expense_total())

    @property
    def discrepancy(self):
        if self.closing_balance_actual is not None:
            return q2(D(self.closing_balance_actual) - D(self.closing_balance_expected))
        return ZERO

    @transaction.atomic
    def recalculate(self, save=True):
        """Rebuild the cached totals from the payment and expense rows."""
        totals = self._payment_totals()
        M = SalePayment.PaymentMethod

        refunds = SalePayment.objects.filter(
            register_session=self,
            entry_type=SalePayment.EntryType.REFUND,
            payment_method=M.CASH,
        ).aggregate(total=Sum('amount'))['total'] or 0

        credit = Sale.objects.filter(register_session=self).exclude(
            status__in=[Sale.Status.CANCELLED, Sale.Status.REFUNDED]
        ).aggregate(
            owed=Sum(models.F('total_amount') - models.F('amount_paid'))
        )['owed'] or 0

        self.total_cash_sales = totals.get(M.CASH, ZERO)
        self.total_momo_sales = totals.get(M.MOMO, ZERO)
        self.total_card_sales = totals.get(M.CARD, ZERO)
        self.total_bank_sales = totals.get(M.BANK_TRANSFER, ZERO)
        self.total_cheque_sales = totals.get(M.CHEQUE, ZERO)
        self.total_cash_refunds = q2(abs(D(refunds)))
        self.total_till_expenses = self.till_expense_total()
        self.total_credit_extended = q2(max(D(credit), ZERO))
        self.closing_balance_expected = self.expected_cash

        if save:
            self.save(update_fields=[
                'total_cash_sales', 'total_momo_sales', 'total_card_sales',
                'total_bank_sales', 'total_cheque_sales', 'total_cash_refunds',
                'total_till_expenses', 'total_credit_extended',
                'closing_balance_expected', 'updated_at',
            ])
        return self

    @property
    def total_digital_sales(self):
        return q2(D(self.total_momo_sales) + D(self.total_card_sales)
                  + D(self.total_bank_sales) + D(self.total_cheque_sales))

    @property
    def total_collected(self):
        """All money actually taken in this shift, by any method."""
        return q2(D(self.total_cash_sales) + self.total_digital_sales)


class Sale(BaseRetailModel):
    """
    Represents a Customer Transaction (Receipt/Invoice).
    """

    class Status(models.TextChoices):
        PENDING_PAYMENT = 'PENDING', _('Pending Payment / Draft')
        COMPLETED = 'COMPLETED', _('Completed')
        CANCELLED = 'CANCELLED', _('Cancelled')
        REFUNDED = 'REFUNDED', _('Fully Refunded')
        PARTIAL_REFUND = 'PARTIAL', _('Partially Refunded')
        ON_HOLD = 'HOLD', _('On Hold / Parked')

    # Statuses that still represent real revenue on the books.
    REVENUE_STATUSES = ['COMPLETED', 'PARTIAL']

    class OrderType(models.TextChoices):
        WALK_IN = 'WALK_IN', _('Walk-In')
        DELIVERY = 'DELIVERY', _('Delivery')
        PICKUP = 'PICKUP', _('Store Pickup')
        ONLINE = 'ONLINE', _('Online Order')

    class FulfillmentStatus(models.TextChoices):
        FULFILLED = 'FULFILLED', _('Items Handed Over')
        UNFULFILLED = 'UNFULFILLED', _('Pending Delivery/Pickup')
        PARTIAL = 'PARTIAL', _('Partially Fulfilled')

    # Identifiers
    invoice_number = models.CharField(max_length=50, unique=True, editable=False, db_index=True)

    # Context
    location = models.ForeignKey('location.Location', on_delete=models.PROTECT, related_name='sales')

    # The "Audit Link" - Which drawer session did this money go into?
    register_session = models.ForeignKey(RegisterSession, on_delete=models.PROTECT, null=True, blank=True,
                                         related_name='sales')

    # Roles
    # Cashier is nullable because a salesperson might create a DRAFT sale before a cashier touches it.
    cashier = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.PROTECT, related_name='processed_sales',
                                null=True, blank=True)
    salesperson = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='assisted_sales')
    customer = models.ForeignKey('customers.Customer', on_delete=models.SET_NULL, null=True, blank=True,
                                 related_name='purchases')

    # ---------------------------------------------------------
    # FINANCIAL SUMMARY (Calculated Fields)
    # ---------------------------------------------------------
    subtotal = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, help_text="Sum of items before tax")
    total_tax = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00, help_text="Final Bill Amount")
    total_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0.00,
                                     help_text="COGS snapshot: exact cost of the batches this sale consumed")

    # Payment Tracking
    amount_paid = models.DecimalField(max_digits=12, decimal_places=2, default=0.00,
                                      help_text="Sum of all SalePayments")
    change_due = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00,
                                          help_text="Value of goods refunded off this invoice")

    # Context Flags
    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING_PAYMENT)
    order_type = models.CharField(max_length=20, choices=OrderType.choices, default=OrderType.WALK_IN)
    fulfillment_status = models.CharField(max_length=20, choices=FulfillmentStatus.choices,
                                          default=FulfillmentStatus.FULFILLED)

    has_price_override = models.BooleanField(default=False,
                                             help_text="A line on this sale was authorised below the margin floor")

    notes = models.TextField(blank=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [
            models.Index(fields=['location', '-created_at']),
            models.Index(fields=['status', '-created_at']),
        ]

    def save(self, *args, **kwargs):
        if not self.invoice_number:
            import uuid, time
            self.invoice_number = f"INV-{int(time.time())}-{uuid.uuid4().hex[:4].upper()}"
        super().save(*args, **kwargs)

    # ---------------------------------------------------------
    # MONEY
    # ---------------------------------------------------------
    @property
    def net_amount(self):
        """What the customer owes after refunds -- the real value of this invoice."""
        return q2(D(self.total_amount) - D(self.refunded_amount))

    @property
    def is_fully_paid(self):
        return q2(self.amount_paid) >= self.net_amount

    @property
    def balance_remaining(self):
        return max(self.net_amount - q2(self.amount_paid), ZERO)

    @property
    def is_credit_sale(self):
        return self.balance_remaining > 0

    @property
    def gross_profit(self):
        return q2(self.net_amount - D(self.total_cost))

    def recalculate_totals(self, save=True):
        """
        Rebuild the invoice from its line items. The server -- never the
        browser -- decides what a sale is worth.

        `subtotal` and `total_amount` stay at the GROSS value that was billed;
        refunds are tracked separately in `refunded_amount`. Netting them into
        the total as well would subtract every return twice.
        """
        lines = list(self.items.all())
        subtotal = q2(sum((D(line.total_price) for line in lines), ZERO))
        discount = q2(sum((D(line.discount_amount) for line in lines), ZERO))
        cost = q2(sum((D(line.total_cost) for line in lines), ZERO))
        tax = q2(self.taxes.aggregate(total=Sum('tax_amount'))['total'] or 0)
        refunded = q2(sum((D(line.refunded_amount) for line in lines), ZERO))

        self.subtotal = subtotal
        self.discount_amount = discount
        self.total_tax = tax
        self.total_amount = q2(subtotal + tax)
        self.total_cost = cost
        self.refunded_amount = refunded

        if save:
            self.save(update_fields=['subtotal', 'discount_amount', 'total_tax', 'total_amount',
                                     'total_cost', 'refunded_amount', 'updated_at'])
        return self

    def refresh_payment_totals(self, save=True):
        """
        Recompute amount_paid from the payment rows (never increment in place).

        Every row is signed, so this one sum handles all four cases: money in
        at the till, debt settled later, change handed back, refunds paid out.
        Tender 50 for a 45 bill and amount_paid is 45, not 50.
        """
        net = SalePayment.objects.filter(sale=self).aggregate(total=Sum('amount'))['total'] or 0

        change = SalePayment.objects.filter(
            sale=self, entry_type=SalePayment.EntryType.CHANGE
        ).aggregate(total=Sum('amount'))['total'] or 0

        self.amount_paid = q2(net)
        self.change_due = q2(abs(D(change)))
        if save:
            self.save(update_fields=['amount_paid', 'change_due', 'updated_at'])
        return self

    def __str__(self):
        return f"{self.invoice_number} - {self.total_amount}"


class SaleItem(models.Model):
    """
    Individual Line Items on the Receipt.

    `quantity` is in BILLED UNITS, not pieces: one pair is quantity=1 with
    pieces_per_unit=2, priced at the pair price. The physical pieces that left
    the shelf, and which batch each came from, are recorded in SaleItemBatch --
    so the receipt reads the way the customer bought it while COGS stays exact.
    """

    class SellMode(models.TextChoices):
        PIECE = 'PIECE', _('Piece')
        PAIR = 'PAIR', _('Pair')
        SINGLE = 'SINGLE', _('Single (broken pair)')

    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey('products.Product', on_delete=models.PROTECT)
    # Primary batch for quick traceability; the full picture is in .batch_lines
    source_batch = models.ForeignKey('inventory.StockBatch', on_delete=models.PROTECT, null=True, blank=True)

    quantity = models.PositiveIntegerField(default=1, help_text="Billed units (pairs, singles or pieces)")
    sell_mode = models.CharField(max_length=10, choices=SellMode.choices, default=SellMode.PIECE)
    pieces_per_unit = models.PositiveSmallIntegerField(default=1, help_text="2 for a pair, otherwise 1")

    # Locking price at moment of sale
    list_price = models.DecimalField(max_digits=12, decimal_places=2, default=0.00,
                                     help_text="System price before haggling")
    unit_price = models.DecimalField(max_digits=12, decimal_places=2, help_text="Price actually charged per unit")
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, help_text="Landed cost per unit (average)")
    total_cost = models.DecimalField(max_digits=12, decimal_places=2, default=0.00,
                                     help_text="Exact cost of the batches consumed -- this is COGS")

    discount_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_price = models.DecimalField(max_digits=12, decimal_places=2)

    # Margin floor governance. The floor is absolute at the till; the only way a
    # line lands below it is an authorised, dated promotion, recorded here.
    below_floor = models.BooleanField(default=False,
                                      help_text="Sold under cost + the shop's minimum margin")
    promotion = models.ForeignKey('products.Promotion', on_delete=models.SET_NULL, null=True, blank=True,
                                  related_name='sale_items',
                                  help_text="The promotion that authorised this price")
    override_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True,
                                    related_name='price_overrides',
                                    help_text="Legacy: manager who authorised a till-level override")
    override_reason = models.CharField(max_length=255, blank=True)

    # Refunds
    is_refunded = models.BooleanField(default=False, help_text="True once every unit on this line is refunded")
    quantity_refunded = models.PositiveIntegerField(default=0)
    refunded_amount = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    # Special instructions (e.g., "Gift Wrap", "No Onions", "Display Unit")
    note = models.CharField(max_length=255, blank=True)

    class Meta:
        indexes = [models.Index(fields=['sale']), models.Index(fields=['product'])]

    def save(self, *args, **kwargs):
        if not self.total_price:
            self.total_price = q2(D(self.unit_price) * self.quantity - D(self.discount_amount))
        super().save(*args, **kwargs)

    @property
    def pieces(self):
        """Physical pieces this line removed from the shelf."""
        return self.quantity * self.pieces_per_unit

    @property
    def line_total(self):
        """Charged value of the line net of refunded units."""
        return q2(D(self.total_price) - D(self.refunded_amount))

    @property
    def quantity_active(self):
        return max(self.quantity - self.quantity_refunded, 0)

    @property
    def gross_profit(self):
        return q2(self.line_total - D(self.total_cost))

    @property
    def margin_percentage(self):
        if self.line_total <= 0:
            return ZERO
        return q2((self.line_total - D(self.total_cost)) / self.line_total * Decimal('100'))

    @property
    def mode_label(self):
        if self.sell_mode == self.SellMode.PAIR:
            return 'PAIR'
        if self.sell_mode == self.SellMode.SINGLE:
            return 'SINGLE'
        return ''

    def __str__(self):
        return f"{self.quantity}x {self.product.name} ({self.sell_mode})"


class SaleItemBatch(models.Model):
    """
    Exactly which pieces came out of which batch for a line item.

    This is what makes COGS defensible: a pair sold across two batches books
    the true cost of both pieces, and a refund knows precisely where to put
    each piece back.
    """
    sale_item = models.ForeignKey(SaleItem, on_delete=models.CASCADE, related_name='batch_lines')
    batch = models.ForeignKey('inventory.StockBatch', on_delete=models.PROTECT, related_name='sale_allocations')
    pieces = models.PositiveIntegerField()
    unit_cost = models.DecimalField(max_digits=12, decimal_places=2, help_text="Cost per piece from this batch")
    pieces_restocked = models.PositiveIntegerField(default=0)

    class Meta:
        indexes = [models.Index(fields=['batch'])]

    @property
    def total_cost(self):
        return q2(D(self.unit_cost) * self.pieces)

    def __str__(self):
        return f"{self.pieces} pcs from {self.batch_id} @ {self.unit_cost}"


class SaleTax(models.Model):
    """
    SNAPSHOT of taxes applied to this specific sale.
    """
    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name='taxes')
    tax_name = models.CharField(max_length=100)  # e.g., "VAT"
    tax_rate = models.DecimalField(max_digits=6, decimal_places=3)  # e.g., 12.5
    tax_amount = models.DecimalField(max_digits=12, decimal_places=2)

    def __str__(self):
        return f"{self.tax_name} ({self.tax_amount})"


class SalePayment(TimeStampedModel):
    """
    Handling Split Payments.

    Every row is a signed movement of money and is bound to the drawer
    (`register_session`) that physically received or released it -- a debt
    settled next Tuesday belongs to Tuesday's drawer, not to the shift that
    made the sale.
    """

    class PaymentMethod(models.TextChoices):
        CASH = 'CASH', _('Cash')
        MOMO = 'MOMO', _('Mobile Money')
        CARD = 'CARD', _('Card')
        BANK_TRANSFER = 'BANK', _('Bank Transfer')
        CHEQUE = 'CHEQUE', _('Cheque')
        CREDIT = 'CREDIT', _('Store Credit / On Account')

    # Methods that put real money in the drawer/account at the moment of sale.
    SETTLEMENT_METHODS = ['CASH', 'MOMO', 'CARD', 'BANK', 'CHEQUE']

    class EntryType(models.TextChoices):
        SALE = 'SALE', _('Payment at point of sale')
        DEBT = 'DEBT', _('Debt settlement')
        CHANGE = 'CHANGE', _('Change given back')
        REFUND = 'REFUND', _('Refund paid out')

    sale = models.ForeignKey(Sale, on_delete=models.CASCADE, related_name='payments')
    register_session = models.ForeignKey(RegisterSession, on_delete=models.PROTECT, null=True, blank=True,
                                          related_name='payments')
    amount = models.DecimalField(max_digits=12, decimal_places=2,
                                 help_text="Positive = money in. Negative = change or refund out.")
    payment_method = models.CharField(max_length=20, choices=PaymentMethod.choices)
    entry_type = models.CharField(max_length=10, choices=EntryType.choices, default=EntryType.SALE)

    # Proof of Payment
    reference_id = models.CharField(max_length=100, blank=True,
                                    help_text="Transaction ID, Cheque Number, or Receipt Ref")
    processed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)

    class Meta:
        ordering = ['created_at']
        indexes = [
            models.Index(fields=['register_session', 'payment_method']),
            models.Index(fields=['sale']),
        ]

    def __str__(self):
        return f"{self.payment_method}: {self.amount} for {self.sale.invoice_number}"


class Delivery(TimeStampedModel):
    """
    Tracks the logistics/fulfillment of a Sale.
    Supports both 3rd party providers (Yango/Uber) and In-House riders.
    """

    class Provider(models.TextChoices):
        YANGO = 'YANGO', _('Yango')
        UBER = 'UBER', _('Uber')
        BOLT = 'BOLT', _('Bolt')
        IN_HOUSE = 'IN_HOUSE', _('In-House Rider')
        DHL = 'DHL', _('DHL / FedEx')
        OTHER = 'OTHER', _('Other')

    class Status(models.TextChoices):
        PENDING = 'PENDING', _('Pending Pickup')
        DISPATCHED = 'DISPATCHED', _('Dispatched / In Transit')
        DELIVERED = 'DELIVERED', _('Delivered')
        FAILED = 'FAILED', _('Failed / Returned')

    # Link one-to-one with Sale. A sale usually has one delivery.
    sale = models.OneToOneField(Sale, on_delete=models.CASCADE, related_name='delivery_details')

    provider = models.CharField(max_length=20, choices=Provider.choices, default=Provider.IN_HOUSE)

    # Rider / Driver Details (Captured for verification)
    rider_name = models.CharField(max_length=255, blank=True, help_text="Name of the rider picking up")
    rider_phone = models.CharField(max_length=20, blank=True, help_text="Phone number for the rider")
    vehicle_details = models.CharField(max_length=255, blank=True, help_text="License Plate / Bike Model / Color")

    # Tracking
    tracking_reference = models.CharField(max_length=255, blank=True, help_text="Yango Link, Tracking ID, or Ride Ref")

    # Location
    destination_address = models.TextField(help_text="Full delivery address provided by customer")
    google_maps_link = models.URLField(blank=True, help_text="Pinned location link")

    # Financials (Logistics Profit/Loss)
    delivery_fee_charged = models.DecimalField(max_digits=10, decimal_places=2, default=0.00,
                                               help_text="Amount charged to customer on receipt")
    cost_to_business = models.DecimalField(max_digits=10, decimal_places=2, default=0.00,
                                           help_text="Actual cost paid to Yango/Rider")

    # Proof of Execution (Screenshots/Photos)
    proof_of_pickup = models.ImageField(upload_to='deliveries/pickup/', null=True, blank=True,
                                        help_text="Photo of rider with items or app screenshot")
    proof_of_delivery = models.ImageField(upload_to='deliveries/dropoff/', null=True, blank=True,
                                          help_text="Photo of item at destination")

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.PENDING)

    dispatched_at = models.DateTimeField(null=True, blank=True)
    delivered_at = models.DateTimeField(null=True, blank=True)

    notes = models.TextField(blank=True, help_text="Driver instructions or issues")

    @property
    def logistics_margin(self):
        return q2(D(self.delivery_fee_charged) - D(self.cost_to_business))

    def __str__(self):
        return f"{self.provider} Delivery for {self.sale.invoice_number}"
