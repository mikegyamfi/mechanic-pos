from django.contrib import admin
from django.urls import path, include
from django.conf import settings
from django.conf.urls.static import static

urlpatterns = [
    path('admin/', admin.site.urls),
    
    # Dashboard / Core
    path('', include('apps.dashboard.urls')),

    # Auth & Users
    path('users/', include('apps.users.urls')),
    
    # Business Modules
    path('products/', include('apps.products.urls')),
    path('inventory/', include('apps.inventory.urls')),
    path('sales/', include('apps.sales.urls')),
    path('customers/', include('apps.customers.urls')),
    path('finance/', include('apps.finance.urls')),
    path('notifications/', include('apps.notifications.urls')),
]

# Serve static and media files during local development
if settings.DEBUG:
    urlpatterns += static(settings.STATIC_URL, document_root=settings.STATIC_ROOT)
    urlpatterns += static(settings.MEDIA_URL, document_root=settings.MEDIA_ROOT)


