"""
Backfill the new money fields onto historical rows.

Without this, every sale made before this release reports zero COGS, no
drawer attribution for its payments, and a zero list price -- which would make
the new profit and reconciliation reports quietly wrong about the past.
"""
from decimal import Decimal

from django.db import migrations


def forwards(apps, schema_editor):
    SaleItem = apps.get_model('sales', 'SaleItem')
    Sale = apps.get_model('sales', 'Sale')
    SalePayment = apps.get_model('sales', 'SalePayment')

    # --- Line items ---
    # Legacy lines were stored per PIECE with a per-piece price, so
    # pieces_per_unit stays 1 and the maths carries over unchanged.
    for item in SaleItem.objects.all().iterator():
        item.pieces_per_unit = 1
        item.sell_mode = 'PIECE'
        item.list_price = item.unit_price
        item.total_cost = (Decimal(item.unit_cost or 0) * item.quantity).quantize(Decimal('0.01'))
        if item.is_refunded:
            item.quantity_refunded = item.quantity
            item.refunded_amount = item.total_price
            item.total_cost = Decimal('0.00')
        item.save(update_fields=['pieces_per_unit', 'sell_mode', 'list_price', 'total_cost',
                                 'quantity_refunded', 'refunded_amount'])

    # --- Payments: attribute each one to the drawer of its sale ---
    for payment in SalePayment.objects.select_related('sale').iterator():
        payment.register_session_id = payment.sale.register_session_id
        if Decimal(payment.amount) < 0:
            # The old code recorded change as a negative CASH row.
            payment.entry_type = 'CHANGE'
        else:
            payment.entry_type = 'SALE'
        payment.save(update_fields=['register_session', 'entry_type'])

    # --- Invoice roll-ups ---
    for sale in Sale.objects.prefetch_related('items').iterator(chunk_size=200):
        items = list(sale.items.all())
        sale.total_cost = sum((Decimal(i.total_cost or 0) for i in items), Decimal('0.00'))
        sale.refunded_amount = sum((Decimal(i.refunded_amount or 0) for i in items), Decimal('0.00'))
        if not sale.subtotal:
            sale.subtotal = sum((Decimal(i.total_price or 0) for i in items), Decimal('0.00'))
        sale.save(update_fields=['total_cost', 'refunded_amount', 'subtotal'])


def backwards(apps, schema_editor):
    # Nothing to undo -- the columns themselves are dropped by the schema
    # migration this depends on.
    pass


class Migration(migrations.Migration):

    dependencies = [
        ('sales', '0003_saleitembatch_alter_registersession_options_and_more'),
    ]

    operations = [
        migrations.RunPython(forwards, backwards),
    ]
