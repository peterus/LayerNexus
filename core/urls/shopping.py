"""Shopping list URL patterns."""

from django.urls import path

from core.views import (
    ProjectShoppingCartOrderedView,
    ProjectShoppingItemSetView,
    ProjectShoppingListView,
    ProjectShoppingResetView,
)

urlpatterns = [
    path(
        "projects/<int:pk>/shopping-list/",
        ProjectShoppingListView.as_view(),
        name="project_shopping_list",
    ),
    path(
        "projects/<int:pk>/shopping-list/cart-ordered/",
        ProjectShoppingCartOrderedView.as_view(),
        name="project_shopping_cart_ordered",
    ),
    path(
        "projects/<int:pk>/shopping-list/reset/",
        ProjectShoppingResetView.as_view(),
        name="project_shopping_reset",
    ),
    path(
        "projects/<int:pk>/shopping-list/<int:hardware_part_pk>/",
        ProjectShoppingItemSetView.as_view(),
        name="project_shopping_item_set",
    ),
]
