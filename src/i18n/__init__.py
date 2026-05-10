"""Translations: per-bot language via Translator(lang)."""

import json
from pathlib import Path

DEFAULT_LANG = "ru"
_DIR = Path(__file__).resolve().parent


def available_languages() -> list[str]:
    return sorted(p.stem for p in _DIR.glob("*.json"))


class Translator:
    def __init__(self, lang: str = DEFAULT_LANG):
        self.lang = lang
        path = _DIR / f"{lang}.json"
        if not path.exists():
            path = _DIR / f"{DEFAULT_LANG}.json"
            self.lang = DEFAULT_LANG
        with path.open(encoding="utf-8") as f:
            self._strings: dict[str, str] = json.load(f)
        # Per-key fallback to the default language so newly added keys do not
        # surface as raw identifiers in not-yet-translated language files.
        self._fallback: dict[str, str] = {}
        if self.lang != DEFAULT_LANG:
            default_path = _DIR / f"{DEFAULT_LANG}.json"
            if default_path.exists():
                with default_path.open(encoding="utf-8") as f:
                    self._fallback = json.load(f)

    def t(self, key: str, **kwargs) -> str:
        s = self._strings.get(key)
        if s is None:
            s = self._fallback.get(key, key)
        if kwargs:
            try:
                return s.format(**kwargs)
            except (KeyError, IndexError):
                return s
        return s

    def get(self, key: str, default=None):
        """Return the raw value for a key (list/dict/str)."""
        if key in self._strings:
            return self._strings[key]
        if key in self._fallback:
            return self._fallback[key]
        return default
