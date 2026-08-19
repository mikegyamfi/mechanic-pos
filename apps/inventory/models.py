from decimal import Decimal

from django.db import models, transaction
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from apps.core.models import BaseRetailModel, TimeStampedModel
from apps.core.money import D, ZERO, q2
from apps.products.models import PriceChangeLog, Product


class Supplier(BaseRetailModel):
    name = models.CharField(max_length=255)
    contact_person = models.CharField(max_length=255, blank=True)
    email = models.EmailField(blank=True)
    phone = models.CharField(max_length=20, blank=True)
    address = models.TextField(blank=True)
    tax_id = models.CharField(max_length=100, blank=True, help_text="TIN / VAT Number")
    credit_period_days = models.PositiveIntegerField(default=0, help_text="Payment terms in days")

    def __str__(self):
        return self.name


class StockBatch(TimeStampedModel):
    """
    The CORE of the inventory system.
    Tracks a specific batch of products at a specific location.
    Essential for Expiry (FEFO) and Cost tracking.
    """
    product = models.ForeignKey('products.Product', on_delete=models.CASCADE, related_name='batches')
    location = models.ForeignKey('location.Location', on_delete=models.CASCADE, related_name='stock_batches')
    supplier = models.ForeignKey(Supplier, on_delete=models.SET_NULL, null=True, blank=True)

    batch_number = models.CharField(max_length=100, blank=True, db_index=True,
                                    help_text="Container / lot reference, e.g. LSA20251018")
    quantity = models.IntegerField(default=0, help_text="Pieces still on the shelf from this batch")
    initial_quantity = models.IntegerField(default=0, help_text="Pieces this batch arrived with (never changes)")

    # Financials per batch. This is the TRUE cost of these specific pieces and
    # is what COGS is booked against. Product.cost_price is the weighted
    # average of the batches on hand and is derived from these numbers.
    cost_price = models.DecimalField(max_digits=12, decimal_places=2, help_text="Cost per piece for this specific batch")

    # Dates
    manufactured_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True, db_index=True)
    received_date = models.DateTimeField(default=timezone.now)

    notes = models.CharField(max_length=255, blank=True)

    class Meta:
        # Consumption order. Spare parts rarely carry an expiry date, so with
        # nulls sorted LAST this degrades cleanly to FIFO (oldest stock first)
        # while still honouring FEFO for anything that does expire.
        ordering = [models.F('expiry_date').asc(nulls_last=True), 'received_date', 'id']
        indexes = [
            models.Index(fields=['product', 'location', 'expiry_date']),
            models.Index(fields=['location', 'quantity']),
        ]

    def save(self, *args, **kwargs):
        if self._state.adding and not self.initial_quantity:
            self.initial_quantity = self.quantity
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.product.name} - {self.batch_number or 'No Batch'} ({self.quantity}) @ {self.location.name}"

    @property
    def is_expired(self):
        if self.expiry_date:
            return self.expiry_date < timezone.now().date()
        return False

    @property
    def days_to_expiry(self):
        if self.expiry_date:
            delta = self.expiry_date - timezone.now().date()
            return delta.days
        return None

    @property
    def stock_value(self):
        """What the pieces still on the shelf are worth at their own cost."""
        return q2(D(self.cost_price) * self.quantity)

    @property
    def quantity_sold(self):
        return max(self.initial_quantity - self.quantity, 0)


class StockTransfer(BaseRetailModel):
    """
    Manages movement between Warehouses and Shops.
    """

    class Status(models.TextChoices):
        DRAFT = 'DRAFT', _('Draft')
        PENDING_APPROVAL = 'PENDING', _('Pending Approval')
        APPROVED = 'APPROVED', _('Approved')  # Ready to be picked
        IN_TRANSIT = 'TRANSIT', _('In Transit')
        RECEIVED = 'RECEIVED', _('Received')
        CANCELLED = 'CANCELLED', _('Cancelled')
        REJECTED = 'REJECTED', _('Rejected')

    reference_number = models.CharField(max_length=50, unique=True, editable=False)

    source_location = models.ForeignKey(
        'location.Location',
        on_delete=models.PROTECT,
        related_name='outgoing_transfers'
    )
    destination_location = models.ForeignKey(
        'location.Location',
        on_delete=models.PROTECT,
        related_name='incoming_transfers'
    )

    status = models.CharField(max_length=20, choices=Status.choices, default=Status.DRAFT)

    # Logistics
    driver_name = models.CharField(max_length=255, blank=True)
    vehicle_number = models.CharField(max_length=50, blank=True)
    estimated_arrival = models.DateTimeField(null=True, blank=True)

    # Audit
    requested_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                     related_name='transfer_requests')
    approved_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                    related_name='transfer_approvals')
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                    related_name='transfer_receipts')

    notes = models.TextField(blank=True)

    def save(self, *args, **kwargs):
        if not self.reference_number:
            # Simple ID generation logic; in prod use a more robust sequence
            import uuid
            self.reference_number = f"TRF-{uuid.uuid4().hex[:8].upper()}"
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.reference_number}: {self.source_location} -> {self.destination_location}"


class StockTransferItem(models.Model):
    transfer = models.ForeignKey(StockTransfer, on_delete=models.CASCADE, related_name='items')
    product = models.ForeignKey('products.Product', on_delete=models.CASCADE)

    # We request a quantity, but fulfill from specific batches
    quantity_requested = models.PositiveIntegerField()
    quantity_sent = models.PositiveIntegerField(default=0)
    quantity_received = models.PositiveIntegerField(default=0)

    # If items are damaged in transit
    quantity_damaged = models.PositiveIntegerField(default=0)
    damage_notes = models.CharField(max_length=255, blank=True)

    def __str__(self):
        return f"{self.product.name} ({self.quantity_requested}) in {self.transfer.reference_number}"


class StockAdjustment(TimeStampedModel):
    """
    For manual corrections (Damages, Theft, Found Stock, Expired Disposal).
    """

    class Reason(models.TextChoices):
        DAMAGE = 'DAMAGE', _('Damaged')
        THEFT = 'THEFT', _('Theft')
        EXPIRED = 'EXPIRED', _('Expired')
        COUNT_CORRECTION = 'CORRECTION', _('Inventory Count Correction')
        RETURN = 'RETURN', _('Customer Return (Restock)')
        OTHER = 'OTHER', _('Other')

    location = models.ForeignKey('location.Location', on_delete=models.PROTECT)
    batch = models.ForeignKey(StockBatch, on_delete=models.PROTECT)
    adjusted_quantity = models.IntegerField(help_text="Negative to remove, Positive to add")
    reason = models.CharField(max_length=20, choices=Reason.choices)
    notes = models.TextField()
    performed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True)

    def __str__(self):
        return f"{self.reason} - {self.batch.product.name} ({self.adjusted_quantity})"


class Shipment(models.Model):
    """
    Represents a full container or consolidated shipment from a supplier.
    e.g., "China Dec 2025 Container"
    """
    STATUS_CHOICES = (
        ('PENDING', 'Pending (In Transit / Clearing)'),
        ('RECEIVED', 'Received into Stock'),
    )

    reference_number = models.CharField(max_length=100, unique=True, help_text="e.g., LSA20251018")
    supplier_name = models.CharField(max_length=255)
    date_shipped = models.DateField(null=True, blank=True)

    # --- The Core Financial Variables ---
    exchange_rate = models.DecimalField(max_digits=10, decimal_places=4,
                                        help_text="Current exchange rate: 1 USD = X GHS")
    total_freight_usd = models.DecimalField(max_digits=12, decimal_places=2,
                                            help_text="Total cost of shipping & clearing in USD")
    total_cbm = models.DecimalField(max_digits=10, decimal_places=4, help_text="Total volume of the container in CBM")

    status = models.CharField(max_length=20, choices=STATUS_CHOICES, default='PENDING')
    created_at = models.DateTimeField(auto_now_add=True)

    # Receipt audit: who pushed this container into stock, where, and when.
    received_at = models.DateTimeField(null=True, blank=True)
    received_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL,
                                    null=True, blank=True, related_name='received_shipments')
    received_location = models.ForeignKey('location.Location', on_delete=models.SET_NULL,
                                          null=True, blank=True, related_name='received_shipments')
    notes = models.TextField(blank=True)

    @property
    def freight_per_cbm_usd(self):
        """Calculates how much it costs to ship 1 CBM in USD"""
        if self.total_cbm and self.total_cbm > 0:
            return self.total_freight_usd / self.total_cbm
        return Decimal('0.00')

    # --- Container-level totals (admin / landed-cost reporting) ---
    @property
    def total_pieces(self):
        return sum(item.quantity for item in self.items.all())

    @property
    def total_material_cost_ghs(self):
        return q2(sum((item.total_material_cost_ghs for item in self.items.all()), ZERO))

    @property
    def total_freight_cost_ghs(self):
        return q2(D(self.total_freight_usd) * D(self.exchange_rate))

    @property
    def total_landed_cost_ghs(self):
        """What the whole container cost you in cedis, freight included."""
        return q2(self.total_material_cost_ghs + self.total_freight_cost_ghs)

    @property
    def projected_revenue_ghs(self):
        """What the container is worth on the shelf, at current selling prices."""
        return q2(sum((item.projected_line_revenue_ghs for item in self.items.all()), ZERO))

    @property
    def projected_profit_ghs(self):
        return q2(self.projected_revenue_ghs - self.total_material_cost_ghs)

    @transaction.atomic
    def receive_into_stock(self, location, received_by):
        """
        Push a container onto the shelf.

        Reconciles the three numbers that used to contradict each other:
          * the BATCH keeps the exact cost of the pieces it brought in,
          * PRODUCT.cost_price becomes the weighted average of all batches on
            hand, so margin reports never disagree with the batches,
          * PRODUCT.selling_price only moves if nobody set it by hand -- a
            manual price is preserved and the invoice price is parked as a
            suggestion instead.
        """
        if self.status == 'RECEIVED':
            raise ValueError(
                f"Shipment {self.reference_number} has already been received into stock "
                f"({self.received_at:%d %b %Y} at {self.received_location}). "
                f"Receiving it twice would duplicate the inventory."
            )
        if location is None:
            raise ValueError("A destination location is required to receive a shipment.")

        supplier_obj, _created = Supplier.objects.get_or_create(name=self.supplier_name)
        summary = {'batches': 0, 'prices_applied': 0, 'prices_suggested': 0}

        for item in self.items.select_related('product'):
            product = item.product

            # Quantity from the invoice is physical PIECES (a pair = 2 pieces).
            # The batch is valued at the true cost of ONE piece.
            StockBatch.objects.create(
                product=product,
                location=location,
                quantity=item.quantity,
                initial_quantity=item.quantity,
                cost_price=item.unit_landed_cost_ghs,
                supplier=supplier_obj,
                batch_number=self.reference_number,
                notes=f"Shipment {self.reference_number} @ {self.exchange_rate} GHS/USD",
            )
            summary['batches'] += 1

            # Selling price: governed, logged, never silently clobbered.
            if item.update_selling_price and item.outside_sale_price_ghs > 0:
                outcome = product.apply_price(
                    item.outside_sale_price_ghs,
                    source=PriceChangeLog.Source.SHIPMENT,
                    changed_by=received_by,
                    note=f"Shipment {self.reference_number}",
                )
                if outcome == 'applied':
                    summary['prices_applied'] += 1
                elif outcome == 'suggested':
                    summary['prices_suggested'] += 1

            # Cost price: weighted average across every batch now on hand.
            product.resync_cost_price(
                changed_by=received_by,
                note=f"After receiving shipment {self.reference_number}",
            )

        self.status = 'RECEIVED'
        self.received_at = timezone.now()
        self.received_by = received_by
        self.received_location = location
        self.save(update_fields=['status', 'received_at', 'received_by', 'received_location'])
        return summary

    def __str__(self):
        return f"{self.reference_number} - {self.supplier_name}"


class ShipmentItem(models.Model):
    shipment = models.ForeignKey(Shipment, related_name='items', on_delete=models.CASCADE)
    product = models.ForeignKey('products.Product', on_delete=models.PROTECT)

    # Input from Excel
    quantity = models.IntegerField(help_text="Number of PIECES (e.g. 10)")
    unit_cost_usd = models.DecimalField(max_digits=10, decimal_places=2, help_text="Supplier price per PIECE in USD")
    total_line_cbm = models.DecimalField(max_digits=10, decimal_places=4, help_text="Total CBM for this line item")
    outside_sale_price_ghs = models.DecimalField(max_digits=12, decimal_places=2,
                                                 help_text="Your selling price (Per PAIR if applicable, else Piece)")
    update_selling_price = models.BooleanField(
        default=True,
        help_text="Untick to receive this line as stock only, leaving the shop price untouched."
    )

    # --- TRUE COST CALCULATIONS ---
    @property
    def total_material_cost_ghs(self):
        return ((self.unit_cost_usd * Decimal(str(self.quantity))) * self.shipment.exchange_rate).quantize(
            Decimal('0.01'))

    @property
    def total_freight_cost_ghs(self):
        return (self.total_line_cbm * self.shipment.freight_per_cbm_usd * self.shipment.exchange_rate).quantize(
            Decimal('0.01'))

    @property
    def total_landed_cost_ghs(self):
        # Per user request: Simplified cost uses ONLY the material cost. Freight is ignored here.
        return self.total_material_cost_ghs

    @property
    def unit_landed_cost_ghs(self):
        """The pure cost of exactly ONE piece (Converted USD Material Cost Only)"""
        return (self.unit_cost_usd * self.shipment.exchange_rate).quantize(Decimal('0.01'))

    # --- PROFIT CALCULATIONS (The Pair/Piece Fix) ---
    @property
    def effective_cost_for_margin(self):
        """
        If this is sold in pairs, the Outside Sale price is for a pair.
        So we must compare it to the cost of TWO pieces to get real profit.
        """
        if self.product.is_sold_in_pairs:
            return (self.unit_landed_cost_ghs * Decimal('2.00')).quantize(Decimal('0.01'))
        return self.unit_landed_cost_ghs

    @property
    def projected_unit_profit_ghs(self):
        """Profit made when selling 1 Pair (or 1 Piece if not paired)"""
        return (self.outside_sale_price_ghs - self.effective_cost_for_margin).quantize(Decimal('0.01'))

    @property
    def profit_margin_percentage(self):
        if self.outside_sale_price_ghs > 0:
            return ((self.projected_unit_profit_ghs / self.outside_sale_price_ghs) * Decimal('100.00')).quantize(
                Decimal('0.01'))
        return Decimal('0.00')

    @property
    def billable_units(self):
        """
        How many things you can actually sell off this line.

        10 pieces of a paired part is 5 pairs; 10 pieces of a normal part is
        10 units. An odd piece count on a paired part still leaves one single
        to sell, so round up.
        """
        if self.product.is_sold_in_pairs:
            return (self.quantity + 1) // 2
        return self.quantity

    @property
    def projected_line_revenue_ghs(self):
        """Revenue if this whole line sells at the current selling price."""
        return q2(D(self.outside_sale_price_ghs) * self.billable_units)

    @property
    def projected_line_profit_ghs(self):
        return q2(self.projected_line_revenue_ghs - self.total_material_cost_ghs)

    @property
    def price_conflicts_with_product(self):
        """
        True when this line's invoice price disagrees with what the shop is
        currently charging -- i.e. a suggestion a human needs to rule on.
        """
        return q2(self.outside_sale_price_ghs) != q2(self.product.selling_price)

    def __str__(self):
        return f"{self.product.sku} x{self.quantity} on {self.shipment.reference_number}"






























