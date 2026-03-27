from decimal import Decimal

from django.db import models
from django.conf import settings
from apps.core.models import BaseRetailModel, TimeStampedModel


class Customer(BaseRetailModel):
    """
    The 'Backlog' of all people who have shopped with us.
    Designed for 'Silent Accumulation' - we can have a profile
    with just a phone number and nothing else.
    """
    # Essential for SMS Receipts
    phone_number = models.CharField(max_length=20, unique=True, db_index=True)

    # Optional Details (captured only if they want to give it)
    first_name = models.CharField(max_length=100, blank=True)
    last_name = models.CharField(max_length=100, blank=True)
    email = models.EmailField(unique=True, null=True, blank=True)
    address = models.TextField(blank=True)

    # Marketing Flags (GDPR/Data Protection compliance)
    accepts_marketing_sms = models.BooleanField(default=True)
    accepts_marketing_email = models.BooleanField(default=False)

    # Auto-Calculated Segmentation (Updated via Signals on every sale)
    total_spent = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)
    total_visits = models.PositiveIntegerField(default=0)
    last_visit_date = models.DateTimeField(null=True, blank=True)

    # Store Credit / Wallet
    wallet_balance = models.DecimalField(max_digits=12, decimal_places=2, default=0.00)

    CUSTOMER_TYPE_CHOICES = (
        ('WALK_IN', 'Regular Walk-in'),
        ('MECHANIC', 'Mechanic / Fitter'),
        ('RETAILER', 'Retail Shop / Reseller'),
    )
    customer_type = models.CharField(max_length=20, choices=CUSTOMER_TYPE_CHOICES, default='WALK_IN')

    workshop_name = models.CharField(max_length=255, blank=True, null=True, help_text="e.g. Master Kojo Motors")

    # --- DEBT & CREDIT GUARDRAILS ---
    credit_limit = models.DecimalField(
        max_digits=12, decimal_places=2, default=0.00,
        help_text="Maximum amount this person is allowed to owe. 0 means no credit allowed."
    )

    @property
    def current_debt(self):
        """Calculates exactly how much they owe right now based on their unpaid invoices"""
        from apps.sales.models import Sale
        from django.db.models import Sum, F

        # Find all sales where amount_paid is less than total_amount
        debts = self.sales.filter(amount_paid__lt=F('total_amount')).aggregate(
            total_owed=Sum(F('total_amount') - F('amount_paid'))
        )
        return debts['total_owed'] or Decimal('0.00')

    @property
    def available_credit(self):
        """How much more can they take on credit before hitting their limit?"""
        return self.credit_limit - self.current_debt

    class Meta:
        ordering = ['-last_visit_date']

    def __str__(self):
        return f"{self.first_name} {self.last_name} ({self.phone_number})".strip()

    @property
    def get_display_name(self):
        if self.first_name:
            return f"{self.first_name} {self.last_name}"
        return "Valued Customer"


class CustomerGroup(TimeStampedModel):
    """
    For Bulk SMS Segmentation.
    e.g., "High Spenders", "Haven't visited in 30 days", "Wholesalers"
    """
    name = models.CharField(max_length=100)
    description = models.TextField(blank=True)
    customers = models.ManyToManyField(Customer, related_name='groups', blank=True)

    def __str__(self):
        return self.name






