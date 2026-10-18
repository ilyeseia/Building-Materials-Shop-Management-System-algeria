"""Internationalisation: locale resolution, translation, and locale-aware formatting.

Design notes
------------
* Three catalogues — Arabic (default), French, English — live in
  :mod:`app.translations`.  The key sets are asserted identical by the test
  suite, so a missing translation fails the build instead of leaking another
  language into the interface.
* Arabic is right-to-left; :func:`text_direction` drives the ``dir`` attribute of
  the ``<html>`` element so layout mirroring is handled by the browser rather
  than by hand-written CSS overrides.
* Money and quantities accept either ``.`` or ``,`` as the decimal separator on
  input (Algerian users type both) and are rendered with the separators
  configured by the administrator.
"""
from __future__ import annotations

from datetime import date, datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

from flask import session

from config import DEFAULT_WILAYA_CODE, WILAYAS
from app.translations import ar as _ar
from app.translations import en as _en
from app.translations import fr as _fr

LANGUAGES: dict[str, str] = {
    "ar": "العربية",
    "fr": "Français",
    "en": "English",
}
DEFAULT_LANGUAGE = "ar"
RTL_LANGUAGES = frozenset({"ar"})

_BASE_CATALOGUES: dict[str, dict[str, str]] = {
    "ar": _ar.CATALOGUE,
    "fr": _fr.CATALOGUE,
    "en": _en.CATALOGUE,
}

#: Numbers of the 58 wilayas are public administrative data (see config.WILAYAS).
_WILAYA_CATALOGUES: dict[str, dict[str, str]] = {
    "ar": {f"wilaya.{code}": name for code, name in _ar.WILAYAS_AR.items()},
    "fr": {f"wilaya.{code}": name for code, name in WILAYAS.items()},
    "en": {f"wilaya.{code}": name for code, name in WILAYAS.items()},
}

CATALOGUES: dict[str, dict[str, str]] = {
    lang: {**_BASE_CATALOGUES[lang], **_WILAYA_CATALOGUES[lang]} for lang in LANGUAGES
}


# ---------------------------------------------------------------------------
# Locale resolution
# ---------------------------------------------------------------------------

def normalise_language(value: str | None) -> str | None:
    """Return a supported language code, or ``None`` if unsupported."""
    if not value:
        return None
    code = value.strip().lower().replace("_", "-").split("-")[0]
    return code if code in LANGUAGES else None


def get_locale() -> str:
    """Current interface language.

    Order of precedence: explicit session choice → the authenticated user's
    stored preference → the configured application default.
    """
    from flask_login import current_user  # local import: avoids import cycles

    chosen = normalise_language(session.get("language"))
    if chosen:
        return chosen
    if getattr(current_user, "is_authenticated", False):
        stored = normalise_language(getattr(current_user, "language", None))
        if stored:
            return stored
    return DEFAULT_LANGUAGE


def set_locale(language: str | None) -> bool:
    """Persist the language choice in the session. Returns ``True`` on success."""
    code = normalise_language(language)
    if not code:
        return False
    session["language"] = code
    session.permanent = True
    return True


def text_direction(language: str | None = None) -> str:
    return "rtl" if (language or get_locale()) in RTL_LANGUAGES else "ltr"


def is_rtl(language: str | None = None) -> bool:
    return text_direction(language) == "rtl"


# ---------------------------------------------------------------------------
# Translation
# ---------------------------------------------------------------------------

def translate(key: str, language: str | None = None, **params: Any) -> str:
    """Translate ``key`` into the active language.

    Missing keys fall back to English and are then returned verbatim so that a
    catalogue gap is visible rather than silent.  ``params`` are interpolated
    with :meth:`str.format`.
    """
    lang = normalise_language(language) or get_locale()
    template = CATALOGUES.get(lang, {}).get(key)
    if template is None:
        template = CATALOGUES["en"].get(key, key)
    if params:
        try:
            return template.format(**params)
        except (KeyError, IndexError):
            return template
    return template


#: WTForms emits its default validation messages in English.  They are mapped
#: onto catalogue keys so that a validation error never appears in a different
#: language than the rest of the screen.  ``tests/test_i18n.py`` checks that every
#: target key exists.
_VALIDATOR_MESSAGE_KEYS: dict[str, str] = {
    "This field is required.": "val.required",
    "Invalid input.": "val.invalid_number",
    "Not a valid decimal value.": "val.invalid_number",
    "Not a valid integer value.": "val.invalid_number",
    "Not a valid date value.": "val.invalid_date",
    "Invalid email address.": "val.invalid_email",
}


def translate_error(message: Any) -> str:
    """Translate a validator message (a catalogue key or a WTForms default)."""
    if message is None:
        return ""
    text = str(message)
    if not text:
        return text
    key = _VALIDATOR_MESSAGE_KEYS.get(text)
    if key:
        return translate(key)
    if "characters long" in text and text.startswith("Field must be at least"):
        return translate("auth.password_too_short")
    if text.startswith("Field must be between") or text.startswith("Field must be at most"):
        return translate("val.invalid_length")
    if text.startswith("Number must be between"):
        return translate("val.out_of_range")
    if "must be equal to" in text:
        return translate("auth.password_mismatch")
    if text.startswith("Number must be greater than or equal to 0.01"):
        return translate("val.must_be_positive")
    if text.startswith("Number must be greater than") or text.startswith("Number must be less than"):
        return translate("val.must_not_be_negative")
    # Anything else is assumed to be a catalogue key already.
    return translate(text)


def localized_name(obj: Any, language: str | None = None, field: str = "name") -> str:
    """Return ``obj.name_<lang>`` when present, else ``obj.name``.

    Used for reference data (categories, units, warehouses) whose names are
    stored per language, while free-form business records keep a single name.
    """
    lang = normalise_language(language) or get_locale()
    for candidate in (f"{field}_{lang}", f"{field}_fr", f"{field}_ar", field):
        value = getattr(obj, candidate, None)
        if value:
            return str(value)
    return ""


def wilaya_name(code: int | None, language: str | None = None) -> str:
    if code is None:
        return ""
    return translate(f"wilaya.{int(code)}", language=language)


def wilaya_choices(language: str | None = None) -> list[tuple[int, str]]:
    lang = normalise_language(language) or get_locale()
    catalogue = _WILAYA_CATALOGUES.get(lang, _WILAYA_CATALOGUES["en"])
    return [
        (code, catalogue.get(f"wilaya.{code}", name))
        for code, name in sorted(WILAYAS.items())
    ]


# ---------------------------------------------------------------------------
# Number / money / date formatting
# ---------------------------------------------------------------------------

_SEPARATOR_DEFAULTS = {
    "ar": (" ", ","),   # (thousands, decimal)
    "fr": (" ", ","),
    "en": (",", "."),
}


def _separators(language: str) -> tuple[str, str]:
    """Separators from BusinessSettings when available, else locale defaults."""
    thousands, decimal = _SEPARATOR_DEFAULTS.get(language, _SEPARATOR_DEFAULTS["en"])
    try:
        from app.services.settings import get_settings  # local import: no cycle

        settings = get_settings()
        if settings is not None:
            thousands = settings.thousands_separator or thousands
            decimal = settings.decimal_separator or decimal
    except Exception:  # pragma: no cover - formatting must never raise
        pass
    return thousands, decimal


def _currency_symbol() -> str:
    try:
        from app.services.settings import get_settings

        settings = get_settings()
        if settings is not None and settings.currency_symbol:
            return settings.currency_symbol
    except Exception:  # pragma: no cover
        pass
    return "DA"


def format_number(
    value: Decimal | float | int | str | None,
    places: int = 2,
    language: str | None = None,
    grouping: bool = True,
) -> str:
    """Format a number with locale separators and no currency symbol."""
    if value is None or value == "":
        return ""
    try:
        dec = to_decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        return str(value)
    dec = dec.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    sign = "-" if dec < 0 else ""
    text = f"{abs(dec):.{places}f}"
    integer, _, fraction = text.partition(".")
    thousands, decimal = _separators(normalise_language(language) or get_locale())
    if grouping:
        chunks: list[str] = []
        while len(integer) > 3:
            chunks.insert(0, integer[-3:])
            integer = integer[:-3]
        chunks.insert(0, integer)
        integer = thousands.join(chunks)
    return f"{sign}{integer}{decimal}{fraction}" if places else f"{sign}{integer}"


def format_money(
    value: Decimal | float | int | str | None,
    language: str | None = None,
    places: int = 2,
    with_symbol: bool = True,
) -> str:
    """Format an amount in the configured currency (DZD by default).

    The symbol follows the amount, which is the conventional layout for the
    Algerian dinar ("12 500,00 DA").
    """
    text = format_number(value, places=places, language=language)
    if not text:
        return ""
    return f"{text} {_currency_symbol()}" if with_symbol else text


def format_quantity(
    value: Decimal | float | int | str | None,
    language: str | None = None,
    places: int = 3,
) -> str:
    """Format a commercial quantity, trimming trailing zeros."""
    if value is None or value == "":
        return ""
    try:
        dec = to_decimal(value)
    except (InvalidOperation, ValueError, TypeError):
        return str(value)
    text = format_number(dec, places=places, language=language)
    if places and ("." in text or "," in text):
        # Trim trailing zeros in the fractional part without touching separators.
        decimal_sep = _separators(normalise_language(language) or get_locale())[1]
        integer, sep, fraction = text.rpartition(decimal_sep)
        if sep:
            fraction = fraction.rstrip("0")
            text = f"{integer}{sep}{fraction}" if fraction else integer
    return text


def format_quantity_with_unit(
    value: Decimal | float | int | str | None,
    unit_code: str | None,
    language: str | None = None,
    places: int = 3,
) -> str:
    quantity = format_quantity(value, language=language, places=places)
    if not unit_code:
        return quantity
    return f"{quantity} {translate(f'unit.{unit_code}', language=language)}"


def to_decimal(value: Any, places: int | None = None) -> Decimal:
    """Convert user input to :class:`Decimal` accepting ``,`` or ``.`` decimals.

    Also tolerates thousands separators (spaces and non-breaking spaces).
    """
    if isinstance(value, Decimal):
        dec = value
    elif isinstance(value, int):
        dec = Decimal(value)
    elif isinstance(value, float):
        # Floats are only encountered for values already stored as float; the
        # string conversion avoids the worst binary artefacts.
        dec = Decimal(repr(value))
    else:
        text = str(value).strip().replace("\u00a0", "").replace("\u202f", "")
        text = text.replace(" ", "")
        if "," in text and "." in text:
            # Whichever comes last is the decimal separator.
            if text.rfind(",") > text.rfind("."):
                text = text.replace(".", "").replace(",", ".")
            else:
                text = text.replace(",", "")
        elif "," in text:
            text = text.replace(",", ".")
        dec = Decimal(text)
    if places is not None:
        dec = dec.quantize(Decimal(1).scaleb(-places), rounding=ROUND_HALF_UP)
    return dec


def format_date(value: date | datetime | None, language: str | None = None) -> str:
    """Render a date as ``DD/MM/YYYY`` (the customary Algerian short format)."""
    if value is None:
        return ""
    if isinstance(value, datetime):
        value = value.date()
    return value.strftime("%d/%m/%Y")


def format_datetime(value: datetime | None, language: str | None = None) -> str:
    if value is None:
        return ""
    return value.strftime("%d/%m/%Y %H:%M")


def format_percent(value: Decimal | float | int | None, places: int = 1) -> str:
    if value is None:
        return ""
    return f"{format_number(value, places=places, grouping=False)}%"


__all__ = [
    "LANGUAGES",
    "DEFAULT_LANGUAGE",
    "RTL_LANGUAGES",
    "CATALOGUES",
    "normalise_language",
    "get_locale",
    "set_locale",
    "text_direction",
    "is_rtl",
    "translate",
    "translate_error",
    "localized_name",
    "wilaya_name",
    "wilaya_choices",
    "format_number",
    "format_money",
    "format_quantity",
    "format_quantity_with_unit",
    "format_date",
    "format_datetime",
    "format_percent",
    "to_decimal",
    "DEFAULT_WILAYA_CODE",
]
