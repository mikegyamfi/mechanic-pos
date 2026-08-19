from decimal import Decimal

from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from apps.core.models import TimeStampedModel


class Location(TimeStampedModel):
    """
    Represents a physical Warehouse or Retail Shop.
    """
    class LocationType(models.TextChoices):
        WAREHOUSE = 'WAREHOUSE', 'Warehouse'
        SHOP = 'SHOP', 'Shop'

    name = models.CharField(max_length=255, unique=True)
    location_type = models.CharField(
        max_length=20,
        choices=LocationType.choices,
        default=LocationType.SHOP
    )
    address = models.TextField()
    phone_number = models.CharField(max_length=15, blank=True)
    manager = models.ForeignKey(
        'users.User',
        on_delete=models.SET_NULL,
        null=True,
        blank=True,
        related_name='managed_locations'
    )
    is_active = models.BooleanField(default=True)

    # The haggling floor for this shop. A cashier cannot price a part below
    # landed cost + this margin without a manager override.
    min_margin_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=Decimal('10.00'),
        validators=[MinValueValidator(Decimal('0.00')), MaxValueValidator(Decimal('500.00'))],
        help_text="Minimum gross margin over landed cost, in %. 10 means nothing may be sold "
                  "below cost + 10% unless a manager authorises it."
    )

    created_at = models.DateTimeField(auto_now_add=True)
    updated_at = models.DateTimeField(auto_now=True)

    class Meta:
        ordering = ['name']

    def __str__(self):
        return f"{self.name} ({self.get_location_type_display()})"