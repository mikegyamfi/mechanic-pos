from decimal import Decimal

from django.conf import settings
from django.core.validators import MinValueValidator, MaxValueValidator
from django.db import models
from django.utils.text import slugify
from django.utils.translation import gettext_lazy as _

from apps.core.models import BaseRetailModel, TimeStampedModel
from apps.core.money import D, ZERO, pct, q2


class Category(BaseRetailModel):
    """
    Hierarchical category system (e.g., Electronics -> Laptops -> Gaming).
    """
    name = models.CharField(max_length=255)
    slug = models.SlugField(unique=True)
    parent = models.ForeignKey(
        'self', on_delete=models.SET_NULL, null=True, blank=True, related_name='subcategories'
    )
    description = models.TextField(blank=True)
    icon = models.ImageField(upload_to='categories/icons/', null=True, blank=True)

    class Meta:
        verbose_name_plural = "Categories"

    def save(self, *args, **kwargs):
        if not self.slug:
            base_slug = slugify(self.name)
            slug = base_slug
            counter = 1
            while Category.objects.filter(slug=slug).exists():
                slug = f"{base_slug}-{counter}"
                counter += 1
            self.slug = slug
        super().save(*args, **kwargs)

    def __str__(self):
        full_path = [self.name]
        k = self.parent
        while k is not None:
            full_path.append(k.name)
            k = k.parent
        return ' -> '.join(full_path[::-1])


class Brand(BaseRetailModel):
    name = models.CharField(max_length=255, unique=True)
    slug = models.SlugField(unique=True)
    logo = models.ImageField(upload_to='brands/logos/', null=True, blank=True)
    website = models.URLField(blank=True)

    def save(self, *args, **kwargs):
        if not self.slug:
            self.slug = slugify(self.name)
        super().save(*args, **kwargs)

    def __str__(self):
        return self.name


class Unit(models.Model):
    """
    Units of measurement (e.g., pcs, kg, box, liter).
    """
    name = models.CharField(max_length=50)  # e.g., Kilogram
    symbol = models.CharField(max_length=10)  # e.g., kg

    def __str__(self):
        return f"{self.name} ({self.symbol})"


class Product(BaseRetailModel):
    """
    The central product definition.
    Stock is NOT stored here; it is stored in the Inventory app.
    """
    name = models.CharField(max_length=255, db_index=True)
    slug = models.SlugField(unique=True, max_length=255)
    description = models.TextField(blank=True)

    # Classification
    category = models.ForeignKey(Category, on_delete=models.SET_NULL, null=True, related_name='products')
    brand = models.ForeignKey(Brand, on_delete=models.SET_NULL, null=True, blank=True, related_name='products')
    unit = models.ForeignKey(Unit, on_delete=models.SET_NULL, null=True, blank=True)

    # Identifiers (Scanning & Internal)
    sku = models.CharField(max_length=100, unique=True, help_text="Internal Stock Keeping Unit")
    barcode = models.CharField(
        max_length=100,
        unique=True,
        null=True,
        blank=True,
        db_index=True,
        help_text="Scannable Barcode (UPC, EAN, etc.)"
    )

    # Pricing
    cost_price = models.DecimalField(max_digits=12, decimal_places=2, help_text="Average or last cost price")
    selling_price = models.DecimalField(max_digits=12, decimal_places=2, db_index=True)
    wholesale_price = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True,
                                          help_text="Discounted price for bulk buyers")

    # Tax & Settings
    tax_rate = models.DecimalField(max_digits=5, decimal_places=2, default=0.00,
                                   help_text="Percentage tax applicable (e.g., 12.5)")
    is_returnable = models.BooleanField(default=True)
    is_perishable = models.BooleanField(default=False, help_text="Does this product have an expiry date?")

    # Inventory Alerts
    low_stock_threshold = models.PositiveIntegerField(default=10, help_text="Global alert level")

    # Physical Attributes (Comprehensive/Nullable)
    weight_kg = models.DecimalField(max_digits=10, decimal_places=3, null=True, blank=True)
    dimensions = models.CharField(max_length=100, blank=True, help_text="L x W x H")
    manufacturer_part_number = models.CharField(max_length=100, blank=True)
    shelf_location = models.CharField(max_length=100, blank=True, help_text="General shelf location hint")

    # ---------------------------------------------------------
    # PAIR / SPLIT PRICING
    # ---------------------------------------------------------
    # Stock is ALWAYS counted in pieces. For a paired item, `selling_price`
    # and `wholesale_price` are PAIR prices; breaking the pair and selling
    # one side is priced by the split rule below.
    class SplitPriceMode(models.TextChoices):
        PERCENT = 'PERCENT', _('Percentage of pair price')
        FIXED = 'FIXED', _('Fixed amount')

    DEFAULT_SPLIT_PERCENTAGE = Decimal('60.00')

    is_sold_in_pairs = models.BooleanField(
        default=False,
        help_text="Check this if the item is imported as a pair (like Headlights or Shocks)."
    )
    split_price_mode = models.CharField(
        max_length=10, choices=SplitPriceMode.choices, default=SplitPriceMode.PERCENT,
        help_text="PERCENT keeps the single-piece price locked to the pair price forever. "
                  "FIXED pins an exact cedi amount that never moves."
    )
    split_price_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, default=DEFAULT_SPLIT_PERCENTAGE,
        validators=[MinValueValidator(Decimal('1.00')), MaxValueValidator(Decimal('100.00'))],
        help_text="What one side costs when the pair is broken, as a % of the pair price. "
                  "60 means one headlight sells for 60% of the pair price."
    )
    single_piece_price = models.DecimalField(
        max_digits=10, decimal_places=2, null=True, blank=True,
        help_text="FIXED mode only: the exact price for one side of a broken pair. "
                  "Ignored while the split rule is set to PERCENT."
    )

    # ---------------------------------------------------------
    # PRICE GOVERNANCE
    # ---------------------------------------------------------
    price_is_manual = models.BooleanField(
        default=False,
        help_text="Set automatically when a human edits the price. Once true, receiving a "
                  "shipment will not silently overwrite the price -- it is offered as a suggestion."
    )
    suggested_selling_price = models.DecimalField(
        max_digits=12, decimal_places=2, null=True, blank=True,
        help_text="A price a shipment wanted to apply but could not, because the price is manual."
    )
    suggested_price_source = models.CharField(max_length=120, blank=True)
    suggested_price_at = models.DateTimeField(null=True, blank=True)

    min_margin_percentage = models.DecimalField(
        max_digits=5, decimal_places=2, null=True, blank=True,
        validators=[MinValueValidator(ZERO), MaxValueValidator(Decimal('500.00'))],
        help_text="Overrides the shop's minimum margin for this part only. Blank = use the shop setting."
    )

    def save(self, *args, **kwargs):
        if not self.slug:
            # Ensure uniqueness by appending the already unique SKU
            self.slug = slugify(f"{self.name}-{self.sku}")
        super().save(*args, **kwargs)

    def __str__(self):
        return f"{self.name} ({self.sku})"

    # ---------------------------------------------------------
    # PRICING API
    # Everything that needs a price -- POS, receipts, reports, the sale
    # validator -- goes through these. Never re-derive pair maths inline.
    # ---------------------------------------------------------
    class SellMode(models.TextChoices):
        PIECE = 'PIECE', _('Piece')
        PAIR = 'PAIR', _('Pair')
        SINGLE = 'SINGLE', _('Single (broken pair)')

    def pieces_per_unit(self, sell_mode):
        """How many physical pieces leave the shelf for one billed unit."""
        if sell_mode == self.SellMode.PAIR and self.is_sold_in_pairs:
            return 2
        return 1

    def split_price_from(self, base_price):
        """
        The single-piece price implied by a given pair price.

        This is the whole point of PERCENT mode: pass in retail and you get the
        retail split price, pass in wholesale and you get the wholesale split
        price. Change the pair price anywhere and the split follows.
        """
        base_price = D(base_price)
        if self.split_price_mode == self.SplitPriceMode.FIXED:
            if self.single_piece_price is not None:
                return q2(self.single_piece_price)
            # FIXED with nothing pinned yet: fall back to the percentage rule
            # rather than handing the cashier a zero.
            return pct(base_price, self.split_price_percentage)
        return pct(base_price, self.split_price_percentage)

    @property
    def effective_single_price(self):
        """Retail price for one side of a broken pair."""
        return self.split_price_from(self.selling_price)

    @property
    def effective_single_wholesale_price(self):
        """Wholesale price for one side of a broken pair."""
        return self.split_price_from(self.wholesale_price or self.selling_price)

    def base_price(self, sell_mode, wholesale=False, promotion=None):
        """
        The list price for one billed unit in the given mode, before haggling.

        A promotion discounts the PAIR price, and the split rule then derives
        the single-piece price from the discounted figure -- so a 15% promotion
        flows through to broken pairs on its own.
        """
        retail = q2(self.selling_price)
        trade = q2(self.wholesale_price) if self.wholesale_price else retail
        headline = trade if wholesale else retail

        if promotion is not None:
            headline = promotion.price_for(headline)

        if sell_mode == self.SellMode.SINGLE and self.is_sold_in_pairs:
            return self.split_price_from(headline)
        return headline

    def unit_cost(self, sell_mode):
        """Landed cost of one billed unit -- doubled for a pair, so margins compare like for like."""
        return q2(D(self.cost_price) * self.pieces_per_unit(sell_mode))

    def effective_min_margin(self, location=None):
        """Per-product override, else the shop's floor, else zero."""
        if self.min_margin_percentage is not None:
            return D(self.min_margin_percentage)
        if location is not None and location.min_margin_percentage is not None:
            return D(location.min_margin_percentage)
        return ZERO

    def price_floor(self, sell_mode, location=None, promotion=None, wholesale=False):
        """
        The absolute lowest price this part may leave the shop for.

        Normally landed cost plus the shop's minimum margin -- and there is no
        override at the counter, for anybody. The ONLY thing that lowers this
        floor is a running Promotion, in which case the floor becomes exactly
        the promotional price: the cashier may charge the promo price, and still
        not a pesewa less.
        """
        cost = self.unit_cost(sell_mode)
        margin = self.effective_min_margin(location)
        cost_floor = q2(cost * (Decimal('1') + margin / Decimal('100')))

        if promotion is not None:
            # A promotion only ever LOWERS the floor. If the promotional price
            # is still above cost + margin, the cashier keeps their normal
            # negotiating room down to the cost floor; if the promotion slashes
            # below cost (authorised loss-leader), the promo price becomes the
            # new bottom.
            promo_price = self.base_price(sell_mode, wholesale=wholesale, promotion=promotion)
            return min(cost_floor, promo_price)
        return cost_floor

    def margin_percentage(self, price, sell_mode):
        """Gross margin on a given price, as a % of the price (not of cost)."""
        price = D(price)
        if price <= 0:
            return ZERO
        return q2((price - self.unit_cost(sell_mode)) / price * Decimal('100'))

    # ---------------------------------------------------------
    # STOCK (lives in inventory.StockBatch -- these are read helpers)
    # ---------------------------------------------------------
    def stock_on_hand(self, location=None):
        """Pieces on hand, optionally at one location."""
        qs = self.batches.all()
        if location is not None:
            qs = qs.filter(location=location)
        return qs.aggregate(total=models.Sum('quantity'))['total'] or 0

    def weighted_average_cost(self, location=None):
        """
        Cost basis: the weighted average cost of the pieces still on the shelf.

        Batches keep their own true cost for shipment accounting; this is the
        single number that keeps Product.cost_price from contradicting them.
        Returns None when there is no stock (caller should keep the old cost).
        """
        qs = self.batches.filter(quantity__gt=0)
        if location is not None:
            qs = qs.filter(location=location)

        total_qty = 0
        total_value = ZERO
        for qty, cost in qs.values_list('quantity', 'cost_price'):
            total_qty += qty
            total_value += D(cost) * qty

        if total_qty <= 0:
            return None
        return q2(total_value / total_qty)

    def resync_cost_price(self, save=True, changed_by=None, note=''):
        """
        Pull Product.cost_price back in line with the batches on hand.

        Called after any event that changes what stock exists at what cost:
        receiving a shipment, receiving a transfer, an adjustment, a refund.
        """
        average = self.weighted_average_cost()
        if average is None or average == q2(self.cost_price):
            return False

        old = q2(self.cost_price)
        self.cost_price = average
        if save:
            self.save(update_fields=['cost_price', 'updated_at'])
            PriceChangeLog.objects.create(
                product=self, field='cost_price', old_value=old, new_value=average,
                source=PriceChangeLog.Source.COST_SYNC, changed_by=changed_by,
                note=note or 'Weighted average of batches on hand',
            )
        return True

    def apply_price(self, new_price, source, changed_by=None, field='selling_price',
                    note='', force=False):
        """
        The ONLY sanctioned way to move a price. Logs every change.

        Returns 'applied', 'suggested' or 'unchanged'. A non-manual source
        (a shipment) will not overwrite a manually-set price -- it parks the
        number in `suggested_selling_price` for a human to accept.
        """
        from django.utils import timezone

        new_price = q2(new_price)
        old_price = q2(getattr(self, field) or 0)

        if new_price == old_price:
            return 'unchanged'

        manual_sources = (PriceChangeLog.Source.MANUAL, PriceChangeLog.Source.SUGGESTION_ACCEPTED)
        is_manual_source = source in manual_sources

        if (field == 'selling_price' and self.price_is_manual
                and not is_manual_source and not force):
            self.suggested_selling_price = new_price
            self.suggested_price_source = note or str(source)
            self.suggested_price_at = timezone.now()
            self.save(update_fields=['suggested_selling_price', 'suggested_price_source',
                                     'suggested_price_at', 'updated_at'])
            PriceChangeLog.objects.create(
                product=self, field=field, old_value=old_price, new_value=new_price,
                source=source, changed_by=changed_by, was_applied=False,
                note=note or 'Held back: price is manually managed',
            )
            return 'suggested'

        setattr(self, field, new_price)
        update_fields = [field, 'updated_at']

        if is_manual_source and field == 'selling_price':
            self.price_is_manual = True
            update_fields.append('price_is_manual')

        if field == 'selling_price':
            # An applied price supersedes any pending suggestion.
            self.suggested_selling_price = None
            self.suggested_price_source = ''
            self.suggested_price_at = None
            update_fields += ['suggested_selling_price', 'suggested_price_source', 'suggested_price_at']

        self.save(update_fields=list(dict.fromkeys(update_fields)))
        PriceChangeLog.objects.create(
            product=self, field=field, old_value=old_price, new_value=new_price,
            source=source, changed_by=changed_by, note=note,
        )
        return 'applied'


class PromotionQuerySet(models.QuerySet):

    def running(self, at=None):
        """Promotions that are switched on and inside their date window."""
        from django.utils import timezone
        at = at or timezone.now()
        return self.filter(
            is_active=True, starts_at__lte=at
        ).filter(models.Q(ends_at__isnull=True) | models.Q(ends_at__gte=at))

    def for_location(self, location):
        """A promotion with no locations set runs everywhere."""
        if location is None:
            return self.filter(locations__isnull=True)
        return self.filter(
            models.Q(locations__isnull=True) | models.Q(locations=location)
        ).distinct()


class Promotion(BaseRetailModel):
    """
    A deliberate, dated, authorised price cut.

    This is the ONLY way a part may be sold below cost + the shop's minimum
    margin. A cashier can never negotiate under the floor at the counter --
    a manager has to declare a promotion, which is dated, named and logged.
    """

    class DiscountType(models.TextChoices):
        PERCENT = 'PERCENT', _('Percentage off the normal price')
        AMOUNT_OFF = 'AMOUNT', _('Fixed amount off (GHS)')
        FIXED_PRICE = 'FIXED', _('Sell at this exact price (GHS)')

    name = models.CharField(max_length=150, help_text="e.g. 'Easter Clearance' or 'Old Corolla stock'")
    description = models.TextField(blank=True, help_text="Why this promotion exists. Shown to managers.")

    discount_type = models.CharField(max_length=10, choices=DiscountType.choices,
                                     default=DiscountType.PERCENT)
    value = models.DecimalField(
        max_digits=12, decimal_places=2, validators=[MinValueValidator(Decimal('0.01'))],
        help_text="Percent for PERCENT (e.g. 15 = 15% off), cedis for AMOUNT/FIXED."
    )

    # Scope: everything, or specific parts, or whole categories.
    applies_to_all = models.BooleanField(
        default=False, help_text="Apply to the entire catalogue. Use with care."
    )
    products = models.ManyToManyField(Product, blank=True, related_name='promotions')
    categories = models.ManyToManyField(Category, blank=True, related_name='promotions',
                                        help_text="Includes every sub-category underneath.")
    locations = models.ManyToManyField('location.Location', blank=True, related_name='promotions',
                                       help_text="Leave empty to run in every shop.")

    starts_at = models.DateTimeField()
    ends_at = models.DateTimeField(null=True, blank=True,
                                   help_text="Leave blank to run until switched off.")

    allow_below_cost = models.BooleanField(
        default=False,
        help_text="Tick ONLY for a deliberate loss-leader. Without this, the promotion "
                  "suspends itself on any part it would sell below landed cost."
    )

    created_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True,
                                   blank=True, related_name='created_promotions')

    objects = PromotionQuerySet.as_manager()

    class Meta:
        ordering = ['-starts_at']
        indexes = [models.Index(fields=['is_active', 'starts_at', 'ends_at'])]

    def __str__(self):
        return f"{self.name} ({self.discount_label})"

    @property
    def discount_label(self):
        if self.discount_type == self.DiscountType.PERCENT:
            # Trim trailing zeros without Decimal.normalize(), which would turn
            # 20.00 into '2E+1'.
            trimmed = f"{self.value:.2f}".rstrip('0').rstrip('.')
            return f"{trimmed}% off"
        if self.discount_type == self.DiscountType.AMOUNT_OFF:
            return f"₵{self.value:,.2f} off"
        return f"₵{self.value:,.2f} flat"

    def is_running(self, at=None):
        from django.utils import timezone
        at = at or timezone.now()
        if not self.is_active or self.starts_at > at:
            return False
        return self.ends_at is None or self.ends_at >= at

    @property
    def status_label(self):
        from django.utils import timezone
        now = timezone.now()
        if not self.is_active:
            return 'OFF'
        if self.starts_at > now:
            return 'SCHEDULED'
        if self.ends_at and self.ends_at < now:
            return 'EXPIRED'
        return 'RUNNING'

    def covers(self, product):
        """
        Does this promotion apply to a given part?

        A category promotion covers everything underneath it, so a promotion on
        'Light' also covers 'Head Lamp' and 'Tail Lamp'.
        """
        if self.applies_to_all:
            return True
        if self.products.filter(pk=product.pk).exists():
            return True
        if product.category_id and self.categories.exists():
            promo_category_ids = set(self.categories.values_list('id', flat=True))
            node = product.category
            while node is not None:
                if node.id in promo_category_ids:
                    return True
                node = node.parent
        return False

    def price_for(self, normal_price):
        """The promotional price for a given normal price. Never below one pesewa."""
        normal_price = q2(normal_price)
        if self.discount_type == self.DiscountType.PERCENT:
            promo = normal_price - pct(normal_price, self.value)
        elif self.discount_type == self.DiscountType.AMOUNT_OFF:
            promo = normal_price - D(self.value)
        else:
            promo = D(self.value)
        return max(q2(promo), Decimal('0.01'))


def resolve_promotions(products, location, at=None):
    """
    Best running promotion per product, in a bounded number of queries.

    'Best' = the one that produces the lowest price for the customer. A
    promotion that would sell a part below landed cost without explicit
    authorisation suspends itself for that part rather than being applied --
    a promotion written last month must not turn into a loss when the exchange
    rate moves the cost up.

    Returns {product_id: {'promotion': Promotion, 'price': Decimal}} and a list
    of (product, promotion) pairs that were suspended, for surfacing in the UI.
    """
    products = list(products)
    if not products:
        return {}, []

    candidates = list(
        Promotion.objects.running(at).for_location(location).prefetch_related(
            'products', 'categories'
        )
    )
    if not candidates:
        return {}, []

    resolved = {}
    suspended = []
    for product in products:
        normal = q2(product.selling_price)
        best = None
        for promo in candidates:
            if not promo.covers(product):
                continue
            promo_price = promo.price_for(normal)
            if promo_price >= normal:
                continue  # not actually a discount
            # Guard: a promotion may only break the cost line if authorised.
            unit_cost = product.unit_cost(
                Product.SellMode.PAIR if product.is_sold_in_pairs else Product.SellMode.PIECE
            )
            if promo_price < unit_cost and not promo.allow_below_cost:
                suspended.append((product, promo))
                continue
            if best is None or promo_price < best[1]:
                best = (promo, promo_price)

        if best is not None:
            resolved[product.id] = {'promotion': best[0], 'price': best[1]}

    return resolved, suspended


class PriceChangeLog(models.Model):
    """
    Immutable audit trail of every price movement.

    Answers the only two questions that matter after the fact: who changed
    this price, and what was it before?
    """

    class Source(models.TextChoices):
        MANUAL = 'MANUAL', _('Edited by hand')
        SHIPMENT = 'SHIPMENT', _('Shipment received')
        IMPORT = 'IMPORT', _('Excel import')
        COST_SYNC = 'COST_SYNC', _('Cost re-synced from batches')
        SUGGESTION_ACCEPTED = 'SUGGESTION', _('Suggested price accepted')

    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='price_history')
    field = models.CharField(max_length=40, default='selling_price')
    old_value = models.DecimalField(max_digits=12, decimal_places=2, null=True, blank=True)
    new_value = models.DecimalField(max_digits=12, decimal_places=2)
    source = models.CharField(max_length=20, choices=Source.choices)
    was_applied = models.BooleanField(default=True, help_text="False = held back as a suggestion only")
    changed_by = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.SET_NULL, null=True, blank=True)
    note = models.CharField(max_length=255, blank=True)
    created_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        ordering = ['-created_at']
        indexes = [models.Index(fields=['product', '-created_at'])]

    def __str__(self):
        return f"{self.product.sku} {self.field}: {self.old_value} -> {self.new_value} ({self.source})"


class ProductImage(TimeStampedModel):
    product = models.ForeignKey(Product, on_delete=models.CASCADE, related_name='images')
    image = models.ImageField(upload_to='products/images/')
    is_primary = models.BooleanField(default=False)
    alt_text = models.CharField(max_length=255, blank=True)

    class Meta:
        ordering = ['-is_primary', '-created_at']

    def __str__(self):
        return f"Image for {self.product.name}"


