"""ASGI config for the Brain project."""

import os

from django.core.asgi import get_asgi_application

os.environ.setdefault("DJANGO_SETTINGS_MODULE", "brain.settings")

application = get_asgi_application()
