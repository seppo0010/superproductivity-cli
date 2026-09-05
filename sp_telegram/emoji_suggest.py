"""Best-effort emoji suggestion for new task titles: a small hand-written
overlay for common Spanish task verbs, backed by a much larger automatic
reverse index built from the `emoji` package's CLDR-derived Spanish names."""

from __future__ import annotations

import re
import unicodedata

import emoji as _emoji_lib

# Hand-written overlay for common task verbs/synonyms the library's literal,
# noun-based names can't reach (e.g. "llamar" doesn't appear anywhere in
# 📞's Spanish name "auricular_de_teléfono"). Matched as accent-stripped
# substrings of the title so inflections (llamar/llamando/llamada) all match
# a single stem without a stemmer.
_OVERLAY: dict[str, str] = {
    "llam": "📞",
    "reunion": "👥",
    "junta": "👥",
    "pag": "💰",
    "factur": "💰",
    "cobr": "💰",
    "turno": "🏥",
    "medic": "🏥",
    "doctor": "🏥",
    "dentist": "🦷",
    "compr": "🛒",
    "cocin": "🍳",
    "comid": "🍳",
    "limpi": "🧹",
    "gimnasio": "🏋️",
    "ejercicio": "🏋️",
    "entren": "🏋️",
    "cumplea": "🎂",
    "lectur": "📚",
}

# Generic short words the library's compound names split into that would
# otherwise match almost any title (prepositions, articles).
_STOPWORDS = {"con", "sin", "para", "por", "del", "las", "los", "una", "uno"}

_MIN_WORD_LEN = 3


def _strip_accents(text: str) -> str:
    decomposed = unicodedata.normalize("NFKD", text)
    return "".join(c for c in decomposed if not unicodedata.combining(c))


def _normalize(text: str) -> str:
    return _strip_accents(text.lower())


def _is_flag(ch: str) -> bool:
    """Regional-indicator flag emoji (~260 country flags) — not useful for
    task titles and would otherwise flood the index with country names."""
    return any(0x1F1E6 <= ord(c) <= 0x1F1FF for c in ch)


def _build_library_index() -> dict[str, str]:
    index: dict[str, str] = {}
    for ch, info in _emoji_lib.EMOJI_DATA.items():
        if info.get("status") != _emoji_lib.STATUS["fully_qualified"] or _is_flag(ch):
            continue
        name = _emoji_lib.demojize(ch, language="es").strip(":") or info.get("en", "").strip(":")
        for word in name.split("_"):
            normalized = _normalize(word)
            if len(normalized) < _MIN_WORD_LEN or normalized in _STOPWORDS:
                continue
            index.setdefault(normalized, ch)
    return index


_LIBRARY_INDEX = _build_library_index()


def extract_emojis(text: str) -> list[str]:
    """Distinct emoji characters found in `text`, in first-appearance order.
    Used for a typed-emoji reply to the new-task emoji prompt (the user types
    an emoji instead of pressing one of the suggested-emoji buttons)."""
    found: list[str] = []
    for match in _emoji_lib.emoji_list(text):
        ch = match["emoji"]
        if ch not in found:
            found.append(ch)
    return found


def suggest_emojis(title: str, max_suggestions: int = 3) -> list[str]:
    """Best-effort emoji suggestions for `title`, most relevant first.

    Checks the hand-written verb overlay (substring match, catches Spanish
    inflections) before the much larger library-derived index (whole-word
    match, since its entries are generic nouns and substring matching would
    be too noisy at that scale). Returns [] if nothing matches."""
    normalized_title = _normalize(title)
    suggestions: list[str] = []

    for stem, ch in _OVERLAY.items():
        if stem in normalized_title and ch not in suggestions:
            suggestions.append(ch)
            if len(suggestions) >= max_suggestions:
                return suggestions

    for word in re.findall(r"[a-z0-9]+", normalized_title):
        ch = _LIBRARY_INDEX.get(word)
        if ch and ch not in suggestions:
            suggestions.append(ch)
            if len(suggestions) >= max_suggestions:
                break

    return suggestions
