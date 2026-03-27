from decimal import Decimal

from django.db import models, transaction
from django.conf import settings
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from apps.core.models import BaseRetailModel, TimeStampedModel
from apps.products.models import Product


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

    batch_number = models.CharField(max_length=100, blank=True, help_text="Manufacturer Batch/Lot Number")
    quantity = models.IntegerField(default=0)

    # Financials per batch
    cost_price = models.DecimalField(max_digits=12, decimal_places=2, help_text="Cost per unit for this specific batch")

    # Dates
    manufactured_date = models.DateField(null=True, blank=True)
    expiry_date = models.DateField(null=True, blank=True, db_index=True)
    received_date = models.DateTimeField(default=timezone.now)

    class Meta:
        # Unique constraint to prevent duplicate batches if needed,
        # but often we just want to aggregate.
        # Indexing for FEFO is crucial.
        ordering = ['expiry_date', 'received_date']
        indexes = [
            models.Index(fields=['product', 'location', 'expiry_date']),
        ]

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

    @property
    def freight_per_cbm_usd(self):
        """Calculates how much it costs to ship 1 CBM in USD"""
        if self.total_cbm and self.total_cbm > 0:
            return self.total_freight_usd / self.total_cbm
        return Decimal('0.00')

    def receive_into_stock(self, location, received_by):
        supplier_obj, created = Supplier.objects.get_or_create(name=self.supplier_name)

        with transaction.atomic():
            for item in self.items.all():
                product = item.product

                # The quantity typed from Excel is physical PIECES (e.g. 10)
                physical_qty_to_add = item.quantity

                # The true cost of exactly ONE piece in GHS (Now strictly Material Cost)
                cost_per_piece = item.unit_landed_cost_ghs

                # Set Global Prices
                product.selling_price = item.outside_sale_price_ghs  # Always your typed Outside Sale

                # Simplified Single Pricing: Exactly half of the pair price
                if product.is_sold_in_pairs and not product.single_piece_price:
                    product.single_piece_price = (item.outside_sale_price_ghs / Decimal('2.00')).quantize(
                        Decimal('0.01'))

                product.cost_price = cost_per_piece
                product.save()

                StockBatch.objects.create(
                    product=product,
                    location=location,
                    quantity=physical_qty_to_add,  # Stores as 10 pieces on the shelf
                    cost_price=cost_per_piece,  # Values it at cost per single piece
                    supplier=supplier_obj,
                    batch_number=self.reference_number
                )

            self.status = 'RECEIVED'
            self.save()

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






























