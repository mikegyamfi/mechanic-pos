from decimal import Decimal

from django.db import models
from django.conf import settings
from apps.core.models import BaseRetailModel, TimeStampedModel
from apps.core.money import D, ZERO, q2


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

    def unpaid_invoices(self):
        """
        Invoices with money still outstanding.

        NOTE: the reverse accessor for Sale.customer is `purchases`, not
        `sales` -- using the wrong one raised AttributeError inside the credit
        check and blocked every credit sale.
        """
        from django.db.models import F
        from apps.sales.models import Sale

        return self.purchases.filter(
            status__in=Sale.REVENUE_STATUSES,
        ).annotate(
            outstanding=F('total_amount') - F('refunded_amount') - F('amount_paid')
        ).filter(outstanding__gt=0)

    @property
    def current_debt(self):
        """Exactly what they owe right now, net of anything they returned."""
        from django.db.models import Sum

        owed = self.unpaid_invoices().aggregate(total=Sum('outstanding'))['total']
        return q2(owed or 0)

    @property
    def available_credit(self):
        """How much more can they take on credit before hitting their limit?"""
        return q2(D(self.credit_limit) - self.current_debt)

    @property
    def is_over_limit(self):
        return self.current_debt > D(self.credit_limit)

    def recalculate_lifetime_stats(self, save=True):
        """
        Rebuild total_spent / total_visits from the invoices themselves.

        Incrementing these with `+=` loses updates when two tills serve the
        same mechanic at once, and never reverses on a refund.
        """
        from django.db.models import Count, Sum
        from apps.sales.models import Sale

        agg = self.purchases.filter(status__in=Sale.REVENUE_STATUSES).aggregate(
            spent=Sum('total_amount'),
            refunded=Sum('refunded_amount'),
            visits=Count('id'),
            last=models.Max('created_at'),
        )
        self.total_spent = q2(D(agg['spent'] or 0) - D(agg['refunded'] or 0))
        self.total_visits = agg['visits'] or 0
        self.last_visit_date = agg['last']
        if save:
            self.save(update_fields=['total_spent', 'total_visits', 'last_visit_date'])
        return self

    class Meta:
        ordering = ['-last_visit_date']

    def __str__(self):
        return self.display_name

    @property
    def display_name(self):
        """Best human label we have: workshop, then name, then phone."""
        if self.workshop_name:
            return self.workshop_name
        full_name = f"{self.first_name} {self.last_name}".strip()
        if full_name:
            return full_name
        return self.phone_number or "Walk-in Customer"

    # Kept for existing templates/views that reference `get_display_name`.
    @property
    def get_display_name(self):
        return self.display_name


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






