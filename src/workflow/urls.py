"""Workflow app URL routes."""

from django.urls import path

from workflow import views

urlpatterns = [
    path("", views.home, name="home"),
    path("health/", views.health, name="health"),
]
