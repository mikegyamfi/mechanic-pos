from .base import *

DEBUG = True

ALLOWED_HOSTS = ['localhost', '127.0.0.1', 'abosseyokai-hub-a4e53ad7e19b.herokuapp.com']

# Database for development
# DATABASES = {
#     'default': {
#         'ENGINE': 'django.db.backends.sqlite3',
#         'NAME': BASE_DIR / 'db.sqlite3',
#     }
# }

import dj_database_url
DATABASES = {
    'default': dj_database_url.config(
        default=os.environ.get('DATABASE_URL')
    )
}

# Email Backend for development (prints to console)
EMAIL_BACKEND = 'django.core.mail.backends.console.EmailBackend'

# Static files for local development
STATICFILES_DIRS = [BASE_DIR / 'static']

# Set Dummy keys for SMS/External APIs during local dev
SMS_API_KEY = "local_dev_dummy_key"

