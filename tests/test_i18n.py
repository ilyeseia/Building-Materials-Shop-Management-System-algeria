"""Interface language integrity.

These tests are the reason a missing translation fails the build instead of
showing a raw key such as ``sale.invoice`` to an Algerian shopkeeper.
"""
from __future__ import annotations

import ast
import re
from decimal import Decimal
from pathlib import Path

import pytest

from app.i18n import (
    CATALOGUES,
    LANGUAGES,
    _VALIDATOR_MESSAGE_KEYS,
    format_date,
    format_money,
    format_number,
    format_quantity,
    is_rtl,
    translate,
    translate_error,
    wilaya_name,
)

REPO_ROOT = Path(__file__).resolve().parent.parent
APP_DIR = REPO_ROOT / "app"

#: Functions whose first string argument is a catalogue key.
_KEY_FUNCTIONS = {
    "t", "translate", "translate_error", "_",
    "ValidationError", "BusinessError", "NotFoundError",
    "InsufficientStock", "InvalidTransition", "PermissionDenied",
}
_KEY_ARG_INDEX = {
    "ValidationError": 0,
    "BusinessError": 0,
}

_KEY_RE = re.compile(r"[a-z][a-z0-9_]*\.[a-z0-9_.]*[a-z0-9_]")
_TEMPLATE_CALL_RE = re.compile(r"""(?:t|translate|translate_error)\(\s*['"]([a-z][a-z0-9_]*\.[a-z0-9_.]*[a-z0-9_])['"]""")


def _catalogue_keys(language: str) -> set[str]:
    return set(CATALOGUES[language])


def test_languages_are_arabic_french_english():
    assert set(LANGUAGES) == {"ar", "fr", "en"}
    assert is_rtl("ar") and not is_rtl("fr") and not is_rtl("en")


def test_every_language_has_the_same_keys():
    """A gap in one language would silently leak another into the interface."""
    reference = _catalogue_keys("en")
    for language in ("ar", "fr"):
        missing = reference - _catalogue_keys(language)
        extra = _catalogue_keys(language) - reference
        assert not missing, f"{language} is missing keys: {sorted(missing)[:10]}"
        assert not extra, f"{language} has unknown keys: {sorted(extra)[:10]}"


def test_catalogue_is_not_empty_and_well_formed():
    keys = _catalogue_keys("ar")
    assert len(keys) > 500
    for key in keys:
        assert _KEY_RE.fullmatch(key), f"suspicious key name: {key!r}"
    for key in _VALIDATOR_MESSAGE_KEYS.values():
        assert key in keys, f"validator mapping points at a missing key: {key}"


def _keys_in_python(path: Path) -> set[str]:
    """Literal keys passed to the translation helpers and domain errors."""
    found: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        if not isinstance(node.func, ast.Name) or node.func.id not in _KEY_FUNCTIONS:
            continue
        if node.func.id in _KEY_ARG_INDEX:
            index = _KEY_ARG_INDEX[node.func.id]
        elif node.func.id in {"translate", "translate_error"} and len(node.args) >= 1:
            index = 0
        else:
            index = 0
        if len(node.args) <= index:
            continue
        first = node.args[index]
        if isinstance(first, ast.Constant) and isinstance(first.value, str):
            value = first.value
            if "{prefix}" in value or value.count(".") >= 1 and not value.startswith(("/", "http")):
                if _KEY_RE.fullmatch(value):
                    found.add(value)
        # keyword form: translate(key="...")
        for keyword in node.keywords:
            if keyword.arg == "key" and isinstance(keyword.value, ast.Constant):
                if isinstance(keyword.value.value, str) and _KEY_RE.fullmatch(keyword.value.value):
                    found.add(keyword.value.value)
    return found


def _keys_in_python_kwargs(path: Path) -> set[str]:
    """``message_key="..."`` keyword arguments in domain errors."""
    found: set[str] = set()
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    for node in ast.walk(tree):
        if isinstance(node, ast.Call):
            for keyword in node.keywords:
                if keyword.arg in {"message_key"} and isinstance(keyword.value, ast.Constant):
                    value = keyword.value.value
                    if isinstance(value, str) and _KEY_RE.fullmatch(value):
                        found.add(value)
    return found


def _keys_in_template(path: Path) -> set[str]:
    return set(_TEMPLATE_CALL_RE.findall(path.read_text(encoding="utf-8")))


def test_application_code_only_uses_existing_keys():
    """No literal key referenced from Python or a template may be missing."""
    keys = _catalogue_keys("en")
    referenced: dict[str, set[str]] = {}

    for path in APP_DIR.rglob("*.py"):
        if path.name == "i18n.py":
            continue
        found = _keys_in_python(path) | _keys_in_python_kwargs(path)
        # ``form_lines``-style helpers and settings keys start lower-case but are
        # not catalogue keys; they are filtered by the dotted-name regex above.
        if found:
            referenced[str(path.relative_to(REPO_ROOT))] = found

    for path in (APP_DIR / "templates").rglob("*.html"):
        found = _keys_in_template(path)
        if found:
            referenced[str(path.relative_to(REPO_ROOT))] = found

    problems = {
        source: sorted(key for key in keys_used if key not in keys)
        for source, keys_used in referenced.items()
    }
    problems = {source: missing for source, missing in problems.items() if missing}
    assert not problems, f"missing translation keys: {problems}"


def test_wilaya_names_are_available_in_all_languages():
    for language in ("ar", "fr", "en"):
        assert wilaya_name(19, language)  # Sétif
        assert wilaya_name(16, language)  # Alger
    assert wilaya_name(19, "ar") != wilaya_name(19, "fr")


def test_translate_falls_back_for_an_unknown_key():
    assert translate("does.not.exist", language="en") == "does.not.exist"


def test_translate_formats_parameters():
    assert translate("common.page", language="en") == "Page"


@pytest.mark.parametrize("language", ["ar", "fr", "en"])
def test_money_and_quantity_formatting_uses_decimal(language):
    rendered = format_money(Decimal("12345.5"), language=language)
    assert "12" in rendered and "345" in rendered
    assert "12345.5" not in rendered  # grouped
    decimal_separator = {"ar": ",", "fr": ",", "en": "."}[language]
    quantity = format_quantity(Decimal("1.2500"), language=language)
    assert quantity == f"1{decimal_separator}25"        # trailing zeros trimmed
    assert format_number(Decimal("0"), places=0, language=language) == "0"


def test_number_grouping_uses_the_locale_separators():
    assert format_number(Decimal("1234.5"), language="en") == "1,234.50"
    assert format_number(Decimal("1234.5"), language="fr") == "1 234,50"
    assert format_number(Decimal("1234.5"), language="ar") == "1 234,50"


def test_dates_are_rendered_dd_mm_yyyy():
    from datetime import date

    assert format_date(date(2026, 3, 9), language="ar") == "09/03/2026"


def test_validator_messages_are_translated(app):
    with app.test_request_context("/"):
        assert translate_error("This field is required.") == translate("val.required")
        assert translate_error("Not a valid decimal value.") == translate("val.invalid_number")
        assert translate_error("val.quantity_zero") == translate("val.quantity_zero")


def test_no_english_default_text_reaches_the_arabic_interface():
    """Spot-check a few labels that must not be identical across languages."""
    pairs = [
        ("sale.invoice", "ar", "en"),
        ("stock.on_hand", "ar", "en"),
        ("payment.amount", "ar", "en"),
        ("common.save", "ar", "en"),
    ]
    for key, first, second in pairs:
        assert translate(key, language=first) != translate(key, language=second), key
