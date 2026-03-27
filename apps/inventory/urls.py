from django.urls import path
from . import views

app_name = 'inventory'

urlpatterns = [
    # Dashboard
    path('', views.inventory_dashboard, name='dashboard'),
    path('shipments/new/', views.shipment_create, name='shipment_create'),
    path('shipments/', views.shipment_list, name='shipment_list'),
    path('shipments/<int:pk>/', views.shipment_detail, name='shipment_detail'),
    path('shipments/<int:pk>/receive/', views.receive_shipment, name='receive_shipment'),
    path('shipments/import/', views.import_shipment_preview, name='import_shipment'),

    # Operations
    path('receive/', views.receive_stock, name='receive_stock'),
    path('transfer/create/', views.create_transfer, name='create_transfer'),

    path('batch/<int:pk>/', views.batch_detail, name='batch_detail'),

    path('transfers/<int:pk>/view/', views.transfer_detail, name='transfer_detail'),

    # History & Processing
    path('transfers/', views.transfer_list, name='transfer_list'),
    path('transfers/<int:pk>/process/', views.process_transfer, name='process_transfer'),  # Warehouse action
    path('transfers/<int:pk>/receive/', views.receive_transfer, name='receive_transfer'),  # Shop action (New)

    # Adjustments & Alerts
    path('adjustments/', views.stock_adjustments, name='adjustments'),
    path('alerts/', views.expiry_alerts, name='expiry_alerts'),
]