from django.contrib import admin
from django.http import HttpResponse
from django.urls import include, path


def healthz(_request):
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("healthz", healthz),
    path("health", healthz),
    path("", include("apps.core.urls")),
    path("api/", include("apps.core.api_urls")),
    path("admin/", admin.site.urls),
]
