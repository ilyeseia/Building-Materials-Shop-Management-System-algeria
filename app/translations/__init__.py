"""Translation catalogues.

``en`` is the reference catalogue; ``ar`` and ``fr`` must define the same keys.
``tests/test_i18n.py`` enforces this, so a missing translation fails the suite
instead of surfacing as a mixed-language screen.
"""
from app.translations import ar, en, fr

CATALOGUES = {"ar": ar.CATALOGUE, "fr": fr.CATALOGUE, "en": en.CATALOGUE}

__all__ = ["ar", "fr", "en", "CATALOGUES"]
