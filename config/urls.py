import time
from django.db import connection, DatabaseError
from django.http import JsonResponse
from django.urls import include, path


def health(request):
    started = time.monotonic()
    try:
        with connection.cursor() as cursor:
            cursor.execute("SELECT 1")
        status = 200
    except DatabaseError:
        status = 503
    return JsonResponse({"status": status, "elapsed_ms": round((time.monotonic()-started)*1000)}, status=status)


urlpatterns = [path("health/", health), path("api/", include("booking.rooms_urls")),
               path("api/", include("booking.flow_urls"))]
