import os
from django.core.wsgi import get_wsgi_application

# Check if the server has a variable called "ENV" set to "production"
if os.environ.get('ENV') == 'production':
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'cortex_pos.settings.production')
else:
    os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'cortex_pos.settings.local')

application = get_wsgi_application()


