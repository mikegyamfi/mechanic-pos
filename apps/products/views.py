from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from django.core.paginator import Paginator

from apps.core.money import q2

from django.views.decorators.http import require_POST

from .models import PriceChangeLog, Product, Category, Brand, Promotion
from .forms import ProductForm, ProductImageForm, CategoryForm, BrandForm, PromotionForm
from ..inventory.models import StockBatch
from ..location.models import Location


@login_required
def product_list(request):
    """
    The Central Catalog View.
    Features comprehensive search and filtering.
    """
    # 1. Base Query
    products = Product.objects.all().select_related('category', 'brand', 'unit').prefetch_related('images')

    # 2. Filtering
    query = request.GET.get('q', '')
    category_id = request.GET.get('category')
    brand_id = request.GET.get('brand')
    status = request.GET.get('status')
    location_id = request.GET.get('location')

    if query:
        products = products.filter(
            Q(name__icontains=query) |
            Q(sku__icontains=query) |
            Q(barcode__icontains=query)
        )

    if category_id:
        products = products.filter(category_id=category_id)

    if brand_id:
        products = products.filter(brand_id=brand_id)

    if status == 'active':
        products = products.filter(is_active=True)
    elif status == 'inactive':
        products = products.filter(is_active=False)

    # Owner-Only Filter: Show products stocked in a specific location
    if request.user.role == 'OWNER' and location_id:
        # distinct() is needed because a product might have multiple batches in one location
        products = products.filter(batches__location_id=location_id, batches__quantity__gt=0).distinct()

    # 3. Stock, so the catalogue never shows a price without a quantity
    products = products.annotate(
        pieces=Coalesce(Sum('batches__quantity'), Value(0)),
        batch_count=Count('batches', filter=Q(batches__quantity__gt=0), distinct=True),
    )

    # 4. Sorting
    products = products.order_by('-created_at')

    # 5. Pagination
    paginator = Paginator(products, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    # 6. Context Data for Filter Dropdowns
    context = {
        'page_obj': page_obj,
        'query': query,
        'suggestion_count': Product.objects.filter(suggested_selling_price__isnull=False).count(),
        'show_cost': request.user.role in ('OWNER', 'MANAGER', 'ACCOUNTANT'),
        'categories': Category.objects.filter(is_active=True),
        'brands': Brand.objects.filter(is_active=True),
        'locations': Location.objects.filter(is_active=True) if request.user.role == 'OWNER' else [],

        # Keep filter state in UI
        'selected_category': int(category_id) if category_id else None,
        'selected_brand': int(brand_id) if brand_id else None,
        'selected_status': status,
        'selected_location': int(location_id) if location_id else None,
    }

    return render(request, 'products/product_list.html', context)


@login_required
def product_detail(request, pk):
    """
    Detailed view of a product including stock levels across all locations.
    """
    product = get_object_or_404(Product, pk=pk)

    # Get Stock Summary per Location
    # This shows "Shop A: 50 units", "Warehouse: 100 units"
    stock_summary = StockBatch.objects.filter(
        product=product,
        quantity__gt=0
    ).values(
        'location__name', 'location__location_type'
    ).annotate(
        total_qty=Sum('quantity')
    ).order_by('location__name')

    # Calculate Global Total
    total_stock = sum(item['total_qty'] for item in stock_summary)

    # Every batch on hand, with its own true cost -- this is what the product's
    # cost_price is the weighted average of.
    batches = StockBatch.objects.filter(product=product, quantity__gt=0).select_related(
        'location', 'supplier'
    ).order_by('location__name', 'received_date')

    recent_batches = StockBatch.objects.filter(product=product).select_related(
        'location', 'supplier'
    ).order_by('-received_date')[:10]

    show_cost = request.user.role in ('OWNER', 'MANAGER', 'ACCOUNTANT')

    context = {
        'product': product,
        'stock_summary': stock_summary,
        'total_stock': total_stock,
        'batches': batches,
        'recent_batches': recent_batches,
        'show_cost': show_cost,
        # Pricing panel: the pair/split maths spelled out so nobody has to guess
        'single_price': product.effective_single_price if product.is_sold_in_pairs else None,
        'single_wholesale': product.effective_single_wholesale_price if product.is_sold_in_pairs else None,
        'weighted_cost': product.weighted_average_cost(),
        'stock_value': q2(sum((b.stock_value for b in batches), q2(0))) if show_cost else None,
        'price_history': product.price_history.select_related('changed_by')[:15],
    }
    return render(request, 'products/product_detail.html', context)


@login_required
def product_create(request):
    """
    Create a new product definition.
    """
    if request.method == 'POST':
        form = ProductForm(request.POST, request.FILES)
        if form.is_valid():
            product = form.save(commit=False)
            # A price typed by a human is a manual price from birth, so no
            # shipment can silently overwrite it later.
            product.price_is_manual = True
            product.save()
            PriceChangeLog.objects.create(
                product=product, field='selling_price', old_value=None,
                new_value=product.selling_price, source=PriceChangeLog.Source.MANUAL,
                changed_by=request.user, note='Product created',
            )
            messages.success(request, f"Product '{product.name}' created successfully.")
            return redirect('products:product_detail', pk=product.pk)
    else:
        form = ProductForm()

    return render(request, 'products/product_form.html', {
        'form': form,
        'category_form': CategoryForm(),
        'title': 'Add New Product',
    })


@login_required
def product_edit(request, pk):
    """
    Update existing product details.

    Price fields are written through `apply_price` so every movement lands in
    the price history with a name against it.
    """
    product = get_object_or_404(Product, pk=pk)

    if request.method == 'POST':
        form = ProductForm(request.POST, request.FILES, instance=product)
        if form.is_valid():
            price_fields = ('selling_price', 'wholesale_price', 'cost_price')
            new_prices = {f: form.cleaned_data.get(f) for f in price_fields}
            originals = {f: getattr(Product.objects.get(pk=product.pk), f) for f in price_fields}

            updated = form.save(commit=False)
            # Reset the price columns; apply_price writes them back with an audit row.
            for field in price_fields:
                setattr(updated, field, originals[field])
            updated.save()

            changed = []
            for field, value in new_prices.items():
                if value is None:
                    continue
                outcome = updated.apply_price(
                    value, source=PriceChangeLog.Source.MANUAL, changed_by=request.user,
                    field=field, note='Edited in product screen',
                )
                if outcome == 'applied':
                    changed.append(field.replace('_', ' '))

            if changed:
                messages.success(
                    request,
                    f"'{updated.name}' updated. Changed: {', '.join(changed)}. "
                    f"This price is now managed by hand and will not be overwritten by a shipment."
                )
            else:
                messages.success(request, f"Product '{updated.name}' updated.")
            return redirect('products:product_detail', pk=updated.pk)
    else:
        form = ProductForm(instance=product)

    return render(request, 'products/product_form.html', {
        'form': form,
        'product': product,
        'title': f'Edit {product.name}',
    })


@login_required
def price_suggestions(request):
    """
    Prices a shipment wanted to apply but could not, because a human had set
    the price by hand. Accept or dismiss each one.
    """
    if request.user.role not in ('OWNER', 'MANAGER'):
        messages.error(request, "Only managers can review price suggestions.")
        return redirect('products:product_list')

    if request.method == 'POST':
        product = get_object_or_404(Product, pk=request.POST.get('product_id'))
        action = request.POST.get('action')

        if action == 'accept' and product.suggested_selling_price is not None:
            new_price = product.suggested_selling_price
            product.apply_price(
                new_price, source=PriceChangeLog.Source.SUGGESTION_ACCEPTED,
                changed_by=request.user, note=product.suggested_price_source or 'Suggestion accepted',
            )
            messages.success(request, f"'{product.name}' is now priced at ₵{new_price:,.2f}.")
        else:
            product.suggested_selling_price = None
            product.suggested_price_source = ''
            product.suggested_price_at = None
            product.save(update_fields=['suggested_selling_price', 'suggested_price_source',
                                        'suggested_price_at'])
            messages.info(request, f"Suggestion for '{product.name}' dismissed. Price unchanged.")

        return redirect('products:price_suggestions')

    pending = Product.objects.filter(
        suggested_selling_price__isnull=False
    ).select_related('category', 'brand').order_by('-suggested_price_at')

    return render(request, 'products/price_suggestions.html', {'pending': pending})


MANAGER_ROLES = ('OWNER', 'MANAGER')


@login_required
def promotion_list(request):
    """
    Promotions are the single authorised way to price below the margin floor,
    so they get their own screen with their status spelled out.
    """
    if request.user.role not in MANAGER_ROLES:
        messages.error(request, "Only managers can manage promotions.")
        return redirect('products:product_list')

    promotions = Promotion.objects.prefetch_related(
        'products', 'categories', 'locations'
    ).order_by('-is_active', '-starts_at')

    return render(request, 'products/promotion_list.html', {
        'promotions': promotions,
        'running_count': Promotion.objects.running().count(),
    })


@login_required
def promotion_form(request, pk=None):
    """Create or edit a promotion."""
    if request.user.role not in MANAGER_ROLES:
        messages.error(request, "Only managers can manage promotions.")
        return redirect('products:product_list')

    promotion = get_object_or_404(Promotion, pk=pk) if pk else None

    if request.method == 'POST':
        form = PromotionForm(request.POST, instance=promotion)
        if form.is_valid():
            saved = form.save(commit=False)
            if promotion is None:
                saved.created_by = request.user
            saved.save()
            form.save_m2m()

            warning = ''
            if saved.allow_below_cost:
                warning = " This promotion is authorised to sell below landed cost."
            messages.success(
                request,
                f"Promotion '{saved.name}' saved ({saved.discount_label}, {saved.status_label})."
                + warning
            )
            return redirect('products:promotion_list')
    else:
        form = PromotionForm(instance=promotion)

    return render(request, 'products/promotion_form.html', {
        'form': form,
        'promotion': promotion,
        'title': f"Edit promotion: {promotion.name}" if promotion else 'New promotion',
    })


@login_required
@require_POST
def promotion_toggle(request, pk):
    """Switch a promotion on or off without deleting its history."""
    if request.user.role not in MANAGER_ROLES:
        messages.error(request, "Only managers can manage promotions.")
        return redirect('products:product_list')

    promotion = get_object_or_404(Promotion, pk=pk)
    promotion.is_active = not promotion.is_active
    promotion.save(update_fields=['is_active', 'updated_at'])

    if promotion.is_active:
        messages.success(request, f"'{promotion.name}' switched on — prices drop immediately.")
    else:
        messages.warning(request, f"'{promotion.name}' switched off — normal prices apply again.")
    return redirect('products:promotion_list')


@login_required
def quick_category_create(request):
    """
    HTMX or Modal view to add a category on the fly while creating a product.
    """
    if request.method == 'POST':
        form = CategoryForm(request.POST)
        if form.is_valid():
            category = form.save()
            # If HTMX, return a partial; if standard, redirect
            messages.success(request, f"Category '{category.name}' added.")
            return redirect('products:product_create')

    return render(request, 'products/partials/category_form.html', {'form': CategoryForm()})