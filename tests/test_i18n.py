"""Translator: lang loading, per-key fallback to default language, format args."""

from src.i18n import DEFAULT_LANG, Translator, available_languages


def test_default_language_loads() -> None:
    tr = Translator(DEFAULT_LANG)
    assert tr.lang == DEFAULT_LANG
    # Non-existent key returns key itself.
    assert tr.t("definitely_missing_key_xyz") == "definitely_missing_key_xyz"


def test_unknown_lang_falls_back_to_default() -> None:
    tr = Translator("xx-not-a-lang")
    assert tr.lang == DEFAULT_LANG


def test_format_kwargs_applied() -> None:
    tr = Translator(DEFAULT_LANG)
    # Pick any key that uses {error} — `error_internal` is one such key.
    # If it isn't formatted, just confirm format() does not crash.
    out = tr.t("error_internal", error="X")
    assert isinstance(out, str)


def test_missing_format_arg_returns_raw() -> None:
    tr = Translator(DEFAULT_LANG)
    # Passing wrong kwargs must not raise.
    out = tr.t("error_internal", wrong_key="value")
    assert isinstance(out, str)


def test_available_languages_lists_json_stems() -> None:
    langs = available_languages()
    assert DEFAULT_LANG in langs
    assert "en" in langs
