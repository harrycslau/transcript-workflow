"""URL configuration for the Brain Django project."""

from django.urls import include, path

urlpatterns = [
    path("", include("workflow.urls")),
]

handler404 = "workflow.views.error_404"
handler500 = "workflow.views.error_500"
