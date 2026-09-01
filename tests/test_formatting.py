import unittest
from datetime import date
from unittest.mock import patch

from sp_telegram import formatting


class LoadHeatEmoji(unittest.TestCase):
    def test_no_window_configured(self):
        self.assertEqual(formatting._load_heat_emoji(120, None), "⬜")

    def test_zero_load(self):
        self.assertEqual(formatting._load_heat_emoji(0, 480), "🟩")

    def test_no_free_time_left(self):
        self.assertEqual(formatting._load_heat_emoji(30, 0), "🟥")

    def test_low_ratio(self):
        self.assertEqual(formatting._load_heat_emoji(100, 480), "🟩")  # ~0.21

    def test_mid_ratio(self):
        self.assertEqual(formatting._load_heat_emoji(350, 480), "🟨")  # ~0.73

    def test_high_ratio(self):
        self.assertEqual(formatting._load_heat_emoji(450, 480), "🟧")  # ~0.94

    def test_over_capacity(self):
        self.assertEqual(formatting._load_heat_emoji(600, 480), "🟥")  # >1.0


class LabelsText(unittest.TestCase):
    def test_no_labels(self):
        self.assertEqual(formatting._labels_text({"labels": []}), "")
        self.assertEqual(formatting._labels_text({}), "")

    def test_with_labels(self):
        task = {"labels": [{"title": "Casa"}, {"title": "Urgente"}]}
        self.assertEqual(formatting._labels_text(task), " · 🏷 Casa, Urgente")

    def test_escapes_html(self):
        task = {"labels": [{"title": "<script>"}]}
        self.assertIn("&lt;script&gt;", formatting._labels_text(task))


class TaskTitleLink(unittest.TestCase):
    def test_builds_link_and_escapes_title(self):
        task = {"id": 42, "title": "<b>urgent</b>"}
        link = formatting._task_title_link(task)
        self.assertIn("/tasks/42", link)
        self.assertIn("&lt;b&gt;urgent&lt;/b&gt;", link)


class FormatDayMessageEmpty(unittest.TestCase):
    def test_nothing_pending(self):
        with patch("sp_telegram.vikunja._real_projects", return_value=[]):
            msg = formatting._format_day_message([], "hoy")
        self.assertIn("No hay tareas", msg)

    def test_only_overdue(self):
        overdue_task = {
            "id": 1, "title": "Old task", "project_id": 5,
            "due_date": "2026-01-01T23:59:00Z",
        }
        with patch("sp_telegram.vikunja._real_projects", return_value=[]):
            msg = formatting._format_day_message([], "hoy", overdue=[overdue_task])
        self.assertIn("Tareas vencidas", msg)
        self.assertIn("Old task", msg)
        self.assertIn("aparte de las vencidas", msg)


class TaskPickerKeyboard(unittest.TestCase):
    def test_includes_cancel_button(self):
        with patch("sp_telegram.vikunja._real_projects", return_value=[]):
            keyboard = formatting._task_picker_keyboard(
                [{"id": 1, "title": "Task", "due_date": ""}], "done"
            )
        rows = keyboard["inline_keyboard"]
        self.assertEqual(rows[-1][0]["callback_data"], "hcancel:0")
        self.assertEqual(rows[0][0]["callback_data"], "done:1")


if __name__ == "__main__":
    unittest.main()
