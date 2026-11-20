from django.urls import path

from booking import rooms

urlpatterns = [path("rooms/available/", rooms.available, name="rooms-available")]
