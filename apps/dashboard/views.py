from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.db.models import Count, DecimalField, F, Q, Sum, Value
from django.db.models.functions import Coalesce
from datetime import timedelta
from decimal import Decimal

from apps.analytics.models import DailyShopSummary
from apps.core.money import D, q2
from apps.sales import services as sales_services
from apps.sales.models import Sale, SalePayment
from apps.inventory.models import StockBatch
from apps.location.models import Location
from apps.products.models import Product


@login_required
def dashboard_router(request):
    """
    Phase 1: The Traffic Controller.
    Decides where to send the user based on their Role.
    """
    user = request.user

    # 1. Cashiers & Salespeople -> Now authorized to see their performance dashboard
    if user.role in ['CASHIER', 'SALESPERSON']:
        return redirect('dashboard:analytics')

    # 2. Warehouse Staff -> Go to Inventory Ops
    elif user.role == 'WAREHOUSE_STAFF':
        return redirect('inventory:dashboard')

    # 3. Owners, Managers, Accountants -> Go to Analytics
    elif user.role in ['OWNER', 'MANAGER', 'ACCOUNTANT']:
        return redirect('dashboard:analytics')

    # Fallback
    return render(request, 'core/welcome.html')


@login_required
def owner_analytics(request):
    """
    Phase 2: The Data Engine (Owner & Staff View).
    Aggregates data for the dashboard. Data visibility depends on Role.
    """
    user = request.user

    # Now explicitly allowing front-line staff
    if user.role not in ['OWNER', 'MANAGER', 'ACCOUNTANT', 'CASHIER', 'SALESPERSON']:
        return redirect('dashboard:index')

    today = timezone.now().date()

    # --- 1. Context Switching & Scope Logic ---
    locations = Location.objects.filter(is_active=True)
    selected_location_id = request.GET.get('location')

    if user.role == 'OWNER':
        if selected_location_id:
            analytics_scope = locations.filter(id=selected_location_id)
            current_view_name = analytics_scope.first().name if analytics_scope.exists() else "All Locations"
        else:
            analytics_scope = locations
            current_view_name = "All Locations"
    else:
        # Everyone else is locked to their assigned location
        if user.assigned_location:
            analytics_scope = locations.filter(id=user.assigned_location.id)
            current_view_name = user.assigned_location.name
            selected_location_id = user.assigned_location.id
        else:
            analytics_scope = Location.objects.none()
            current_view_name = "Unassigned Location"

    # --- 2. Base Querysets ---
    sales_qs = Sale.objects.filter(
        created_at__date=today,
        status__in=Sale.REVENUE_STATUSES,
        location__in=analytics_scope
    )

    payments_qs = SalePayment.objects.filter(
        created_at__date=today,
        sale__location__in=analytics_scope
    )

    start_date = today - timedelta(days=6)
    historical_sales_qs = Sale.objects.filter(
        location__in=analytics_scope,
        created_at__date__gte=start_date,
        status__in=Sale.REVENUE_STATUSES
    )

    # --- 3. Role-Specific Filters ---
    # If Cashier or Salesperson, they ONLY see their own transactions and revenue
    is_admin_role = user.role in ['OWNER', 'MANAGER', 'ACCOUNTANT']

    if not is_admin_role:
        sales_qs = sales_qs.filter(cashier=user)
        payments_qs = payments_qs.filter(processed_by=user)
        historical_sales_qs = historical_sales_qs.filter(cashier=user)
        current_view_name = f"My Sales ({current_view_name})"

    # The shift the user has open right now, if any -- so a salesperson can see
    # their own drawer without walking to the close-register screen.
    open_session = sales_services.get_open_session(user)
    session_stats = None
    if open_session:
        open_session.recalculate()
        session_stats = {
            'opening': q2(open_session.opening_balance),
            'cash': q2(open_session.total_cash_sales),
            'digital': open_session.total_digital_sales,
            'credit': q2(open_session.total_credit_extended),
            'expected_cash': open_session.expected_cash,
            'started': open_session.start_time,
        }

    # --- 4. The Big Numbers (Today) ---
    # NOTE: revenue/count and the item-level cost MUST be aggregated separately.
    # Putting Sum('total_amount') and Sum('items__...') in one aggregate() joins
    # the items table and multiplies the revenue by the number of lines per sale.
    todays_sales = sales_qs.aggregate(
        revenue=Sum('total_amount'),
        refunded=Sum('refunded_amount'),
        cost=Sum('total_cost'),
        transactions=Count('id'),
    )

    revenue = q2(D(todays_sales['revenue'] or 0) - D(todays_sales['refunded'] or 0))
    profit = q2(revenue - D(todays_sales['cost'] or 0))
    transactions = todays_sales['transactions'] or 0
    average_basket = q2(revenue / transactions) if transactions else Decimal('0.00')

    # --- 5. Cash Flow (Money in Hand) ---
    # Signed sums: change given and refunds paid out are negative rows, so this
    # is the true net movement, not the gross takings.
    payments = payments_qs.aggregate(
        cash=Sum('amount', filter=Q(payment_method='CASH')),
        digital=Sum('amount', filter=~Q(payment_method='CASH')),
    )

    cash_in_hand = q2(payments['cash'] or 0)
    digital_sales = q2(payments['digital'] or 0)

    credit_extended = q2(
        sales_qs.aggregate(
            owed=Sum(F('total_amount') - F('refunded_amount') - F('amount_paid'))
        )['owed'] or 0
    )

    # --- 6. Stock health (location-wide, not user specific) ---
    stock_alerts = _stock_alerts(analytics_scope)

    # --- 7. Chart Data (Last 7 Days) ---
    sales_data = historical_sales_qs.values('created_at__date').annotate(
        total=Sum('total_amount')
    ).order_by('created_at__date')

    sales_map = {item['created_at__date']: item['total'] for item in sales_data}

    chart_labels = []
    chart_data = []

    for i in range(6, -1, -1):
        date = today - timedelta(days=i)
        chart_labels.append(date.strftime('%a'))
        amount = sales_map.get(date, 0)
        chart_data.append(float(amount))

    context = {
        'locations': locations,
        'selected_location_id': int(selected_location_id) if selected_location_id else None,
        'view_name': current_view_name,

        # Security Flag for UI
        'show_profit': is_admin_role,

        # Big Cards
        'revenue': revenue,
        'transactions': transactions,
        'profit': profit,
        'average_basket': average_basket,

        # Cash Flow
        'cash_in_hand': cash_in_hand,
        'digital_sales': digital_sales,
        'credit_extended': credit_extended,

        # Alerts (everyone on the floor needs these)
        'low_stock_count': stock_alerts['low_count'],
        'out_of_stock_count': stock_alerts['out_count'],
        'low_stock_items': stock_alerts['low_items'],
        'stock_value': stock_alerts['stock_value'] if is_admin_role else None,

        # My open shift
        'open_session': open_session,
        'session_stats': session_stats,

        # Charts
        'chart_labels': chart_labels,
        'chart_data': chart_data,
    }

    return render(request, 'dashboard/analytics.html', context)


def _stock_alerts(location_scope, limit=12):
    """
    What is running out. Every role sees this -- a salesperson who does not
    know a part is down to its last two pieces cannot do their job.
    """
    stock = Product.objects.filter(is_active=True).annotate(
        pieces=Coalesce(
            Sum('batches__quantity', filter=Q(batches__location__in=location_scope)),
            Value(0),
        )
    )

    low_items = list(
        stock.filter(pieces__gt=0, pieces__lte=F('low_stock_threshold'))
        .select_related('category')
        .order_by('pieces', 'name')[:limit]
    )

    low_count = stock.filter(pieces__gt=0, pieces__lte=F('low_stock_threshold')).count()
    out_count = stock.filter(pieces__lte=0).count()

    value = StockBatch.objects.filter(
        location__in=location_scope, quantity__gt=0
    ).aggregate(
        total=Sum(F('quantity') * F('cost_price'), output_field=DecimalField(max_digits=18, decimal_places=2))
    )['total']

    return {
        'low_items': low_items,
        'low_count': low_count,
        'out_count': out_count,
        'stock_value': q2(value or 0),
    }