from django.urls import path
from . import views

app_name = 'products'

urlpatterns = [
    path('', views.product_list, name='product_list'),
    path('add/', views.product_create, name='product_create'),
    path('view/<int:pk>/', views.product_detail, name='product_detail'), # New
    path('edit/<int:pk>/', views.product_edit, name='product_edit'),
    path('price-suggestions/', views.price_suggestions, name='price_suggestions'),

    # Promotions: the only authorised route below the margin floor
    path('promotions/', views.promotion_list, name='promotion_list'),
    path('promotions/new/', views.promotion_form, name='promotion_create'),
    path('promotions/<int:pk>/', views.promotion_form, name='promotion_edit'),
    path('promotions/<int:pk>/toggle/', views.promotion_toggle, name='promotion_toggle'),
    path('category/add/', views.quick_category_create, name='quick_category_create'),
]