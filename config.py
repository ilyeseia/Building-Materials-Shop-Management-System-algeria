"""Application configuration.

All environment-specific values are read from environment variables so that no
credential, secret or regulatory parameter is hard-coded in source control.

Algerian business defaults (currency, time zone, wilaya) live here as *defaults*
that the administrator can override through :class:`app.models.settings.BusinessSettings`
and the ``/settings`` screen.  Nothing here asserts legal compliance; regulatory
values are configuration, documented in ``docs/ALGERIA_LOCALIZATION.md``.
"""
from __future__ import annotations

import os
from datetime import timedelta
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent
INSTANCE_DIR = BASE_DIR / "instance"


def _bool_env(name: str, default: bool = False) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def _int_env(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except (TypeError, ValueError):
        return default


def _read_version() -> str:
    """Release version from ``VERSION``, with a safe source-checkout fallback."""
    try:
        value = (BASE_DIR / "VERSION").read_text(encoding="utf-8").strip()
        return value or "0.0.0-dev"
    except OSError:
        return "0.0.0-dev"


# ---------------------------------------------------------------------------
# Algerian reference data (public administrative data, not regulatory advice)
# ---------------------------------------------------------------------------

#: The 58 Algerian wilayas (``code`` -> French name).  Arabic names are in the
#: translation catalogue under ``wilaya.<code>``.
WILAYAS: dict[int, str] = {
    1: "Adrar", 2: "Chlef", 3: "Laghouat", 4: "Oum El Bouaghi", 5: "Batna",
    6: "Béjaïa", 7: "Biskra", 8: "Béchar", 9: "Blida", 10: "Bouira",
    11: "Tamanrasset", 12: "Tébessa", 13: "Tlemcen", 14: "Tiaret",
    15: "Tizi Ouzou", 16: "Alger", 17: "Djelfa", 18: "Jijel", 19: "Sétif",
    20: "Saïda", 21: "Skikda", 22: "Sidi Bel Abbès", 23: "Annaba", 24: "Guelma",
    25: "Constantine", 26: "Médéa", 27: "Mostaganem", 28: "M'Sila",
    29: "Mascara", 30: "Ouargla", 31: "Oran", 32: "El Bayadh", 33: "Illizi",
    34: "Bordj Bou Arreridj", 35: "Boumerdès", 36: "El Tarf", 37: "Tindouf",
    38: "Tissemsilt", 39: "El Oued", 40: "Khenchela", 41: "Souk Ahras",
    42: "Tipaza", 43: "Mila", 44: "Aïn Defla", 45: "Naâma", 46: "Aïn Témouchent",
    47: "Ghardaïa", 48: "Relizane", 49: "Timimoun", 50: "Bordj Badji Mokhtar",
    51: "Ouled Djellal", 52: "Béni Abbès", 53: "In Salah", 54: "In Guezzam",
    55: "Touggourt", 56: "Djanet", 57: "El M'Ghair", 58: "El Meniaa",
}

#: Default business wilaya (Sétif) — configurable by the administrator.
DEFAULT_WILAYA_CODE = 19


class Config:
    """Base configuration shared by every environment."""

    # --- Core ---------------------------------------------------------
    APP_NAME = os.environ.get("APP_NAME", "Building Materials Management")
    ENVIRONMENT = os.environ.get("APP_ENV", "development")
    #: Release identity.  The release script and the Docker build pass these in;
    #: the defaults keep a source checkout usable without any build pipeline.
    APP_VERSION = os.environ.get("APP_VERSION") or _read_version()
    BUILD_COMMIT = os.environ.get("BUILD_COMMIT", "")
    BUILD_DATE = os.environ.get("BUILD_DATE", "")
    #: Informational licensing.  A missing, expired or malformed license never
    #: disables a feature or blocks sign-in; see docs/COMMERCIAL.md.
    LICENSE_FILE = os.environ.get(
        "LICENSE_FILE", str(INSTANCE_DIR / "license.json")
    )
    LICENSE_SECRET = os.environ.get("LICENSE_SECRET")
    #: Backups are written here by scripts/backup.sh (same default as the image).
    BACKUP_DIR = os.environ.get("BACKUP_DIR", str(BASE_DIR / "backups"))
    #: A status page warns when the newest successful backup is older than this.
    BACKUP_STALE_HOURS = _int_env("BACKUP_STALE_HOURS", 48)

    #: Required in production.  ``create_app`` refuses to boot a production
    #: deployment without it; there is deliberately no usable fallback secret.
    SECRET_KEY = os.environ.get("SECRET_KEY")

    # --- Database -----------------------------------------------------
    # Development/tests default to a local SQLite file so the application runs
    # without a MySQL server.  Production must supply DATABASE_URL (MySQL).
    SQLALCHEMY_DATABASE_URI = os.environ.get("DATABASE_URL")
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS: dict = {
        "pool_pre_ping": True,
        "future": True,
    }

    # --- Session / cookies -------------------------------------------
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    SESSION_COOKIE_SECURE = _bool_env("SESSION_COOKIE_SECURE", False)
    PERMANENT_SESSION_LIFETIME = timedelta(
        minutes=_int_env("SESSION_LIFETIME_MINUTES", 480)
    )
    REMEMBER_COOKIE_HTTPONLY = True
    REMEMBER_COOKIE_SAMESITE = "Lax"
    REMEMBER_COOKIE_SECURE = _bool_env("SESSION_COOKIE_SECURE", False)

    # --- CSRF ---------------------------------------------------------
    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = _int_env("WTF_CSRF_TIME_LIMIT", 3600)

    # --- Uploads ------------------------------------------------------
    MAX_CONTENT_LENGTH = _int_env("MAX_CONTENT_LENGTH", 8 * 1024 * 1024)

    # --- Pagination ---------------------------------------------------
    ITEMS_PER_PAGE = _int_env("ITEMS_PER_PAGE", 25)

    # --- Brute-force protection (in-process, see security.py) --------
    LOGIN_MAX_ATTEMPTS = _int_env("LOGIN_MAX_ATTEMPTS", 5)
    LOGIN_WINDOW_SECONDS = _int_env("LOGIN_WINDOW_SECONDS", 300)

    # --- Localisation ------------------------------------------------
    LANGUAGES = {"ar": "العربية", "fr": "Français", "en": "English"}
    BABEL_DEFAULT_LOCALE = os.environ.get("DEFAULT_LANGUAGE", "ar")
    TIMEZONE = os.environ.get("TIMEZONE", "Africa/Algiers")

    # --- Business defaults (overridable at runtime in BusinessSettings)
    DEFAULT_CURRENCY = os.environ.get("CURRENCY", "DZD")
    DEFAULT_CURRENCY_SYMBOL = os.environ.get("CURRENCY_SYMBOL", "DA")
    DEFAULT_WILAYA_CODE = DEFAULT_WILAYA_CODE
    #: Quantitative precision for goods (mass/volume/length) and for money.
    QUANTITY_DECIMAL_PLACES = 3
    MONEY_DECIMAL_PLACES = 2

    #: An observation older than this is displayed as "needs update".  This is
    #: a display rule, not a stored judgement about the source.
    MARKET_PRICE_STALE_DAYS = _int_env("MARKET_PRICE_STALE_DAYS", 30)

    # --- Feature modules (a business may switch these off) -----------
    FEATURE_WEIGHBRIDGE = _bool_env("FEATURE_WEIGHBRIDGE", True)
    FEATURE_FLEET = _bool_env("FEATURE_FLEET", True)
    FEATURE_ACCOUNTING = _bool_env("FEATURE_ACCOUNTING", False)
    #: The AI workspace is optional.  With no external provider configured it
    #: still works: a deterministic, read-only assistant answers common stock,
    #: sales and balance questions locally.  A provider failure must never
    #: affect sales, stock or accounting; the workspace degrades to that local
    #: mode instead.
    FEATURE_AI = _bool_env("FEATURE_AI", True)

    # --- AI workspace ------------------------------------------------
    #: ``local`` (no network) or ``openai`` (any OpenAI-compatible
    #: ``/chat/completions`` endpoint, including self-hosted gateways).
    AI_PROVIDER = os.environ.get("AI_PROVIDER", "local").strip().lower()
    AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://api.openai.com/v1").rstrip("/")
    AI_API_KEY = os.environ.get("AI_API_KEY")
    AI_MODEL = os.environ.get("AI_MODEL", "gpt-4o-mini")
    AI_TIMEOUT_SECONDS = _int_env("AI_TIMEOUT_SECONDS", 20)
    AI_MAX_QUESTION_CHARS = _int_env("AI_MAX_QUESTION_CHARS", 1000)
    AI_MAX_TOOL_ROUNDS = _int_env("AI_MAX_TOOL_ROUNDS", 3)
    #: Requests per user per hour for the workspace ask endpoint.
    AI_RATE_LIMIT_PER_HOUR = _int_env("AI_RATE_LIMIT_PER_HOUR", 30)

    # --- Accounting ------------------------------------------------
    #: Tax rates are *configuration*, never hard-coded into business logic.
    #: Expressed as a **percentage** (``19`` means 19 %), matching the
    #: ``vat_rate`` form field (0–100), the ``Numeric`` "percent" columns and the
    #: ``| percent`` template filter.  ``0.19`` here would apply 0.19 %.
    DEFAULT_VAT_RATE = os.environ.get("DEFAULT_VAT_RATE", "19")

    JSON_SORT_KEYS = False


class DevelopmentConfig(Config):
    ENVIRONMENT = "development"
    DEBUG = True
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "DATABASE_URL", f"sqlite:///{(INSTANCE_DIR / 'dev.db').as_posix()}"
    )
    SQLALCHEMY_ECHO = _bool_env("SQLALCHEMY_ECHO", False)
    #: Development convenience secret.  Never used in production because
    #: ProductionConfig refuses to start without a real SECRET_KEY.
    SECRET_KEY = os.environ.get("SECRET_KEY") or "dev-only-insecure-secret"


class TestingConfig(Config):
    ENVIRONMENT = "testing"
    TESTING = True
    SECRET_KEY = "testing-secret-key"
    SQLALCHEMY_DATABASE_URI = "sqlite:///:memory:"
    SQLALCHEMY_ENGINE_OPTIONS: dict = {}
    WTF_CSRF_ENABLED = _bool_env("TEST_CSRF", False)
    LOGIN_MAX_ATTEMPTS = 1000  # tests exercise other paths
    ITEMS_PER_PAGE = 5


class ProductionConfig(Config):
    ENVIRONMENT = "production"
    DEBUG = False
    SESSION_COOKIE_SECURE = True
    REMEMBER_COOKIE_SECURE = True
    PREFERRED_URL_SCHEME = "https"


class BenchmarkConfig(Config):
    """Disposable performance database only.

    ``scripts/benchmark.py`` refuses to run unless the database name contains
    ``bench``, ``perf`` or ``scratch`` and ``BENCHMARK_CONFIRM=1`` is set, so this
    configuration cannot silently point at production.
    """

    ENVIRONMENT = "benchmark"
    TESTING = True
    DEBUG = False
    SECRET_KEY = "benchmark-only-not-for-production"
    SQLALCHEMY_DATABASE_URI = os.environ.get(
        "BENCHMARK_DATABASE_URL", f"sqlite:///{(INSTANCE_DIR / 'benchmark.db').as_posix()}"
    )
    SQLALCHEMY_ENGINE_OPTIONS: dict = {}
    WTF_CSRF_ENABLED = False
    SESSION_COOKIE_SECURE = False
    REMEMBER_COOKIE_SECURE = False
    LOGIN_MAX_ATTEMPTS = 1000
    ITEMS_PER_PAGE = 25


CONFIGS = {
    "development": DevelopmentConfig,
    "production": ProductionConfig,
    "testing": TestingConfig,
    "benchmark": BenchmarkConfig,
    "default": DevelopmentConfig,
}


def get_config(name: str | None = None):
    name = name or os.environ.get("APP_ENV") or os.environ.get("FLASK_ENV") or "default"
    return CONFIGS.get(name, CONFIGS["default"])


# Backwards-compatible alias for the legacy import style used before the refactor.
config = CONFIGS
