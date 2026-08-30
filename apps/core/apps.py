from django.apps import AppConfig


class CoreConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.core"
    label = "core"

    def ready(self):
        from apps.core.copy_runtime import start_copy_listener
        from apps.core.telegram_runtime import start_telegram_listener

        start_telegram_listener()
        start_copy_listener()
