from django.urls import path
from . import views

app_name = 'sales'

urlpatterns = [
    # Main Point of Sale
    path('pos/', views.pos_view, name='pos'),
    path('process/', views.process_sale, name='process_sale'),
    path('register/close/', views.close_register_view, name='close_register'),
    path('returns/', views.refund_list, name='returns'),

    # POS server-side data endpoints
    path('api/products/', views.api_products, name='api_products'),
    path('api/products/<int:pk>/batches/', views.api_product_batches, name='api_product_batches'),
    path('api/quote/', views.api_quote_cart, name='api_quote'),
    path('api/receipt/<int:pk>/', views.receipt_html, name='api_receipt'),

    path('payment/add/<int:pk>/', views.add_payment, name='add_payment'),
    # Returns & Deliveries
    path('refund/<int:pk>/', views.process_refund, name='refund'),
    path('deliveries/', views.delivery_management, name='deliveries'),
    path('deliveries/<int:pk>/', views.delivery_management, name='delivery_edit'),

    # History & Management
    path('history/', views.sale_list, name='list'),
    path('receipt/<int:pk>/', views.sale_detail, name='detail'),
    path('sessions/', views.session_list, name='sessions'),  # Cashier Shifts
    path('sessions/<int:pk>/', views.session_detail, name='session_detail'),
]
