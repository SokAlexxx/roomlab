import os
import sys

SECRET_KEY = os.environ["DJANGO_SECRET_KEY"]
ALLOWED_HOSTS = ["localhost", "127.0.0.1", "testserver"]
ROOT_URLCONF = "config.urls"
INSTALLED_APPS = ["django.contrib.auth", "django.contrib.contenttypes", "django.contrib.postgres", "booking"]
DATABASES = {"default": {"ENGINE": "django.db.backends.postgresql", "HOST": os.environ.get("DB_HOST", "db"),
                         "NAME": "roomlab", "USER": "roomlab", "PASSWORD": os.environ["PG_PASS"]}}
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
USE_TZ = True
TIME_ZONE = "UTC"
CACHES = {"default": {"BACKEND": "django.core.cache.backends.locmem.LocMemCache", "LOCATION": "roomlab"}}
if len(sys.argv) > 1 and sys.argv[1] == "test":
    # Fast hashing for the test suite only; the running service keeps Django's default PBKDF2.
    PASSWORD_HASHERS = ["django.contrib.auth.hashers.MD5PasswordHasher"]
MIDDLEWARE = []
