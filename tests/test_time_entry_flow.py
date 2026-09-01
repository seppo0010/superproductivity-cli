import unittest

from sp_telegram.time_entry_flow import _resolve_due_reply


class ResolveDueReply(unittest.TestCase):
    def test_time_only(self):
        parsed = _resolve_due_reply("14:30")
        self.assertIsNotNone(parsed)
        due_date_iso, due_label = parsed
        self.assertTrue(due_label.endswith(" 14:30"))

    def test_invalid_time(self):
        self.assertIsNone(_resolve_due_reply("25:00"))

    def test_date_with_explicit_year_no_time(self):
        due_date_iso, due_label = _resolve_due_reply("5/9/2027")
        self.assertEqual(due_label, "2027-09-05, sin hora")

    def test_date_with_explicit_year_and_time(self):
        due_date_iso, due_label = _resolve_due_reply("5/9/2027 13:00")
        self.assertEqual(due_label, "2027-09-05 13:00")

    def test_two_digit_year(self):
        due_date_iso, due_label = _resolve_due_reply("5/9/27")
        self.assertEqual(due_label, "2027-09-05, sin hora")

    def test_invalid_calendar_date(self):
        self.assertIsNone(_resolve_due_reply("32/13/2027"))

    def test_garbage(self):
        self.assertIsNone(_resolve_due_reply("not a date"))

    def test_empty(self):
        self.assertIsNone(_resolve_due_reply(""))


if __name__ == "__main__":
    unittest.main()
