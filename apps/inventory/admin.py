from django.contrib import admin
from .models import (
    Supplier, StockBatch, StockTransfer,
    StockTransferItem, StockAdjustment,
    Shipment, ShipmentItem
)


@admin.register(Supplier)
class SupplierAdmin(admin.ModelAdmin):
    list_display = ('name', 'contact_person', 'phone', 'email')
    search_fields = ('name', 'contact_person')


@admin.register(StockBatch)
class StockBatchAdmin(admin.ModelAdmin):
    list_display = ('product', 'location', 'quantity', 'expiry_date', 'batch_number', 'cost_price')
    list_filter = ('location', 'expiry_date', 'supplier')
    search_fields = ('product__name', 'batch_number')
    date_hierarchy = 'received_date'


class StockTransferItemInline(admin.TabularInline):
    model = StockTransferItem
    extra = 0


@admin.register(StockTransfer)
class StockTransferAdmin(admin.ModelAdmin):
    inlines = [StockTransferItemInline]
    list_display = ('reference_number', 'source_location', 'destination_location', 'status', 'created_at')
    list_filter = ('status', 'source_location', 'destination_location')
    search_fields = ('reference_number',)


@admin.register(StockAdjustment)
class StockAdjustmentAdmin(admin.ModelAdmin):
    list_display = ('batch', 'location', 'reason', 'adjusted_quantity', 'performed_by', 'created_at')
    list_filter = ('reason', 'location')


# ==============================================================================
# IMPORT & SHIPMENT ADMIN (PHASE 1)
# ==============================================================================

class ShipmentItemInline(admin.TabularInline):
    model = ShipmentItem
    extra = 0
    # Updated to match the new properties from the Pairs/Pieces math update
    readonly_fields = (
        'total_landed_cost_ghs',
        'unit_landed_cost_ghs',
        'effective_cost_for_margin',
        'projected_unit_profit_ghs',
        'profit_margin_percentage'
    )


@admin.register(Shipment)
class ShipmentAdmin(admin.ModelAdmin):
    inlines = [ShipmentItemInline]
    list_display = ('reference_number', 'supplier_name', 'date_shipped', 'total_freight_usd', 'exchange_rate', 'status',
                    'created_at')
    list_filter = ('status', 'date_shipped')
    search_fields = ('reference_number', 'supplier_name')
