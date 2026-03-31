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

from django.conf import settings
from django.contrib import admin
from django.contrib.auth.decorators import login_required
from django.http import JsonResponse
from django.urls import include, path, re_path
from django.views.static import serve


@login_required
def authenticated_media(request, path, document_root=None):
    """Serve media files only to authenticated users."""
    return serve(request, path, document_root=document_root)


def health_check(request):
    """Return a simple health status for Docker HEALTHCHECK."""
    return JsonResponse({"status": "ok"})


urlpatterns = [
    path("health/", health_check, name="health_check"),
    path("admin/", admin.site.urls),
    path("accounts/", include("django.contrib.auth.urls")),
    path("", include("core.urls")),
]

# Always serve user-uploaded media (STL, gcode).  LayerNexus is a
# self-hosted application — Django's static() helper is a no-op when
# DEBUG=False, so we register the pattern unconditionally.
# In production, media is served only to authenticated users.
_media_view = serve if settings.DEBUG else authenticated_media
urlpatterns += [
    re_path(
        rf"^{re.escape(settings.MEDIA_URL.lstrip('/'))}(?P<path>.*)$",
        _media_view,
        {"document_root": settings.MEDIA_ROOT},
    ),
]
