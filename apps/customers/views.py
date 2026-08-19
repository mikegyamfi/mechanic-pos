import json

from django.shortcuts import render, redirect, get_object_or_404
from django.contrib.auth.decorators import login_required
from django.contrib import messages
from django.http import JsonResponse
from django.db.models import Q, Sum, Count, F
from django.core.paginator import Paginator
from django.views.decorators.http import require_GET, require_POST

from apps.core.money import D, ZERO, q2

from .models import Customer
from .forms import CustomerForm


@login_required
def customer_list(request):
    """
    List of all customers with search functionality.
    """
    query = request.GET.get('q', '')
    customers = Customer.objects.all().order_by('-last_visit_date')

    if query:
        customers = customers.filter(
            Q(first_name__icontains=query) |
            Q(last_name__icontains=query) |
            Q(phone_number__icontains=query) |
            Q(email__icontains=query)
        )

    paginator = Paginator(customers, 20)
    page_number = request.GET.get('page')
    page_obj = paginator.get_page(page_number)

    return render(request, 'customers/customer_list.html', {
        'page_obj': page_obj,
        'query': query
    })


@login_required
def customer_create(request):
    if request.method == 'POST':
        form = CustomerForm(request.POST)
        if form.is_valid():
            customer = form.save()
            messages.success(request, f"Customer {customer.get_display_name} added successfully.")
            return redirect('customers:list')
    else:
        form = CustomerForm()
    return render(request, 'customers/customer_form.html', {'form': form, 'title': 'Add Customer'})


@login_required
def customer_edit(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    if request.method == 'POST':
        form = CustomerForm(request.POST, instance=customer)
        if form.is_valid():
            form.save()
            messages.success(request, f"Customer {customer.get_display_name} updated.")
            return redirect('customers:list')
    else:
        form = CustomerForm(instance=customer)
    return render(request, 'customers/customer_form.html', {'form': form, 'title': 'Edit Customer'})


@login_required
def customer_detail(request, pk):
    customer = get_object_or_404(Customer, pk=pk)
    # Get purchase history (reverse relation from Sale model)
    purchases = customer.purchases.all().order_by('-created_at')[:20]

    # Calculate lifetime stats
    stats = customer.purchases.aggregate(
        total_spent=Sum('total_amount'),
        visit_count=Count('id')
    )

    return render(request, 'customers/customer_detail.html', {
        'customer': customer,
        'purchases': purchases,
        'stats': stats
    })


def _customer_payload(customer):
    """
    The shape the POS needs to make a credit decision at the counter:
    who they are, what they already owe, and what is left on their limit.
    """
    return {
        'id': customer.id,
        'name': customer.display_name,
        'phone': customer.phone_number,
        'email': customer.email or '',
        'customer_type': customer.get_customer_type_display(),
        'credit_limit': float(customer.credit_limit),
        'current_debt': float(customer.current_debt),
        'available_credit': float(customer.available_credit),
        'total_spent': float(customer.total_spent),
    }


@login_required
@require_GET
def api_search_customers(request):
    """API Endpoint for the POS to search customers by name, workshop or phone."""
    query = (request.GET.get('q') or '').strip()
    if len(query) < 2:
        return JsonResponse({'results': []})

    customers = Customer.objects.filter(
        Q(first_name__icontains=query)
        | Q(last_name__icontains=query)
        | Q(phone_number__icontains=query)
        | Q(workshop_name__icontains=query)
    )[:15]

    return JsonResponse({'results': [_customer_payload(c) for c in customers]})


@login_required
@require_POST
def api_create_customer(request):
    """Quick endpoint for the POS to register a mechanic mid-sale."""
    try:
        data = json.loads(request.body or '{}')
    except json.JSONDecodeError:
        return JsonResponse({'success': False, 'message': 'Malformed request.'}, status=400)

    phone = (data.get('phone') or '').strip()
    if not phone:
        return JsonResponse({'success': False, 'message': 'Phone number required'}, status=400)

    name = (data.get('name') or '').strip()
    workshop = (data.get('workshop') or '').strip()

    customer, created = Customer.objects.get_or_create(
        phone_number=phone,
        defaults={
            'first_name': name,
            'workshop_name': workshop,
            'customer_type': 'MECHANIC' if workshop else 'WALK_IN',
        },
    )

    # Fill in anything we learned about an existing silent-accumulation profile.
    updates = []
    if not created:
        if name and not customer.first_name:
            customer.first_name = name
            updates.append('first_name')
        if workshop and not customer.workshop_name:
            customer.workshop_name = workshop
            updates.append('workshop_name')
        if updates:
            customer.save(update_fields=updates)

    return JsonResponse({
        'success': True,
        'created': created,
        'customer': _customer_payload(customer),
    })


@login_required
def receivables(request):
    """
    Who owes the shop money, oldest debt first.

    This is the working screen for part-payments: a mechanic pays something
    today and the rest later, and this is where you see what is still out.
    """
    from django.utils import timezone
    from apps.sales.models import Sale

    query = (request.GET.get('q') or '').strip()

    unpaid = Sale.objects.filter(status__in=Sale.REVENUE_STATUSES).annotate(
        outstanding=F('total_amount') - F('refunded_amount') - F('amount_paid')
    ).filter(outstanding__gt=0, customer__isnull=False)

    # Non-owners only see debt raised at their own shop.
    if request.user.role != 'OWNER':
        from apps.sales import services as sales_services
        location = sales_services.resolve_location(request.user)
        if location is not None:
            unpaid = unpaid.filter(location=location)

    if query:
        unpaid = unpaid.filter(
            Q(customer__first_name__icontains=query)
            | Q(customer__last_name__icontains=query)
            | Q(customer__phone_number__icontains=query)
            | Q(customer__workshop_name__icontains=query)
            | Q(invoice_number__icontains=query)
        )

    unpaid = unpaid.select_related('customer', 'location').order_by('created_at')

    today = timezone.now().date()
    rows = {}
    for sale in unpaid:
        row = rows.setdefault(sale.customer_id, {
            'customer': sale.customer,
            'invoices': [],
            'total': ZERO,
            'oldest_days': 0,
        })
        row['invoices'].append(sale)
        row['total'] = q2(row['total'] + D(sale.outstanding))
        row['oldest_days'] = max(row['oldest_days'], (today - sale.created_at.date()).days)

    debtors = sorted(rows.values(), key=lambda r: r['oldest_days'], reverse=True)
    grand_total = q2(sum((r['total'] for r in debtors), ZERO))
    over_limit = [r for r in debtors if r['total'] > D(r['customer'].credit_limit)]

    return render(request, 'customers/receivables.html', {
        'debtors': debtors,
        'grand_total': grand_total,
        'invoice_count': len(unpaid),
        'over_limit_count': len(over_limit),
        'query': query,
    })


@login_required
def customer_statement(request, pk):
    """Generates a professional statement of account (Arrears) for a mechanic."""
    customer = get_object_or_404(Customer, pk=pk)

    # `unpaid_invoices()` annotates each row with `outstanding`, net of refunds.
    unpaid_invoices = customer.unpaid_invoices().select_related('location').order_by('created_at')

    return render(request, 'customers/statement.html', {
        'customer': customer,
        'unpaid_invoices': unpaid_invoices,
        'total_outstanding': customer.current_debt,
        'available_credit': customer.available_credit,
    })

