"""
Base Django settings for DreamLens.
"""

from pathlib import Path

import environ

BASE_DIR = Path(__file__).resolve().parent.parent.parent

env = environ.Env(
    DEBUG=(bool, False),
    ALLOWED_HOSTS=(list, []),
    MOCK_DREAMDEX=(bool, False),
    DREAMDEX_EVENT_SYNC_INTERVAL=(int, 60),
    DREAMDEX_COLLATERAL_DECIMALS=(int, 6),
    DREAMDEX_TICK=(int, 1000),
    DREAMDEX_LOT=(int, 1000),
)

environ.Env.read_env(BASE_DIR / ".env")

SECRET_KEY = env("SECRET_KEY", default="django-insecure-change-me-in-production")
DEBUG = env("DEBUG")
ALLOWED_HOSTS = env("ALLOWED_HOSTS")

DJANGO_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
]

THIRD_PARTY_APPS = [
    "rest_framework",
]

LOCAL_APPS = [
    "apps.accounts",
    "apps.events",
    "apps.markets",
    "apps.trading",
    "apps.agents",
    "apps.dreamcopy",
    "apps.portfolio",
    "apps.analytics",
    "apps.blockchain",
    "apps.notifications",
    "apps.core",
]

INSTALLED_APPS = DJANGO_APPS + THIRD_PARTY_APPS + LOCAL_APPS

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [BASE_DIR / "templates"],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.debug",
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "apps.core.context_processors.dreamlens",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"
ASGI_APPLICATION = "config.asgi.application"

DATABASES = {
    "default": env.db(
        "DATABASE_URL",
        default="postgres://dreamlens:dreamlens@localhost:5432/dreamlens",
    )
}
# dj-database-url defaults CONN_MAX_AGE to 0 (new TLS handshake every request,
# ~2s to Neon). Reuse the socket unless CONN_MAX_AGE is set explicitly.
DATABASES["default"]["CONN_MAX_AGE"] = env.int("CONN_MAX_AGE", default=60)
DATABASES["default"].setdefault("CONN_HEALTH_CHECKS", True)
_db_opts = DATABASES["default"].setdefault("OPTIONS", {})
_db_opts.setdefault("sslmode", "require")
_db_opts.setdefault("connect_timeout", 15)
# libpq may try GSSAPI before TLS; that hang looks like a cloud-Postgres timeout.
_db_opts.setdefault("gssencmode", "disable")
# Neon poolers speak TLS immediately and do not answer libpq's SSLRequest.
# Supabase (and most other hosts) use a normal SSL handshake — do not force this.
_db_host = (DATABASES["default"].get("HOST") or "").lower()
if "neon.tech" in _db_host:
    _db_opts.setdefault("sslnegotiation", "direct")

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.UserAttributeSimilarityValidator"},
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator"},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "en-us"
TIME_ZONE = "UTC"
USE_I18N = True
USE_TZ = True

STATIC_URL = "/static/"
STATICFILES_DIRS = [BASE_DIR / "static"]
STATIC_ROOT = BASE_DIR / "staticfiles"

MEDIA_URL = "/media/"
MEDIA_ROOT = BASE_DIR / "media"

DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"
AUTH_USER_MODEL = "accounts.User"

REDIS_URL = env("REDIS_URL", default="redis://localhost:6379/0")

CACHES = {
    "default": {
        "BACKEND": "django_redis.cache.RedisCache",
        "LOCATION": REDIS_URL,
        "KEY_PREFIX": "dreamlens",
        "OPTIONS": {
            "CLIENT_CLASS": "django_redis.client.DefaultClient",
            "IGNORE_EXCEPTIONS": True,
        },
    }
}
DJANGO_REDIS_IGNORE_EXCEPTIONS = True
DJANGO_REDIS_LOG_IGNORED_EXCEPTIONS = True

SESSION_ENGINE = "django.contrib.sessions.backends.cache"
SESSION_CACHE_ALIAS = "default"

REST_FRAMEWORK = {
    "DEFAULT_AUTHENTICATION_CLASSES": [
        "rest_framework.authentication.SessionAuthentication",
    ],
    "DEFAULT_PERMISSION_CLASSES": [
        "rest_framework.permissions.IsAuthenticatedOrReadOnly",
    ],
    "DEFAULT_PAGINATION_CLASS": "rest_framework.pagination.PageNumberPagination",
    "PAGE_SIZE": 25,
    "DEFAULT_RENDERER_CLASSES": [
        "rest_framework.renderers.JSONRenderer",
        "rest_framework.renderers.BrowsableAPIRenderer",
    ],
}

CELERY_BROKER_URL = REDIS_URL
CELERY_RESULT_BACKEND = REDIS_URL
CELERY_ACCEPT_CONTENT = ["json"]
CELERY_TASK_SERIALIZER = "json"
CELERY_RESULT_SERIALIZER = "json"
CELERY_TIMEZONE = TIME_ZONE
CELERY_TASK_TRACK_STARTED = True
CELERY_TASK_TIME_LIMIT = 30 * 60
CELERY_IMPORTS = ("workers.event_sync", "workers.copy_monitor")

# DreamDEX integration
MOCK_DREAMDEX = env("MOCK_DREAMDEX")
DREAMDEX_NETWORK = env("DREAMDEX_NETWORK", default="testnet")
DREAMDEX_CHAIN_ID = env.int("DREAMDEX_CHAIN_ID", default=50312)
DREAMDEX_RPC_URL = env(
    "DREAMDEX_RPC_URL",
    default="https://api.infra.testnet.somnia.network",
)
DREAMDEX_WS_RPC_URL = env(
    "DREAMDEX_WS_RPC_URL",
    default="wss://api.infra.testnet.somnia.network/ws",
)
DREAMDEX_INDEXER_URL = env(
    "DREAMDEX_INDEXER_URL",
    default="https://dev.smk.somnia.host/v1/graphql",
)
DREAMDEX_VENUE_ID = env(
    "DREAMDEX_VENUE_ID",
    default="0x679795a0195a1b76cdebb7c51d74e058aee92919b8c3389af86ef24535e8a28c",
)
DREAMDEX_EVENT_SYNC_INTERVAL = env("DREAMDEX_EVENT_SYNC_INTERVAL")
DREAMDEX_COLLATERAL_DECIMALS = env("DREAMDEX_COLLATERAL_DECIMALS")
DREAMDEX_TICK = env("DREAMDEX_TICK")
DREAMDEX_LOT = env("DREAMDEX_LOT")
DREAMDEX_BINARY_MODULE = env("DREAMDEX_BINARY_MODULE", default="")
DREAMDEX_COLLATERAL = env("DREAMDEX_COLLATERAL", default="")

# MetaMask Smart Accounts / DreamAgent (ERC-7710)
# Runtime default is live. Tests force MOCK_SMART_ACCOUNT=true in conftest.
MOCK_SMART_ACCOUNT = env.bool("MOCK_SMART_ACCOUNT", default=False)
METAMASK_DELEGATION_MANAGER = env("METAMASK_DELEGATION_MANAGER", default="")
METAMASK_SIMPLE_FACTORY = env("METAMASK_SIMPLE_FACTORY", default="")
METAMASK_HYBRID_IMPL = env("METAMASK_HYBRID_IMPL", default="")
METAMASK_ENTRY_POINT = env("METAMASK_ENTRY_POINT", default="")
# Session EOA private key for redeemDelegations (never the user's owner key).
# Leave empty in mock mode — a deterministic mock session address is used.
DREAM_AGENT_SESSION_KEY = env("DREAM_AGENT_SESSION_KEY", default="")
DREAM_AGENT_GAS_LIMIT = env.int("DREAM_AGENT_GAS_LIMIT", default=800_000)
# Reimburse session-key gas from the Hybrid Smart Account's STT on each redeem.
DREAM_AGENT_SA_PAYS_GAS = env.bool("DREAM_AGENT_SA_PAYS_GAS", default=True)
DREAM_AGENT_GAS_BUFFER_BPS = env.int("DREAM_AGENT_GAS_BUFFER_BPS", default=2500)
# Cap per-trade native reimbursement (0.05 STT default).
DREAM_AGENT_MAX_GAS_PAYMENT_WEI = env.int(
    "DREAM_AGENT_MAX_GAS_PAYMENT_WEI", default=5 * 10**16
)

# Supabase Data API (HTTPS). Not a Django database backend — ORM still uses DATABASE_URL.
SUPABASE_URL = env("SUPABASE_URL", default="")
SUPABASE_KEY = env("SUPABASE_KEY", default="")

# Telegram bot (DreamAgent remote control). Token stays in env — never commit it.
TELEGRAM_BOT_TOKEN = env("TELEGRAM_BOT_TOKEN", default="")
TELEGRAM_BOT_USERNAME = env("TELEGRAM_BOT_USERNAME", default="")
TELEGRAM_WEBHOOK_SECRET = env("TELEGRAM_WEBHOOK_SECRET", default="")

# Periodic DreamDEX market sync when Celery Beat is running.
CELERY_BEAT_SCHEDULE = {
    "sync-dreamdex-markets": {
        "task": "workers.event_sync.full_event_sync_task",
        "schedule": float(DREAMDEX_EVENT_SYNC_INTERVAL),
    },
}

# LLM (AI Lens) — Google AI Studio Gemini 3.7 Flash by default
GEMINI_API_KEY = env("GEMINI_API_KEY", default="") or env("GOOGLE_AI_STUDIO_API_KEY", default="")
OPENROUTER_API_KEY = env("OPENROUTER_API_KEY", default="")
LLM_PROVIDER = env("LLM_PROVIDER", default="google")
LLM_MODEL = env("LLM_MODEL", default="gemini-3.7-flash")
LLM_BASE_URL = env(
    "LLM_BASE_URL",
    default="https://generativelanguage.googleapis.com/v1beta/openai",
)
LLM_API_KEY = env("LLM_API_KEY", default="") or GEMINI_API_KEY or OPENROUTER_API_KEY
LLM_HTTP_REFERER = env("LLM_HTTP_REFERER", default="http://127.0.0.1:8000")
LLM_APP_TITLE = env("LLM_APP_TITLE", default="DreamLens")

# Local OpenAI-compatible fallback (Ollama / LM Studio / vLLM)
LOCAL_LLM_ENABLED = env.bool("LOCAL_LLM_ENABLED", default=True)
LOCAL_LLM_BASE_URL = env("LOCAL_LLM_BASE_URL", default="http://127.0.0.1:11434/v1")
LOCAL_LLM_API_KEY = env("LOCAL_LLM_API_KEY", default="local")
LOCAL_LLM_MODEL = env("LOCAL_LLM_MODEL", default="llama3.2")

# CSRF
CSRF_TRUSTED_ORIGINS = env.list("CSRF_TRUSTED_ORIGINS", default=[])
CSRF_COOKIE_SECURE = env.bool("CSRF_COOKIE_SECURE", default=not DEBUG)
SESSION_COOKIE_SECURE = env.bool("SESSION_COOKIE_SECURE", default=not DEBUG)

LOGGING = {
    "version": 1,
    "disable_existing_loggers": False,
    "formatters": {
        "structured": {
            "format": "{levelname} {asctime} {name} {message}",
            "style": "{",
        },
    },
    "handlers": {
        "console": {
            "class": "logging.StreamHandler",
            "formatter": "structured",
        },
    },
    "root": {
        "handlers": ["console"],
        "level": env("LOG_LEVEL", default="INFO"),
    },
    "loggers": {
        "django": {
            "handlers": ["console"],
            "level": env("DJANGO_LOG_LEVEL", default="INFO"),
            "propagate": False,
        },
        "dreamlens": {
            "handlers": ["console"],
            "level": env("DREAMLENS_LOG_LEVEL", default="INFO"),
            "propagate": False,
        },
        "celery": {
            "handlers": ["console"],
            "level": env("CELERY_LOG_LEVEL", default="INFO"),
            "propagate": False,
        },
    },
}
