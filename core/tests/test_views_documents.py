"""Tests for project document views."""

from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.uploadedfile import SimpleUploadedFile
from django.test import Client, RequestFactory, TestCase, override_settings
from django.urls import reverse

from core.models import ProjectDocument
from core.tests.mixins import TestDataMixin


class ProjectDocumentViewTests(TestDataMixin, TestCase):
    """Tests for document CRUD views."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")

    def test_create_document(self):
        f = SimpleUploadedFile("test.pdf", b"content", content_type="application/pdf")
        url = reverse("core:document_create", args=[self.project.pk])
        resp = self.client.post(url, {"name": "Test Doc", "file": f})
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.project.documents.count(), 1)

    def test_delete_document(self):
        f = SimpleUploadedFile("test.pdf", b"content")
        doc = ProjectDocument.objects.create(project=self.project, name="Test", file=f)
        url = reverse("core:document_delete", args=[doc.pk])
        resp = self.client.post(url)
        self.assertEqual(resp.status_code, 302)
        self.assertEqual(self.project.documents.count(), 0)

    def test_create_requires_permission(self):
        self.client.login(username="otheruser", password="otherpass123")
        url = reverse("core:document_create", args=[self.project.pk])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 403)


@override_settings(MEDIA_ROOT="/tmp/layernexus_test_media/")  # noqa: S108
class ProjectDocumentDownloadViewTests(TestDataMixin, TestCase):
    """Tests for ProjectDocumentDownloadView."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        f = SimpleUploadedFile("guide.pdf", b"pdf content", content_type="application/pdf")
        self.doc = ProjectDocument.objects.create(
            project=self.project,
            name="Guide",
            file=f,
        )
        self.url = reverse("core:document_download", args=[self.doc.pk])

    def test_download_redirects_anonymous(self):
        """Unauthenticated requests should be redirected to the login page."""
        self.client.logout()
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 302)
        self.assertIn("/accounts/login/", resp["Location"])

    def test_download_returns_200_for_logged_in_user(self):
        """Authenticated users receive a 200 file response."""
        self.client.login(username="testuser", password="testpass123")
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)

    def test_download_content_disposition(self):
        """Response should include Content-Disposition attachment with a .pdf filename."""
        self.client.login(username="testuser", password="testpass123")
        resp = self.client.get(self.url)
        self.assertEqual(resp.status_code, 200)
        content_disposition = resp.get("Content-Disposition", "")
        self.assertIn("attachment", content_disposition)
        # Django may append a suffix to avoid name collisions, so check the extension only
        self.assertIn(".pdf", content_disposition)

    def test_download_404_for_missing_document(self):
        """Requesting a non-existent document pk returns 404."""
        self.client.login(username="testuser", password="testpass123")
        url = reverse("core:document_download", args=[99999])
        resp = self.client.get(url)
        self.assertEqual(resp.status_code, 404)


@override_settings(MEDIA_ROOT="/tmp/layernexus_test_media_sec/")  # noqa: S108
class MediaSecurityHeaderTests(TestDataMixin, TestCase):
    """Potentially-active ``/media/`` uploads must be served safely against stored XSS.

    An uploaded SVG rendered inline in the app origin could execute
    JavaScript.  Such potentially-active media (SVG, PDFs, arbitrary
    uploads — everything except non-scriptable raster images) must
    therefore force a download (``Content-Disposition: attachment``) and
    carry a locked-down ``Content-Security-Policy`` so the browser will
    not run embedded scripts.  Raster images stay inline and are covered
    by ``RasterImageMediaTests``.

    The media view functions are exercised directly (rather than through
    the ``/media/`` URL) because the URLconf captures ``document_root`` at
    import time, so ``override_settings(MEDIA_ROOT=...)`` would not reach
    the wired serve view.
    """

    def setUp(self):
        super().setUp()
        svg = b'<svg xmlns="http://www.w3.org/2000/svg"><script>alert(1)</script></svg>'
        self.doc = ProjectDocument.objects.create(
            project=self.project,
            name="evil",
            file=SimpleUploadedFile("evil.svg", svg, content_type="image/svg+xml"),
        )

    def _request(self):
        request = RequestFactory().get(settings.MEDIA_URL + self.doc.file.name)
        request.user = User.objects.get(username="testuser")
        return request

    def _serve(self, view):
        return view(self._request(), self.doc.file.name, document_root=settings.MEDIA_ROOT)

    def test_authenticated_media_is_attachment(self):
        """The production (auth-enforcing) media view forces download."""
        from layernexus.urls import authenticated_media

        resp = self._serve(authenticated_media)
        self.assertIn("attachment", resp.get("Content-Disposition", ""))

    def test_authenticated_media_sandbox_csp(self):
        from layernexus.urls import authenticated_media

        resp = self._serve(authenticated_media)
        csp = resp.get("Content-Security-Policy", "")
        self.assertIn("sandbox", csp)
        self.assertIn("default-src 'none'", csp)

    def test_authenticated_media_nosniff(self):
        from layernexus.urls import authenticated_media

        resp = self._serve(authenticated_media)
        self.assertEqual(resp.get("X-Content-Type-Options", ""), "nosniff")

    def test_debug_serve_media_hardens(self):
        """The DEBUG serve path (no auth) still hardens the response."""
        from layernexus.urls import serve_media

        resp = self._serve(serve_media)
        self.assertIn("attachment", resp.get("Content-Disposition", ""))
        self.assertIn("sandbox", resp.get("Content-Security-Policy", ""))


@override_settings(MEDIA_ROOT="/tmp/layernexus_test_media_img/")  # noqa: S108
class RasterImageMediaTests(TestDataMixin, TestCase):
    """Raster images must stay viewable inline.

    Project cover images are ``ImageField`` uploads (raster only — Pillow
    rejects SVG) rendered via ``<img>`` and opened via ``<a target=_blank>``.
    They cannot execute scripts, so forcing ``attachment`` on them would be a
    UX regression with no security benefit.  They are served inline (with
    ``nosniff``); only potentially-active content (SVG, PDF, other uploads) is
    forced to download.
    """

    def setUp(self):
        super().setUp()
        # 1x1 transparent PNG.
        png = bytes.fromhex(
            "89504e470d0a1a0a0000000d49484452000000010000000108060000001f15c4"
            "890000000a49444154789c6360000002000154a24f8b0000000049454e44ae426082"
        )
        self.doc = ProjectDocument.objects.create(
            project=self.project,
            name="cover",
            file=SimpleUploadedFile("cover.png", png, content_type="image/png"),
        )

    def _serve(self):
        from layernexus.urls import serve_media

        request = RequestFactory().get(settings.MEDIA_URL + self.doc.file.name)
        request.user = User.objects.get(username="testuser")
        return serve_media(request, self.doc.file.name, document_root=settings.MEDIA_ROOT)

    def test_png_served_inline(self):
        resp = self._serve()
        self.assertNotIn("attachment", resp.get("Content-Disposition", ""))

    def test_png_csp_blocks_scripts_but_not_rendering(self):
        # No sandbox/attachment (would break inline <img> and the open-in-new-tab
        # link), but script-src 'none' neutralises any disguised active content
        # (belt-and-suspenders with nosniff), without restricting image loads.
        csp = self._serve().get("Content-Security-Policy", "")
        self.assertNotIn("sandbox", csp)
        self.assertIn("script-src 'none'", csp)

    def test_png_still_has_nosniff(self):
        self.assertEqual(self._serve().get("X-Content-Type-Options", ""), "nosniff")


class GlobalSecurityHeaderTests(TestDataMixin, TestCase):
    """Regular app pages carry a conservative baseline CSP (defense in depth)."""

    def setUp(self):
        super().setUp()
        self.client = Client()
        self.client.login(username="testuser", password="testpass123")

    def test_app_page_has_baseline_csp(self):
        resp = self.client.get(reverse("core:dashboard"))
        self.assertEqual(resp.status_code, 200)
        csp = resp.get("Content-Security-Policy", "")
        self.assertIn("default-src 'self'", csp)
        # Bootstrap + three.js are loaded from jsDelivr, so the CSP must allow it.
        self.assertIn("https://cdn.jsdelivr.net", csp)
        self.assertIn("object-src 'none'", csp)
        self.assertIn("frame-ancestors 'none'", csp)


class SecurityHeadersMiddlewareTests(TestCase):
    """The baseline-CSP middleware sets a CSP but never clobbers a stricter one."""

    def test_sets_csp_when_absent(self):
        from django.http import HttpResponse

        from layernexus.settings import SecurityHeadersMiddleware

        middleware = SecurityHeadersMiddleware(lambda request: HttpResponse("ok"))
        resp = middleware(RequestFactory().get("/"))
        self.assertIn("default-src 'self'", resp["Content-Security-Policy"])

    def test_preserves_existing_media_csp(self):
        """A media response's strict sandbox CSP must survive the middleware."""
        from django.http import HttpResponse

        from layernexus.settings import SecurityHeadersMiddleware

        def get_response(request):
            resp = HttpResponse("file")
            resp["Content-Security-Policy"] = "sandbox; default-src 'none'"
            return resp

        middleware = SecurityHeadersMiddleware(get_response)
        resp = middleware(RequestFactory().get(settings.MEDIA_URL + "x.svg"))
        self.assertEqual(resp["Content-Security-Policy"], "sandbox; default-src 'none'")
