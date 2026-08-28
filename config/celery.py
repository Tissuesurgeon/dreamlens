from celery import Celery

from django_settings_boot import configure_django_settings

configure_django_settings()

app = Celery("dreamlens")
app.config_from_object("django.conf:settings", namespace="CELERY")
app.autodiscover_tasks()
