import unittest

from sp_telegram import emoji_suggest


class SuggestEmojis(unittest.TestCase):
    def test_overlay_match(self):
        # Capped at 2 so the overlay's two hits ("llam" -> 📞, "dentist" -> 🦷)
        # return before the library index gets a chance to add a third.
        self.assertEqual(emoji_suggest.suggest_emojis("Llamar al dentista", max_suggestions=2), ["📞", "🦷"])

    def test_overlay_matches_inflections(self):
        self.assertIn("📞", emoji_suggest.suggest_emojis("Llamando a mamá"))

    def test_accent_insensitive(self):
        self.assertIn("👥", emoji_suggest.suggest_emojis("Reunion de equipo"))
        self.assertIn("👥", emoji_suggest.suggest_emojis("Reunión de equipo"))

    def test_library_index_match(self):
        self.assertIn("🏥", emoji_suggest.suggest_emojis("Ir al hospital"))

    def test_no_match_returns_empty(self):
        self.assertEqual(emoji_suggest.suggest_emojis("asdf qwer zxcv"), [])

    def test_max_suggestions_cap(self):
        suggestions = emoji_suggest.suggest_emojis("Llamar, pagar y comprar", max_suggestions=2)
        self.assertEqual(len(suggestions), 2)

    def test_dedup(self):
        suggestions = emoji_suggest.suggest_emojis("Pagar la factura")
        self.assertEqual(len(suggestions), len(set(suggestions)))


if __name__ == "__main__":
    unittest.main()
