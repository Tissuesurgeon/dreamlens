"""Celery is imported lazily so Gunicorn does not touch the broker at web boot."""


def __getattr__(name: str):
    if name in {"celery_app", "app", "celery"}:
        from config.celery import app

        return app
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = ("celery_app",)
