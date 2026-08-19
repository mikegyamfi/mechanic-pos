import json
from decimal import Decimal

from django.contrib import messages
from django.contrib.auth.decorators import login_required
from django.core.paginator import Paginator
from django.db.models import Count, F, Q, Sum
from django.http import JsonResponse
from django.shortcuts import get_object_or_404, redirect, render
from django.template.loader import render_to_string
from django.utils import timezone
from django.views.decorators.http import require_GET, require_POST

from apps.core.money import D, ZERO, q2

from . import services
from .models import Delivery, RegisterSession, Sale, SalePayment
from .services import SaleError
from ..customers.models import Customer
from ..inventory.models import StockBatch
from ..location.models import Location
from ..products.models import Category, Product, resolve_promotions

MANAGER_ROLES = services.MANAGER_ROLES
PRICE_VISIBLE_ROLES = ('OWNER', 'MANAGER', 'ACCOUNTANT')


def _can_see_cost(user):
    """Cashiers and salespeople see prices and stock, not landed cost."""
    return user.role in PRICE_VISIBLE_ROLES


# ---------------------------------------------------------------------------
# POS TERMINAL
# ---------------------------------------------------------------------------
@login_required
def pos_view(request):
    """
    The Cashier's Cockpit.

    The catalogue is NOT dumped into the page any more -- it is paged and
    searched server-side through `api_products`, so a 10,000-part catalogue
    loads as fast as a 10-part one.
    """
    user = request.user
    location = services.resolve_location(user)

    if location is None:
        messages.error(request, "You are not assigned to a shop. Ask the owner to set your location.")
        return redirect('dashboard:index')

    active_session = services.get_open_session(user, location)

    if not active_session:
        if request.method == 'POST':
            opening = request.POST.get('opening_balance', 0)
            try:
                opening = q2(opening)
            except ValueError:
                messages.error(request, "Enter the opening cash as a number.")
                return render(request, 'sales/open_register.html', {'location': location})
            RegisterSession.objects.create(user=user, location=location, opening_balance=opening)
            return redirect('sales:pos')
        return render(request, 'sales/open_register.html', {'location': location})

    return render(request, 'sales/pos.html', {
        'session': active_session,
        'location': location,
        'categories': Category.objects.filter(is_active=True).order_by('name'),
        'can_see_cost': _can_see_cost(user),
        'is_manager': user.role in MANAGER_ROLES,
        'min_margin': location.min_margin_percentage,
        'shift_stats': _shift_stats(active_session),
    })


def _shift_stats(session):
    """What this cashier has done since opening the drawer."""
    session.recalculate()
    sales = Sale.objects.filter(register_session=session, status__in=Sale.REVENUE_STATUSES)
    agg = sales.aggregate(
        revenue=Sum('total_amount'),
        refunded=Sum('refunded_amount'),
        count=Count('id'),
    )
    revenue = q2(D(agg['revenue'] or 0) - D(agg['refunded'] or 0))
    return {
        'revenue': revenue,
        'transactions': agg['count'] or 0,
        'cash': q2(session.total_cash_sales),
        'digital': session.total_digital_sales,
        'credit': q2(session.total_credit_extended),
        'expected_cash': session.expected_cash,
        'opening': q2(session.opening_balance),
    }


def _product_row(product, location, stock_map, can_see_cost, promo_map=None):
    """One row of the POS catalogue table."""
    stock = stock_map.get(product.id, {'pieces': 0, 'batches': 0})
    pieces = stock['pieces']
    promotion = ((promo_map or {}).get(product.id) or {}).get('promotion')

    normal_retail = q2(product.selling_price)
    retail = product.base_price(
        Product.SellMode.PAIR if product.is_sold_in_pairs else Product.SellMode.PIECE,
        promotion=promotion,
    )
    wholesale = product.base_price(
        Product.SellMode.PAIR if product.is_sold_in_pairs else Product.SellMode.PIECE,
        wholesale=True, promotion=promotion,
    )

    row = {
        'id': product.id,
        'name': product.name,
        'sku': product.sku,
        'barcode': product.barcode or '',
        'category': product.category.name if product.category else '',
        'category_id': product.category_id,
        'brand': product.brand.name if product.brand else '',
        'shelf': product.shelf_location or '',
        'unit': product.unit.symbol if product.unit else 'pc',
        'is_pair': product.is_sold_in_pairs,
        'split_mode': product.split_price_mode,
        'split_percentage': float(product.split_price_percentage),
        'retail': float(retail),
        'wholesale': float(wholesale),
        'single_retail': float(product.split_price_from(retail)) if product.is_sold_in_pairs else None,
        'single_wholesale': float(product.split_price_from(wholesale)) if product.is_sold_in_pairs else None,
        'pieces': pieces,
        'batches': stock['batches'],
        'sellable_pairs': pieces // 2 if product.is_sold_in_pairs else None,
        'low_stock_threshold': product.low_stock_threshold,
        'is_out': pieces <= 0,
        'is_low': 0 < pieces <= product.low_stock_threshold,
        'floor_pair': float(product.price_floor(Product.SellMode.PAIR, location, promotion=promotion)),
        'floor_piece': float(product.price_floor(Product.SellMode.PIECE, location, promotion=promotion)),
        'floor_single': float(product.price_floor(Product.SellMode.SINGLE, location, promotion=promotion)),
        'floor_pair_wholesale': float(product.price_floor(
            Product.SellMode.PAIR, location, promotion=promotion, wholesale=True)),
        'floor_piece_wholesale': float(product.price_floor(
            Product.SellMode.PIECE, location, promotion=promotion, wholesale=True)),
        'floor_single_wholesale': float(product.price_floor(
            Product.SellMode.SINGLE, location, promotion=promotion, wholesale=True)),
        'has_suggestion': product.suggested_selling_price is not None,
        # Undiscounted prices, so the terminal can show the customer their saving.
        'normal_retail': float(normal_retail),
        'normal_single_retail': (float(product.split_price_from(normal_retail))
                                 if product.is_sold_in_pairs else None),
        # Promotion: the only authorised way under the margin floor
        'promo': ({
            'id': promotion.id,
            'name': promotion.name,
            'label': promotion.discount_label,
            'normal_price': float(normal_retail),
            'ends_at': promotion.ends_at.strftime('%d %b %Y') if promotion.ends_at else None,
        } if promotion else None),
    }

    if can_see_cost:
        row['cost'] = float(q2(product.cost_price))
        row['margin_retail'] = float(product.margin_percentage(
            retail, Product.SellMode.PAIR if product.is_sold_in_pairs else Product.SellMode.PIECE
        ))
    return row


@login_required
@require_GET
def api_products(request):
    """
    Server-side catalogue search for the POS table.

    Searches the whole catalogue -- not just the page on screen -- across name,
    SKU, barcode, part number, brand and shelf location.
    """
    user = request.user
    location = services.resolve_location(user)
    if location is None:
        return JsonResponse({'success': False, 'message': 'No location assigned.'}, status=400)

    query = (request.GET.get('q') or '').strip()
    category_id = request.GET.get('category') or ''
    stock_filter = request.GET.get('stock') or 'all'
    sort = request.GET.get('sort') or 'name'
    try:
        page_number = max(int(request.GET.get('page', 1)), 1)
    except ValueError:
        page_number = 1
    try:
        per_page = min(max(int(request.GET.get('per_page', 25)), 5), 100)
    except ValueError:
        per_page = 25

    products = Product.objects.filter(is_active=True).select_related('category', 'brand', 'unit')

    if query:
        products = products.filter(
            Q(name__icontains=query)
            | Q(sku__icontains=query)
            | Q(barcode__icontains=query)
            | Q(manufacturer_part_number__icontains=query)
            | Q(brand__name__icontains=query)
            | Q(shelf_location__icontains=query)
        )

    if category_id.isdigit():
        products = products.filter(category_id=int(category_id))

    # Stock at THIS location only -- annotated so we can filter and sort on it.
    products = products.annotate(
        stock_here=Sum('batches__quantity', filter=Q(batches__location=location)),
    )

    if stock_filter == 'in':
        products = products.filter(stock_here__gt=0)
    elif stock_filter == 'out':
        products = products.filter(Q(stock_here__lte=0) | Q(stock_here__isnull=True))
    elif stock_filter == 'low':
        products = products.filter(stock_here__gt=0, stock_here__lte=F('low_stock_threshold'))

    sort_map = {
        'name': ['name'],
        'sku': ['sku'],
        'price_asc': ['selling_price', 'name'],
        'price_desc': ['-selling_price', 'name'],
        'stock_asc': [F('stock_here').asc(nulls_first=True), 'name'],
        'stock_desc': [F('stock_here').desc(nulls_last=True), 'name'],
        'newest': ['-created_at'],
    }
    products = products.order_by(*sort_map.get(sort, sort_map['name']))

    paginator = Paginator(products, per_page)
    page = paginator.get_page(page_number)

    page_products = list(page.object_list)
    stock_map = _stock_map([p.id for p in page_products], location)
    promo_map, _suspended = resolve_promotions(page_products, location)
    can_see_cost = _can_see_cost(user)

    return JsonResponse({
        'success': True,
        'results': [_product_row(p, location, stock_map, can_see_cost, promo_map)
                    for p in page_products],
        'page': page.number,
        'num_pages': paginator.num_pages,
        'total': paginator.count,
        'per_page': per_page,
        'has_next': page.has_next(),
        'has_previous': page.has_previous(),
        'start_index': page.start_index() if paginator.count else 0,
        'end_index': page.end_index() if paginator.count else 0,
    })


def _stock_map(product_ids, location):
    """{product_id: {pieces, batches}} at one location, in a single query."""
    rows = StockBatch.objects.filter(
        product_id__in=product_ids, location=location, quantity__gt=0
    ).values('product_id').annotate(pieces=Sum('quantity'), batches=Count('id'))
    return {r['product_id']: {'pieces': r['pieces'] or 0, 'batches': r['batches']} for r in rows}


@login_required
@require_GET
def api_product_batches(request, pk):
    """
    Batch-level detail for one part, for the POS stock popup.

    Salespeople get to see exactly what is on the shelf and where it came
    from; landed cost stays hidden unless their role allows it.
    """
    user = request.user
    location = services.resolve_location(user)
    product = get_object_or_404(Product, pk=pk)
    can_see_cost = _can_see_cost(user)

    batches = StockBatch.objects.filter(product=product, quantity__gt=0).select_related(
        'location', 'supplier'
    ).order_by('location__name', 'expiry_date', 'received_date')

    rows = []
    for batch in batches:
        row = {
            'id': batch.id,
            'batch_number': batch.batch_number or '-',
            'location': batch.location.name,
            'is_here': batch.location_id == (location.id if location else None),
            'quantity': batch.quantity,
            'initial_quantity': batch.initial_quantity,
            'sold': batch.quantity_sold,
            'supplier': batch.supplier.name if batch.supplier else '-',
            'received': batch.received_date.strftime('%d %b %Y'),
            'expiry': batch.expiry_date.strftime('%d %b %Y') if batch.expiry_date else None,
            'notes': batch.notes,
        }
        if can_see_cost:
            row['cost_price'] = float(q2(batch.cost_price))
            row['stock_value'] = float(batch.stock_value)
        rows.append(row)

    here = sum(r['quantity'] for r in rows if r['is_here'])
    payload = {
        'success': True,
        'product': {
            'id': product.id,
            'name': product.name,
            'sku': product.sku,
            'is_pair': product.is_sold_in_pairs,
            'retail': float(q2(product.selling_price)),
            'single': float(product.effective_single_price) if product.is_sold_in_pairs else None,
            'split_mode': product.split_price_mode,
            'split_percentage': float(product.split_price_percentage),
            'pieces_here': here,
            'pieces_total': product.stock_on_hand(),
            'pairs_here': here // 2 if product.is_sold_in_pairs else None,
            'odd_piece': bool(product.is_sold_in_pairs and here % 2) if product.is_sold_in_pairs else False,
        },
        'batches': rows,
    }
    if can_see_cost:
        payload['product']['cost'] = float(q2(product.cost_price))
        payload['product']['average_cost'] = float(product.weighted_average_cost() or 0)
    return JsonResponse(payload)


@login_required
@require_POST
def api_quote_cart(request):
    """
    Price a cart without committing it.

    The POS calls this on every change so the cashier always sees the exact
    number the server will charge -- the two can never drift apart.
    """
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'message': 'Malformed request.'}, status=400)

    location = services.resolve_location(request.user)
    if location is None:
        return JsonResponse({'success': False, 'message': 'No location assigned.'}, status=400)

    try:
        quote = services.quote_cart(
            data.get('cart', []), location, request.user, wholesale=bool(data.get('wholesale'))
        )
    except SaleError as exc:
        return JsonResponse({'success': False, 'code': exc.code, 'message': exc.message,
                             'detail': exc.detail}, status=400)

    return JsonResponse({
        'success': True,
        'subtotal': float(quote.subtotal),
        'tax': float(quote.tax),
        'total': float(quote.total),
        'lines': [{
            'product_id': line.product.id,
            'quantity': line.quantity,
            'sell_mode': line.sell_mode,
            'unit_price': float(line.unit_price),
            'list_price': float(line.list_price),
            'normal_price': float(line.normal_price),
            'line_total': float(line.line_total),
            'below_floor': line.below_floor,
            'floor': float(line.floor),
            'pieces': line.pieces_needed,
            'promotion': line.promotion.name if line.promotion else None,
        } for line in quote.lines],
        'promotions': [{'name': p.name, 'label': p.discount_label} for p in quote.promotions],
    })


@login_required
@require_POST
def process_sale(request):
    """
    Commit a sale.

    Thin wrapper: every rule lives in services.create_sale so the same
    guarantees apply however a sale is created.
    """
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'message': 'Malformed request.'}, status=400)

    customer = None
    customer_id = data.get('customer_id')
    if customer_id:
        customer = Customer.objects.filter(pk=customer_id).first()
        if customer is None:
            return JsonResponse({'success': False, 'message': 'That customer no longer exists.'}, status=400)

    try:
        sale = services.create_sale(
            user=request.user,
            cart=data.get('cart', []),
            payments=data.get('payments', []),
            customer=customer,
            wholesale=bool(data.get('wholesale')),
            expected_total=data.get('total_amount'),
            notes=data.get('notes', '') or '',
            allow_stock_correction=bool(data.get('allow_stock_correction')),
        )
    except SaleError as exc:
        return JsonResponse({'success': False, 'code': exc.code, 'message': exc.message,
                             'detail': exc.detail}, status=400)

    receipt_html = render_to_string('sales/partials/receipt_content.html',
                                    {'sale': sale, 'location': sale.location}, request=request)

    session = sale.register_session
    return JsonResponse({
        'success': True,
        'invoice_number': sale.invoice_number,
        'sale_id': sale.id,
        'total': float(sale.total_amount),
        'paid': float(sale.amount_paid),
        'change': float(sale.change_due),
        'balance': float(sale.balance_remaining),
        'receipt_html': receipt_html,
        'shift': {k: float(v) if isinstance(v, Decimal) else v
                  for k, v in _shift_stats(session).items()} if session else {},
    })


@login_required
@require_GET
def receipt_html(request, pk):
    """Re-print a receipt for an existing sale."""
    sale = get_object_or_404(Sale.objects.select_related('location', 'customer', 'cashier'), pk=pk)
    location = services.resolve_location(request.user)
    if request.user.role not in PRICE_VISIBLE_ROLES and sale.location_id != getattr(location, 'id', None):
        return JsonResponse({'success': False, 'message': 'That receipt belongs to another shop.'}, status=403)
    return JsonResponse({
        'success': True,
        'invoice_number': sale.invoice_number,
        'receipt_html': render_to_string('sales/partials/receipt_content.html',
                                         {'sale': sale, 'location': sale.location}, request=request),
    })


# ---------------------------------------------------------------------------
# HISTORY
# ---------------------------------------------------------------------------
@login_required
def sale_list(request):
    """
    Transaction History with Filters.
    Includes Debt/Arrears filtering and Owner "God Mode".
    """
    user = request.user

    if user.role == 'OWNER':
        sales = Sale.objects.all()
        location_filter = request.GET.get('location')
        if location_filter:
            sales = sales.filter(location_id=location_filter)
    else:
        sales = Sale.objects.filter(location=services.resolve_location(user))

    sales = sales.select_related('customer', 'cashier', 'location').order_by('-created_at')

    query = request.GET.get('q')
    if query:
        sales = sales.filter(
            Q(invoice_number__icontains=query)
            | Q(customer__phone_number__icontains=query)
            | Q(customer__first_name__icontains=query)
            | Q(customer__workshop_name__icontains=query)
        )

    status = request.GET.get('status')
    if status:
        if status == 'DEBT':
            sales = sales.filter(
                status__in=Sale.REVENUE_STATUSES,
            ).annotate(
                outstanding=F('total_amount') - F('refunded_amount') - F('amount_paid')
            ).filter(outstanding__gt=0)
        elif status == 'OVERRIDE':
            sales = sales.filter(has_price_override=True)
        else:
            sales = sales.filter(status=status)

    start_date = request.GET.get('start_date')
    end_date = request.GET.get('end_date')
    if start_date:
        sales = sales.filter(created_at__date__gte=start_date)
    if end_date:
        sales = sales.filter(created_at__date__lte=end_date)

    totals = sales.aggregate(
        revenue=Sum('total_amount'),
        refunded=Sum('refunded_amount'),
        paid=Sum('amount_paid'),
    )
    summary = {
        'revenue': q2(D(totals['revenue'] or 0) - D(totals['refunded'] or 0)),
        'paid': q2(totals['paid'] or 0),
        'outstanding': q2(D(totals['revenue'] or 0) - D(totals['refunded'] or 0) - D(totals['paid'] or 0)),
    }

    paginator = Paginator(sales, 50)
    page = paginator.get_page(request.GET.get('page'))

    return render(request, 'sales/sale_list.html', {
        'sales': page.object_list,
        'page_obj': page,
        'summary': summary,
        'show_profit': _can_see_cost(user),
        'filters': {
            'q': query,
            'status': status,
            'start_date': start_date,
            'end_date': end_date,
            'location': request.GET.get('location'),
        },
        'locations': Location.objects.filter(is_active=True) if user.role == 'OWNER' else [],
    })


@login_required
def sale_detail(request, pk):
    """View Receipt / Sale Details."""
    sale = get_object_or_404(
        Sale.objects.select_related('customer', 'cashier', 'location', 'register_session'),
        pk=pk,
    )
    items = sale.items.select_related('product', 'source_batch').prefetch_related('batch_lines__batch')
    return render(request, 'sales/sale_detail.html', {
        'sale': sale,
        'items': items,
        'payments': sale.payments.select_related('processed_by', 'register_session'),
        'show_profit': _can_see_cost(request.user),
        'can_refund': request.user.role in MANAGER_ROLES,
        'payment_methods': SalePayment.PaymentMethod.choices,
    })


# ---------------------------------------------------------------------------
# REGISTER SESSIONS
# ---------------------------------------------------------------------------
@login_required
def session_list(request):
    """List of cashier shifts (for closing/reconciling)."""
    user = request.user
    if user.role == 'OWNER':
        sessions = RegisterSession.objects.all()
    elif user.role in ('MANAGER', 'ACCOUNTANT'):
        sessions = RegisterSession.objects.filter(location=services.resolve_location(user))
    else:
        sessions = RegisterSession.objects.filter(user=user)

    sessions = sessions.select_related('user', 'location').order_by('-start_time')
    paginator = Paginator(sessions, 50)
    page = paginator.get_page(request.GET.get('page'))

    return render(request, 'sales/session_list.html', {
        'sessions': page.object_list,
        'page_obj': page,
        'open_session': services.get_open_session(user),
    })


@login_required
def close_register_view(request):
    """
    End of Shift Logic.
    1. Cashier counts physical money.
    2. Enters totals.
    3. System calculates variance.
    """
    user = request.user
    session = services.get_open_session(user)

    if not session:
        messages.error(request, "No open register session found.")
        return redirect('sales:sessions')

    session.recalculate()

    if request.method == 'POST':
        try:
            services.close_session(
                session=session,
                user=user,
                actual_cash=request.POST.get('actual_cash', '0'),
                notes=request.POST.get('notes', ''),
            )
        except SaleError as exc:
            messages.error(request, exc.message)
            return redirect('sales:close_register')

        if session.status == RegisterSession.Status.DISCREPANCY:
            messages.warning(
                request,
                f"Register closed with a variance of ₵{session.discrepancy:,.2f}. "
                f"Expected ₵{session.closing_balance_expected:,.2f}, counted "
                f"₵{session.closing_balance_actual:,.2f}."
            )
        else:
            messages.success(request, "Register closed and balanced exactly. Well done.")
        return redirect('sales:session_detail', pk=session.pk)

    return render(request, 'sales/close_register.html', {
        'session': session,
        'expected_cash': session.expected_cash,
        'stats': _shift_stats(session),
    })


@login_required
def session_detail(request, pk):
    """Detailed Report of a Cashier Shift (Session)."""
    session = get_object_or_404(RegisterSession.objects.select_related('user', 'location'), pk=pk)

    if request.user.role not in PRICE_VISIBLE_ROLES and session.user != request.user:
        messages.error(request, "You do not have permission to view this report.")
        return redirect('sales:sessions')

    if session.status == RegisterSession.Status.OPEN:
        session.recalculate()

    sales = session.sales.select_related('customer').order_by('-created_at')

    return render(request, 'sales/session_detail.html', {
        'session': session,
        'sales': sales,
        'stats': _shift_stats(session) if session.status == RegisterSession.Status.OPEN else None,
        'payments': session.payments.select_related('sale').order_by('created_at'),
        'expenses': session.expenses.select_related('category').order_by('created_at'),
        'show_profit': _can_see_cost(request.user),
    })


# ---------------------------------------------------------------------------
# DEBT & REFUNDS
# ---------------------------------------------------------------------------
@login_required
@require_POST
def add_payment(request, pk):
    """Settle Debt: Add a payment to an existing sale."""
    sale = get_object_or_404(Sale, pk=pk)
    try:
        payment = services.settle_debt(
            sale=sale,
            user=request.user,
            amount=request.POST.get('amount', 0),
            method=request.POST.get('payment_method', ''),
            reference=request.POST.get('reference', ''),
        )
    except SaleError as exc:
        messages.error(request, exc.message)
        return redirect('sales:detail', pk=pk)

    sale.refresh_from_db()
    if sale.balance_remaining > 0:
        messages.success(
            request,
            f"₵{payment.amount:,.2f} received. ₵{sale.balance_remaining:,.2f} still outstanding."
        )
    else:
        messages.success(request, f"₵{payment.amount:,.2f} received. Invoice is now fully settled.")
    return redirect('sales:detail', pk=pk)


@login_required
def process_refund(request, pk):
    """Handle Full or Partial Refunds -- restocks inventory and reverses money."""
    sale = get_object_or_404(Sale.objects.select_related('customer', 'location'), pk=pk)

    if request.user.role not in MANAGER_ROLES:
        messages.error(request, "Only Managers can process refunds.")
        return redirect('sales:detail', pk=pk)

    if request.method == 'POST':
        lines = []
        for item in sale.items.all():
            raw = request.POST.get(f'refund_qty_{item.id}')
            if raw:
                try:
                    quantity = int(raw)
                except ValueError:
                    quantity = 0
                if quantity > 0:
                    lines.append({'item_id': item.id, 'quantity': quantity})

        try:
            result = services.refund_sale(
                sale=sale,
                user=request.user,
                lines=lines,
                reason=request.POST.get('reason', 'Customer return'),
                refund_method=request.POST.get('refund_method', 'CASH'),
                restock=request.POST.get('restock', 'on') == 'on',
            )
        except SaleError as exc:
            messages.error(request, exc.message)
            return redirect('sales:refund', pk=pk)

        parts = [f"Refunded ₵{result['refund_value']:,.2f}"]
        if result['offset_against_debt'] > 0:
            parts.append(f"₵{result['offset_against_debt']:,.2f} written off the mechanic's debt")
        if result['cash_back'] > 0:
            parts.append(f"₵{result['cash_back']:,.2f} paid back out of the drawer")
        messages.success(request, ". ".join(parts) + ".")
        return redirect('sales:detail', pk=pk)

    return render(request, 'sales/process_refund.html', {
        'sale': sale,
        'items': sale.items.select_related('product'),
        'payment_methods': [c for c in SalePayment.PaymentMethod.choices if c[0] != 'CREDIT'],
    })


@login_required
def refund_list(request):
    """Specific list for Returned/Refunded transactions."""
    user = request.user
    if user.role == 'OWNER':
        refunds = Sale.objects.all()
    else:
        refunds = Sale.objects.filter(location=services.resolve_location(user))

    refunds = refunds.filter(
        status__in=[Sale.Status.REFUNDED, Sale.Status.PARTIAL_REFUND]
    ).select_related('customer', 'cashier').order_by('-updated_at')

    total_refunded = refunds.aggregate(total=Sum('refunded_amount'))['total'] or ZERO

    return render(request, 'sales/refund_list.html', {
        'refunds': refunds,
        'total_refunded': q2(total_refunded),
    })


# ---------------------------------------------------------------------------
# DELIVERIES
# ---------------------------------------------------------------------------
@login_required
def delivery_management(request, pk=None):
    """Manage Deliveries. If pk is provided, edit specific delivery. Else list pending."""
    if pk:
        delivery = get_object_or_404(Delivery, pk=pk)
        if request.method == 'POST':
            status = request.POST.get('status')
            rider_name = request.POST.get('rider_name')
            tracking_ref = request.POST.get('tracking_ref')
            cost = request.POST.get('cost_to_business')

            if status:
                delivery.status = status
            if rider_name:
                delivery.rider_name = rider_name
            if tracking_ref:
                delivery.tracking_reference = tracking_ref
            if cost:
                try:
                    delivery.cost_to_business = q2(cost)
                except ValueError:
                    messages.error(request, "Delivery cost must be a number.")
                    return redirect('sales:delivery_edit', pk=pk)

            if status == 'DELIVERED':
                delivery.delivered_at = timezone.now()
            elif status == 'DISPATCHED':
                delivery.dispatched_at = timezone.now()

            delivery.save()
            messages.success(request, "Delivery updated.")
            return redirect('sales:deliveries')

        return render(request, 'sales/delivery_form.html', {'delivery': delivery})

    deliveries = Delivery.objects.filter(
        sale__location=services.resolve_location(request.user)
    ).select_related('sale', 'sale__customer').order_by('-created_at')
    return render(request, 'sales/delivery_list.html', {'deliveries': deliveries})
