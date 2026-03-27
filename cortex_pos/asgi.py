import os
from django.core.asgi import get_asgi_application

if os.environ.get('ENV') == 'production':
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'retail_pos.settings.production')
else:
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'retail_pos.settings.local')

application = get_asgi_application()

