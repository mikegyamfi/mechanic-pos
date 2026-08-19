from decimal import Decimal

from django import forms

from apps.core.money import D, q2

from .models import Product, Category, Brand, Unit, ProductImage, Promotion


class ProductForm(forms.ModelForm):
    """
    The Master Form for creating/editing products.
    Includes support for the essential Barcode/SKU fields.
    """

    class Meta:
        model = Product
        fields = [
            'name', 'category', 'brand', 'unit',
            'sku', 'barcode',
            'cost_price', 'selling_price', 'wholesale_price',
            'is_sold_in_pairs', 'split_price_mode', 'split_price_percentage', 'single_piece_price',
            'min_margin_percentage',
            'tax_rate', 'low_stock_threshold',
            'is_perishable', 'is_returnable',
            'weight_kg', 'dimensions', 'shelf_location',
            'description'
        ]
        widgets = {
            'description': forms.Textarea(attrs={'rows': 3}),
            'category': forms.Select(attrs={'class': 'select2 form-control'}),
            'brand': forms.Select(attrs={'class': 'select2'}),
            'unit': forms.Select(attrs={'class': 'select2'}),
            'split_price_percentage': forms.NumberInput(attrs={'step': '0.5', 'min': '1', 'max': '100'}),
        }
        labels = {
            'selling_price': 'Selling price (per PAIR for paired items)',
            'split_price_mode': 'How is a broken pair priced?',
            'split_price_percentage': 'Single piece = this % of the pair price',
            'single_piece_price': 'Fixed single-piece price',
            'min_margin_percentage': 'Minimum margin % for this part',
        }

    def clean_barcode(self):
        barcode = self.cleaned_data.get('barcode')
        if barcode:
            # Check if barcode exists (excluding current instance if editing)
            if Product.objects.filter(barcode=barcode).exclude(pk=self.instance.pk).exists():
                raise forms.ValidationError("This barcode is already assigned to another product.")
        return barcode

    def clean(self):
        """
        Guard the pricing rules so an impossible price can never be saved.
        """
        cleaned = super().clean()
        is_pair = cleaned.get('is_sold_in_pairs')
        mode = cleaned.get('split_price_mode')
        percentage = cleaned.get('split_price_percentage')
        fixed = cleaned.get('single_piece_price')
        selling = cleaned.get('selling_price')
        cost = cleaned.get('cost_price')
        wholesale = cleaned.get('wholesale_price')

        if is_pair and mode == Product.SplitPriceMode.FIXED and fixed is None:
            self.add_error('single_piece_price',
                           "Pick a fixed amount, or switch the rule back to a percentage of the pair price.")

        if is_pair and mode == Product.SplitPriceMode.PERCENT and not percentage:
            self.add_error('split_price_percentage',
                           "Enter the percentage of the pair price that one side sells for (e.g. 60).")

        # One side of a pair should not cost more than the whole pair.
        if is_pair and selling and fixed and mode == Product.SplitPriceMode.FIXED and D(fixed) > D(selling):
            self.add_error('single_piece_price',
                           f"A single piece (₵{q2(fixed)}) cannot cost more than the whole pair (₵{q2(selling)}).")

        if wholesale and selling and D(wholesale) > D(selling):
            self.add_error('wholesale_price',
                           "Wholesale price is above the retail price. Check the two figures.")

        # Selling below cost is a decision, not an accident -- warn loudly.
        if selling and cost:
            pair_cost = D(cost) * (Decimal('2') if is_pair else Decimal('1'))
            if D(selling) < pair_cost:
                self.add_error('selling_price',
                               f"This price (₵{q2(selling)}) is below the landed cost of "
                               f"₵{q2(pair_cost)}{' for a pair' if is_pair else ''}. "
                               f"Every sale would lose money.")

        return cleaned


class PromotionForm(forms.ModelForm):
    """
    Declaring a promotion is the only sanctioned way to price below the margin
    floor, so this form is where the guardrails live.
    """

    class Meta:
        model = Promotion
        fields = [
            'name', 'description',
            'discount_type', 'value',
            'applies_to_all', 'products', 'categories', 'locations',
            'starts_at', 'ends_at',
            'allow_below_cost', 'is_active',
        ]
        widgets = {
            'description': forms.Textarea(attrs={'rows': 2}),
            'starts_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
            'ends_at': forms.DateTimeInput(attrs={'type': 'datetime-local'}, format='%Y-%m-%dT%H:%M'),
            'products': forms.SelectMultiple(attrs={'size': 12}),
            'categories': forms.SelectMultiple(attrs={'size': 6}),
            'locations': forms.SelectMultiple(attrs={'size': 4}),
        }
        labels = {
            'applies_to_all': 'Apply to the entire catalogue',
            'allow_below_cost': 'Allow this promotion to sell below landed cost',
            'is_active': 'Switched on',
        }

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.fields['products'].queryset = Product.objects.filter(is_active=True).order_by('name')
        self.fields['categories'].queryset = Category.objects.filter(is_active=True).order_by('name')
        self.fields['products'].required = False
        self.fields['categories'].required = False
        self.fields['locations'].required = False
        if not self.instance.pk:
            from django.utils import timezone
            self.fields['starts_at'].initial = timezone.now()

    def clean_value(self):
        value = self.cleaned_data.get('value')
        discount_type = self.data.get('discount_type')
        if value is not None and discount_type == Promotion.DiscountType.PERCENT and value >= 100:
            raise forms.ValidationError("A discount of 100% or more would give the part away free.")
        return value

    def clean(self):
        cleaned = super().clean()
        starts_at = cleaned.get('starts_at')
        ends_at = cleaned.get('ends_at')
        applies_to_all = cleaned.get('applies_to_all')
        products = cleaned.get('products')
        categories = cleaned.get('categories')

        if ends_at and starts_at and ends_at <= starts_at:
            self.add_error('ends_at', "The end must come after the start.")

        if not applies_to_all and not products and not categories:
            raise forms.ValidationError(
                "Choose what this promotion covers: specific parts, whole categories, "
                "or tick 'apply to the entire catalogue'."
            )

        # Tell the manager exactly which parts would go below cost, and refuse
        # unless they have explicitly authorised a loss-leader.
        if not cleaned.get('allow_below_cost') and cleaned.get('value'):
            probe = Promotion(discount_type=cleaned.get('discount_type'), value=cleaned['value'])
            affected = []
            if applies_to_all:
                affected = list(Product.objects.filter(is_active=True)[:500])
            else:
                affected = list(products or [])
                for category in (categories or []):
                    affected += list(Product.objects.filter(category=category, is_active=True))

            losers = []
            for product in affected:
                mode = (Product.SellMode.PAIR if product.is_sold_in_pairs
                        else Product.SellMode.PIECE)
                promo_price = probe.price_for(product.selling_price)
                if promo_price < product.unit_cost(mode):
                    losers.append(f"{product.name} (₵{promo_price} vs cost ₵{product.unit_cost(mode)})")

            if losers:
                shown = "; ".join(losers[:4])
                more = f" and {len(losers) - 4} more" if len(losers) > 4 else ""
                self.add_error(
                    'value',
                    f"This would sell below landed cost: {shown}{more}. "
                    f"Reduce the discount, or tick 'allow below landed cost' to accept the loss."
                )

        return cleaned


class ProductImageForm(forms.ModelForm):
    class Meta:
        model = ProductImage
        fields = ['image', 'is_primary']


class CategoryForm(forms.ModelForm):
    class Meta:
        model = Category
        fields = ['name', 'parent', 'description']


class BrandForm(forms.ModelForm):
    class Meta:
        model = Brand
        fields = ['name', 'website', 'logo']
