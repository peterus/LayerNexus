"""Tests for the project shopping list views."""

from decimal import Decimal

from django.conf import settings
from django.db import connection
from django.test.utils import CaptureQueriesContext
from django.urls import reverse

from core.models import HardwarePart, HardwareShoppingItem, Project, ProjectHardware
from core.tests.mixins import _RBACTestBase


class ShoppingViewTestBase(_RBACTestBase):
    """Project with two required hardware parts."""

    def setUp(self) -> None:
        super().setUp()
        self.m3 = HardwarePart.objects.create(name="M3 Screw", category=HardwarePart.CATEGORY_SCREWS)
        self.bearing = HardwarePart.objects.create(name="Bearing 608", category=HardwarePart.CATEGORY_BEARINGS)
        ProjectHardware.objects.create(project=self.project, hardware_part=self.m3, quantity=4)
        ProjectHardware.objects.create(project=self.project, hardware_part=self.bearing, quantity=2)
        self.list_url = reverse("core:project_shopping_list", args=[self.project.pk])
        self.cart_url = reverse("core:project_shopping_cart_ordered", args=[self.project.pk])
        self.reset_url = reverse("core:project_shopping_reset", args=[self.project.pk])

    def set_url(self, part: HardwarePart) -> str:
        return reverse("core:project_shopping_item_set", args=[self.project.pk, part.pk])

    def login(self, user) -> None:
        self.client.login(username=user.username, password="testpass123")

    def status(self, part: HardwarePart) -> str:
        return next(r for r in HardwareShoppingItem.build_list(self.project) if r.hardware_part.pk == part.pk).status


class ShoppingListViewTests(ShoppingViewTestBase):
    def _list_query_count(self) -> int:
        """Return the number of queries needed to render the list page."""
        with CaptureQueriesContext(connection) as ctx:
            self.assertEqual(self.client.get(self.list_url).status_code, 200)
        return len(ctx)

    def test_list_query_count_grows_at_most_one_per_sub_project(self) -> None:
        self.login(self.operator_user)

        def add_child(name: str) -> None:
            child = Project.objects.create(parent=self.project, quantity=1, name=name)
            ProjectHardware.objects.create(project=child, hardware_part=self.m3, quantity=1)

        add_child("Child 1")
        self._list_query_count()  # warm up caches (content types, permissions)
        one_child = self._list_query_count()
        add_child("Child 2")
        add_child("Child 3")
        # Only the per-node hardware_assignments lookup remains (one query per sub-project);
        # edge/child traversal comes from the prefetch cache.
        self.assertLessEqual(self._list_query_count(), one_child + 2)

    def test_list_renders_for_operator_without_controls(self) -> None:
        self.login(self.operator_user)
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 200)
        self.assertContains(response, "M3 Screw")
        self.assertContains(response, "Bearing 608")
        self.assertNotContains(response, self.set_url(self.m3))
        self.assertNotContains(response, "Reset all")

    def test_list_renders_controls_for_designer(self) -> None:
        self.login(self.designer_user)
        response = self.client.get(self.list_url)
        self.assertContains(response, f'action="{self.set_url(self.m3)}"')
        self.assertContains(response, "Mark cart as ordered")
        self.assertContains(response, "Reset all")

    def test_list_shows_used_in_line(self) -> None:
        child = Project.objects.create(parent=self.project, quantity=2, name="Child Module", created_by=self.admin_user)
        ProjectHardware.objects.create(project=child, hardware_part=self.m3, quantity=1)
        self.login(self.operator_user)
        response = self.client.get(self.list_url)
        self.assertContains(response, f"Used in: Child Module, {self.project.name}")
        self.assertNotContains(response, "bi-info-circle")

    def test_list_used_in_root_only_and_escaped(self) -> None:
        child = Project.objects.create(parent=self.project, quantity=1, name="<b>X</b>", created_by=self.admin_user)
        ProjectHardware.objects.create(project=child, hardware_part=self.m3, quantity=1)
        self.login(self.designer_user)
        response = self.client.get(self.list_url)
        self.assertContains(response, f"Used in: {self.project.name}</div>")
        self.assertContains(response, "&lt;b&gt;X&lt;/b&gt;")
        self.assertNotContains(response, "<b>X</b>")

    def test_list_requires_login(self) -> None:
        response = self.client.get(self.list_url)
        self.assertEqual(response.status_code, 302)
        self.assertIn(settings.LOGIN_URL, response["Location"])

    def test_list_shows_no_prices(self) -> None:
        self.m3.unit_price = Decimal("9.99")
        self.m3.save()
        self.login(self.designer_user)
        response = self.client.get(self.list_url)
        self.assertNotContains(response, "9.99")
        self.assertNotContains(response, "€")

    def test_list_shows_no_link_badge_and_shop_link(self) -> None:
        self.m3.url = "https://shop.example.com/m3"
        self.m3.save()
        self.login(self.designer_user)
        response = self.client.get(self.list_url)
        self.assertContains(response, 'href="https://shop.example.com/m3"')
        self.assertContains(response, 'target="_blank"')
        self.assertContains(response, "no link")

    def test_list_shows_quantity_changed_hint(self) -> None:
        HardwareShoppingItem.objects.create(
            project=self.project,
            hardware_part=self.m3,
            status=HardwareShoppingItem.STATUS_ORDERED,
            quantity=8,
        )
        self.login(self.designer_user)
        response = self.client.get(self.list_url)
        self.assertContains(response, "Quantity changed (was 8)")

    def test_empty_list_shows_empty_state(self) -> None:
        empty = Project.objects.create(name="Empty", created_by=self.admin_user)
        self.login(self.designer_user)
        response = self.client.get(reverse("core:project_shopping_list", args=[empty.pk]))
        self.assertContains(response, "No hardware")
        self.assertContains(response, reverse("core:project_detail", args=[empty.pk]))


class ShoppingWriteViewTests(ShoppingViewTestBase):
    def test_set_status_designer_redirects_with_anchor(self) -> None:
        self.login(self.designer_user)
        response = self.client.post(self.set_url(self.m3), {"status": "in_cart"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].endswith(f"#hw-{self.m3.pk}"))
        self.assertEqual(self.status(self.m3), "in_cart")

    def test_write_endpoints_forbidden_for_operator(self) -> None:
        self.login(self.operator_user)
        for url, data in (
            (self.set_url(self.m3), {"status": "in_cart"}),
            (self.cart_url, {}),
            (self.reset_url, {}),
        ):
            self.assertEqual(self.client.post(url, data).status_code, 403)
        self.assertFalse(HardwareShoppingItem.objects.exists())

    def test_write_endpoints_forbidden_for_anonymous(self) -> None:
        for url, data in (
            (self.set_url(self.m3), {"status": "in_cart"}),
            (self.cart_url, {}),
            (self.reset_url, {}),
        ):
            self.assertEqual(self.client.post(url, data).status_code, 403)
        self.assertFalse(HardwareShoppingItem.objects.exists())

    def test_list_renders_state_after_actions(self) -> None:
        self.login(self.designer_user)
        response = self.client.get(self.list_url)
        self.assertContains(response, "2 open · 0 in cart · 0 ordered")
        self.assertContains(response, 'data-bs-target="#cartOrderedModal" disabled')
        self.client.post(self.set_url(self.m3), {"status": "in_cart"})
        response = self.client.get(self.list_url)
        self.assertContains(response, "1 open · 1 in cart · 0 ordered")
        self.assertNotContains(response, 'data-bs-target="#cartOrderedModal" disabled')
        self.assertContains(response, 'name="status" value="ordered"')
        self.assertContains(response, 'name="status" value="in_cart"')

    def test_stale_status_post_is_rejected_without_write(self) -> None:
        self.login(self.designer_user)
        response = self.client.post(self.set_url(self.m3), {"status": "ordered", "quantity": "3"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].endswith(f"#hw-{self.m3.pk}"))
        self.assertFalse(HardwareShoppingItem.objects.exists())
        page = self.client.get(response["Location"].split("#")[0])
        self.assertContains(page, "Nothing was changed")
        response = self.client.post(self.set_url(self.m3), {"status": "ordered", "quantity": "4"})
        self.assertEqual(response.status_code, 302)
        self.assertTrue(response["Location"].endswith(f"#hw-{self.m3.pk}"))
        self.assertEqual(self.status(self.m3), "ordered")

    def test_set_status_non_numeric_quantity_returns_400(self) -> None:
        self.login(self.designer_user)
        response = self.client.post(self.set_url(self.m3), {"status": "in_cart", "quantity": "x"})
        self.assertEqual(response.status_code, 400)
        self.assertEqual(response.content, b"Invalid quantity.")
        self.assertFalse(HardwareShoppingItem.objects.exists())

    def test_list_renders_ordered_styling_and_action_labels(self) -> None:
        HardwareShoppingItem.set_status(self.project, self.m3.pk, HardwareShoppingItem.STATUS_IN_CART)
        HardwareShoppingItem.set_status(self.project, self.bearing.pk, HardwareShoppingItem.STATUS_ORDERED)
        self.login(self.designer_user)
        response = self.client.get(self.list_url)
        self.assertContains(response, 'aria-label="Mark as ordered"')
        self.assertContains(response, 'aria-label="Mark as open"')
        self.assertContains(response, f'text-body-secondary" id="hw-{self.bearing.pk}"')
        self.assertNotContains(response, f'text-body-secondary" id="hw-{self.m3.pk}"')
        self.assertContains(response, 'name="quantity" value="4"')

    def test_write_endpoints_reject_get(self) -> None:
        self.login(self.admin_user)
        for url in (self.set_url(self.m3), self.cart_url, self.reset_url):
            self.assertEqual(self.client.get(url).status_code, 405)

    def test_set_status_part_not_required_returns_404(self) -> None:
        other = HardwarePart.objects.create(name="Unused")
        self.login(self.designer_user)
        self.assertEqual(self.client.post(self.set_url(other), {"status": "in_cart"}).status_code, 404)

    def test_set_status_invalid_value_returns_400(self) -> None:
        self.login(self.designer_user)
        self.assertEqual(self.client.post(self.set_url(self.m3), {"status": "bogus"}).status_code, 400)
        self.assertEqual(self.client.post(self.set_url(self.m3), {}).status_code, 400)

    def test_cart_ordered_and_reset_via_views(self) -> None:
        self.login(self.designer_user)
        self.client.post(self.set_url(self.m3), {"status": "in_cart"})
        self.client.post(self.set_url(self.bearing), {"status": "ordered"})
        self.assertEqual(self.client.post(self.cart_url).status_code, 302)
        self.assertEqual(self.status(self.m3), "ordered")
        self.assertEqual(self.status(self.bearing), "ordered")
        self.assertEqual(self.client.post(self.reset_url).status_code, 302)
        self.assertEqual(self.status(self.m3), "open")
        self.assertEqual(self.status(self.bearing), "open")

    def test_bulk_actions_on_empty_list_are_noops(self) -> None:
        empty = Project.objects.create(name="Empty", created_by=self.admin_user)
        self.login(self.designer_user)
        for name in ("project_shopping_cart_ordered", "project_shopping_reset"):
            response = self.client.post(reverse(f"core:{name}", args=[empty.pk]))
            self.assertEqual(response.status_code, 302)


class ShoppingSummaryTests(ShoppingViewTestBase):
    """Shopping-list summary on the project detail and cost pages."""

    def test_project_detail_shows_shopping_summary(self) -> None:
        HardwareShoppingItem.set_status(self.project, self.m3.pk, HardwareShoppingItem.STATUS_IN_CART)
        self.login(self.designer_user)
        response = self.client.get(reverse("core:project_detail", args=[self.project.pk]))
        self.assertContains(response, self.list_url)
        self.assertContains(response, "1 in cart")
        self.assertContains(response, "1 open")

    def test_project_detail_drops_aggregated_hardware_block(self) -> None:
        child = Project.objects.create(name="Child Module", parent=self.project)
        ProjectHardware.objects.create(project=child, hardware_part=self.m3, quantity=1)
        self.login(self.designer_user)
        response = self.client.get(reverse("core:project_detail", args=[self.project.pk]))
        self.assertNotContains(response, "Aggregated Hardware (incl. Sub-Projects)")

    def test_project_detail_summary_for_child_only_hardware(self) -> None:
        ProjectHardware.objects.filter(project=self.project).delete()
        child = Project.objects.create(parent=self.project, quantity=1, name="Child Module", created_by=self.admin_user)
        ProjectHardware.objects.create(project=child, hardware_part=self.m3, quantity=1)
        self.login(self.designer_user)
        response = self.client.get(reverse("core:project_detail", args=[self.project.pk]))
        self.assertContains(response, self.list_url)
        self.assertNotContains(response, "No hardware items yet.")

    def test_project_detail_no_summary_without_hardware(self) -> None:
        ProjectHardware.objects.filter(project=self.project).delete()
        self.login(self.designer_user)
        response = self.client.get(reverse("core:project_detail", args=[self.project.pk]))
        self.assertNotContains(response, self.list_url)
        self.assertContains(response, "No hardware items yet.")

    def test_cost_page_links_shopping_list(self) -> None:
        self.login(self.designer_user)
        response = self.client.get(reverse("core:project_cost", args=[self.project.pk]))
        self.assertContains(response, self.list_url)
