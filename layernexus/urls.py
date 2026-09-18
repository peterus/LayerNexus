"""
URL configuration for layernexus project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/6.0/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""

import re
from pathlib import PurePosixPath

from django.conf import settings
from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.urls import include, path, re_path
from django.views.static import serve

#: CSP applied to every potentially-active uploaded-media response.
#: ``sandbox`` disables scripts, plugins, forms and same-origin privileges
#: for the resource, so a malicious uploaded SVG/HTML cannot run JavaScript
#: in our origin.
MEDIA_CSP = "sandbox; default-src 'none'"

#: Raster image extensions that cannot execute scripts.  These are served
#: inline (project cover images are ``ImageField`` uploads rendered via
#: ``<img>`` and opened directly), so forcing ``attachment`` on them would
#: be a UX regression with no security benefit.  Everything else — notably
#: ``.svg`` (scriptable), PDFs and arbitrary uploads — is downloaded and
#: sandboxed.  Pillow's ``ImageField`` validation rejects SVG, so covers are
#: always raster.
INLINE_IMAGE_EXTENSIONS = frozenset({".png", ".jpg", ".jpeg", ".gif", ".webp", ".bmp", ".ico", ".tif", ".tiff"})


def _harden_media_response(response, path):
    """Neutralise stored-XSS vectors in user-uploaded media.

    Uploaded files are attacker-controlled.  Serving an active type (e.g.
    ``.svg``) inline in the application origin is a stored-XSS vector, and
    ``SECURE_CONTENT_TYPE_NOSNIFF`` does not help for a correctly typed
    ``image/svg+xml`` document.  Such responses are forced to ``attachment``
    with a sandbox CSP so the browser never renders/executes them as active
    content.  Non-scriptable raster images are left inline so cover images
    keep rendering; ``nosniff`` still guards against MIME confusion.
    """
    response["X-Content-Type-Options"] = "nosniff"
    if PurePosixPath(path).suffix.lower() in INLINE_IMAGE_EXTENSIONS:
        return response
    response["Content-Disposition"] = "attachment"
    response["Content-Security-Policy"] = MEDIA_CSP
    return response


def serve_media(request, path, document_root=None):
    """Serve a media file, hardening potentially-active content against XSS."""
    response = serve(request, path, document_root=document_root)
    return _harden_media_response(response, path)


@login_required
def authenticated_media(request, path, document_root=None):
    """Serve media files only to authenticated users, hardened against XSS."""
    return serve_media(request, path, document_root=document_root)


def health_check(request):
    """Return a simple health status for Docker HEALTHCHECK."""
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("health/", health_check, name="health_check"),
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("core.urls")),
]

# Serve user-uploaded media (STL, gcode).  LayerNexus is a self-hosted
# application — Django's static() helper is a no-op when DEBUG=False,
# so we register the pattern unconditionally.
# In production, media is served only to authenticated users.
# NOTE: For high-traffic or large-file scenarios, consider fronting
# this with Nginx + X-Accel-Redirect for better performance.
_media_view = serve_media if settings.DEBUG else authenticated_media
urlpatterns += [
    re_path(
        rf"^{re.escape(settings.MEDIA_URL.lstrip('/'))}(?P<path>.*)$",
        _media_view,
        {"document_root": settings.MEDIA_ROOT},
    ),
]
