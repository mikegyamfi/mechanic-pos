from django.shortcuts import render, redirect
from django.contrib.auth.decorators import login_required
from django.utils import timezone
from django.db.models import Sum, Count, Q
from datetime import timedelta
from decimal import Decimal

from apps.analytics.models import DailyShopSummary
from apps.sales.models import Sale, SalePayment
from apps.inventory.models import StockBatch
from apps.location.models import Location


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
        status=Sale.Status.COMPLETED,
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
        status=Sale.Status.COMPLETED
    )

    # --- 3. Role-Specific Filters ---
    # If Cashier or Salesperson, they ONLY see their own transactions and revenue
    is_admin_role = user.role in ['OWNER', 'MANAGER', 'ACCOUNTANT']

    if not is_admin_role:
        sales_qs = sales_qs.filter(cashier=user)
        payments_qs = payments_qs.filter(processed_by=user)
        historical_sales_qs = historical_sales_qs.filter(cashier=user)
        current_view_name = f"My Sales ({current_view_name})"

    # --- 4. The Big Numbers (Today) ---
    todays_sales = sales_qs.aggregate(
        revenue=Sum('total_amount'),
        transactions=Count('id'),
        profit=Sum('items__total_price') - Sum('items__unit_cost')  # Simplified Gross Profit
    )

    # Strictly format to 2 decimal places
    revenue = Decimal(str(todays_sales['revenue'] or '0.00')).quantize(Decimal('0.01'))
    profit = Decimal(str(todays_sales['profit'] or '0.00')).quantize(Decimal('0.01'))
    transactions = todays_sales['transactions'] or 0

    # --- 5. Cash Flow (Money in Hand) ---
    payments = payments_qs.aggregate(
        cash=Sum('amount', filter=Q(payment_method='CASH')),
        digital=Sum('amount', filter=~Q(payment_method='CASH'))
    )

    # Strictly format to 2 decimal places
    cash_in_hand = Decimal(str(payments['cash'] or '0.00')).quantize(Decimal('0.01'))
    digital_sales = Decimal(str(payments['digital'] or '0.00')).quantize(Decimal('0.01'))

    # --- 6. Critical Alerts (Location-wide, not user specific) ---
    low_stock_count = StockBatch.objects.filter(
        location__in=analytics_scope,
        quantity__lte=5
    ).count()

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

        # Cash Flow
        'cash_in_hand': cash_in_hand,
        'digital_sales': digital_sales,

        # Alerts
        'low_stock_count': low_stock_count,

        # Charts
        'chart_labels': chart_labels,
        'chart_data': chart_data,
    }

    return render(request, 'dashboard/analytics.html', context)