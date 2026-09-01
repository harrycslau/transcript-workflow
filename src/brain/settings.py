"""Django settings for the Brain project.

All storage paths and the database location come from the shared
brainlib configuration loader (``config/config.yaml`` or ``BRAIN_CONFIG``).
A missing or invalid configuration raises ImproperlyConfigured at boot;
``brain serve`` pre-validates and reports it concisely.
"""

from pathlib import Path

import django
from django.core.exceptions import ImproperlyConfigured

from brainlib import __version__
from brainlib.config import load_config
from brainlib.paths import ensure_runtime_dirs

try:
    CONFIG = load_config()
    ensure_runtime_dirs(CONFIG)
except Exception as exc:  # ConfigError or OSError creating dirs
    if isinstance(exc, ImproperlyConfigured):
        raise
    raise ImproperlyConfigured(f"brain: invalid configuration: {exc}") from exc

BASE_DIR = Path(__file__).resolve().parent.parent

SECRET_KEY = __import__("os").environ.get("BRAIN_DJANGO_SECRET_KEY", "django-insecure-local-only-dev-key")

# Local-only by default; the app is a personal local tool.
DEBUG = True
ALLOWED_HOSTS = ["localhost", "127.0.0.1"]

INSTALLED_APPS = [
    "django.contrib.staticfiles",
    "django.contrib.messages",
    "workflow",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
    "workflow.middleware.ContentSecurityPolicyMiddleware",
]

# Messages use signed cookies: no session table, no session cookies.
# CookieStorage derives its cookie security flags from the
# SESSION_COOKIE_* settings below (HttpOnly, SameSite=Lax; Secure is
# off because the app serves plain local HTTP on 127.0.0.1).
MESSAGE_STORAGE = "django.contrib.messages.storage.cookie.CookieStorage"
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_COOKIE_SECURE = False
SESSION_COOKIE_NAME = "brain_session"

X_FRAME_OPTIONS = "DENY"
SECURE_CONTENT_TYPE_NOSNIFF = True
SECURE_REFERRER_POLICY = "same-origin"

ROOT_URLCONF = "brain.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.messages.context_processors.messages",
            ],
        },
    },
]

WSGI_APPLICATION = "brain.wsgi.application"

DATABASES = {
    "default": {
        "ENGINE": "django.db.backends.sqlite3",
        "NAME": str(CONFIG.storage.database),
    }
}

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

BRAIN_VERSION = __version__
BRAIN_CONFIG_OBJ = CONFIG
