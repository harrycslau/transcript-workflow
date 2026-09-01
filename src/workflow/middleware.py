"""Local security middleware: a strict, self-hosted Content-Security-Policy.

Everything is served from this app (styles and scripts are static
files); no CDN, inline script, or external origin is used, so the
policy can be deny-by-default.
"""

from __future__ import annotations

CSP = (
    "default-src 'self'; "
    "style-src 'self'; "
    "script-src 'self'; "
    "img-src 'self'; "
    "font-src 'self'; "
    "connect-src 'self'; "
    "form-action 'self'; "
    "frame-ancestors 'none'; "
    "base-uri 'none'"
)


class ContentSecurityPolicyMiddleware:
    def __init__(self, get_response):
        self.get_response = get_response

    def __call__(self, request):
        response = self.get_response(request)
        response.headers.setdefault("Content-Security-Policy", CSP)
        return response
