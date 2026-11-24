from django.urls import path

from . import views

app_name = "trovi_launcher"

urlpatterns = [
    path("launches/<uuid:launch_id>/events/", views.events, name="events"),
]
