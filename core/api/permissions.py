"""DRF permission classes mapping the UI's RBAC onto the build API."""

from __future__ import annotations

from rest_framework.permissions import SAFE_METHODS, BasePermission
from rest_framework.request import Request
from rest_framework.views import APIView


class ReadOrProjectManage(BasePermission):
    """Read for any authenticated user; writes require ``core.can_manage_projects``.

    This mirrors the UI's ``ProjectManageMixin`` so the token's user carries the exact
    same authority through the API as it does through the web interface. Safe methods
    (GET/HEAD/OPTIONS) only require authentication; all mutating methods require the
    ``core.can_manage_projects`` permission (held by the ``Admin`` and ``Designer``
    groups).
    """

    def has_permission(self, request: Request, view: APIView) -> bool:
        """Return whether ``request`` may perform its method on ``view``."""
        if request.method in SAFE_METHODS:
            return bool(request.user and request.user.is_authenticated)
        return bool(request.user and request.user.has_perm("core.can_manage_projects"))
