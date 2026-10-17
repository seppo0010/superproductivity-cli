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

    def test_url_in_title_links_to_itself(self):
        task = {"id": 42, "title": "Leer https://example.com/a?x=1&y=2."}
        link = formatting._task_title_link(task)
        self.assertIn('/tasks/42">Leer </a>', link)
        self.assertIn(
            '<a href="https://example.com/a?x=1&amp;y=2">https://example.com/a?x=1&amp;y=2</a>', link
        )
        self.assertTrue(link.endswith('/tasks/42">.</a>'))

    def test_url_only_title_keeps_task_link(self):
        task = {"id": 42, "title": "https://example.com"}
        link = formatting._task_title_link(task)
        self.assertIn('<a href="https://example.com">https://example.com</a>', link)
        self.assertIn('/tasks/42">📋</a>', link)


class FormatDayMessageEmpty(unittest.TestCase):
    def test_nothing_pending(self):
        with patch("sp_telegram.vikunja._real_projects", return_value=[]):
            msg, keyboard = formatting._format_day_message([], "hoy")
        self.assertIn("No hay tareas", msg)
        self.assertIsNone(keyboard)

    def test_only_overdue(self):
        overdue_task = {
            "id": 1, "title": "Old task", "project_id": 5,
            "due_date": "2026-01-01T23:59:00Z",
        }
        with patch("sp_telegram.vikunja._real_projects", return_value=[]):
            msg, keyboard = formatting._format_day_message([], "hoy", overdue=[overdue_task])
        self.assertIn("Tareas vencidas", msg)
        self.assertIn("Old task", msg)
        self.assertIn("aparte de las vencidas", msg)
        self.assertIsNone(keyboard)


class FormatDayMessageEstimateShortcut(unittest.TestCase):
    def test_keyboard_offers_first_unestimated_task(self):
        tasks = [
            {"id": 1, "title": "[15m] Estimated", "project_id": None, "due_date": "2026-01-01T09:00:00Z"},
            {"id": 2, "title": "Missing estimate", "project_id": None, "due_date": "2026-01-01T10:00:00Z"},
        ]
        with patch("sp_telegram.vikunja._real_projects", return_value=[]):
            msg, keyboard = formatting._format_day_message(tasks, "hoy")
        self.assertIn("sin estimación", msg)
        self.assertIsNotNone(keyboard)
        self.assertEqual(keyboard["inline_keyboard"][0][0]["callback_data"], "estim:2")

    def test_no_keyboard_when_all_estimated(self):
        tasks = [{"id": 1, "title": "[15m] Estimated", "project_id": None, "due_date": "2026-01-01T09:00:00Z"}]
        with patch("sp_telegram.vikunja._real_projects", return_value=[]):
            _, keyboard = formatting._format_day_message(tasks, "hoy")
        self.assertIsNone(keyboard)


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


class TappableCommands(unittest.TestCase):
    def test_day_message_lists_hecho_command_per_task(self):
        tasks = [{"id": 7, "title": "Foo", "project_id": 1}, {"id": 12, "title": "Bar", "project_id": 1}]
        with patch("sp_telegram.vikunja._real_projects", return_value=[]):
            msg, _ = formatting._format_day_message(tasks, "hoy")
        self.assertIn("/hecho7", msg)
        self.assertIn("/hecho12", msg)

    def test_load_message_lists_dia_command_per_day(self):
        start = date(2026, 9, 19)
        with patch("sp_telegram.vikunja._free_windows_for", return_value=(None, None, False)):
            msg = formatting._format_load_message({}, start, 2)
        self.assertIn("/dia20260919", msg)
        self.assertIn("/dia20260920", msg)

    def test_day_message_omits_hecho_for_projected_recurring_occurrence(self):
        # Task actually due 2026-09-19, shown on 2026-09-23 only because its
        # fixed repeat interval projects it there (see
        # vk._recurring_projection_dates) — marking it done here would really
        # complete the 2026-09-19 occurrence, not this one, so no /hecho.
        real_task = {"id": 7, "title": "Real", "project_id": 1, "due_date": "2026-09-19T10:00:00Z"}
        projected_task = {"id": 8, "title": "Projected", "project_id": 1, "due_date": "2026-09-19T10:00:00Z"}
        with patch("sp_telegram.vikunja._real_projects", return_value=[]):
            msg, _ = formatting._format_day_message(
                [projected_task], "23/09", day=date(2026, 9, 23),
            )
            msg_real, _ = formatting._format_day_message(
                [real_task], "19/09", day=date(2026, 9, 19),
            )
        self.assertNotIn("/hecho8", msg)
        self.assertIn("/hecho7", msg_real)


class HechoOccurrenceGuard(unittest.TestCase):
    """Recurring tasks carry the occurrence date in /hecho so a repeated
    tap can't complete the next occurrence too."""

    RECURRING = {"id": 7, "title": "Regar", "project_id": 1, "repeat_after": 86400, "repeat_mode": 0,
                 "due_date": "2026-10-17T13:00:00Z"}

    def test_recurring_command_includes_occurrence_date(self):
        self.assertEqual(formatting._hecho_command(self.RECURRING), " /hecho7_20261017")

    def test_one_off_command_has_no_date(self):
        task = {"id": 9, "title": "Foo", "due_date": "2026-10-17T13:00:00Z"}
        self.assertEqual(formatting._hecho_command(task), " /hecho9")

    def _run(self, text, task):
        from sp_telegram import commands, config
        with patch("sp_telegram.vikunja._find_task", return_value=task), \
             patch("sp_telegram.vikunja._vk_task_update") as upd, \
             patch("sp_telegram.commands._telegram_call") as tg, \
             patch("sp_telegram.commands._save_json"), \
             patch("sp_telegram.commands._load_json", return_value={}):
            commands._handle_message({"chat": {"id": config.CHAT_ID}, "text": text})
        return upd, tg

    def test_matching_occurrence_marks_done(self):
        upd, _ = self._run("/hecho7_20261017", self.RECURRING)
        upd.assert_called_once_with(7, {"done": True})

    def test_stale_occurrence_is_rejected(self):
        advanced = dict(self.RECURRING, due_date="2026-10-18T13:00:00Z")
        upd, tg = self._run("/hecho7_20261017", advanced)
        upd.assert_not_called()
        self.assertIn("ya no está pendiente", tg.call_args.kwargs["text"])

    def test_bare_command_on_recurring_asks_for_confirmation(self):
        upd, tg = self._run("/hecho7", self.RECURRING)
        upd.assert_not_called()
        self.assertIn("/hecho7_20261017", tg.call_args.kwargs["text"])

    def test_already_done_one_off_is_not_touched(self):
        upd, _ = self._run("/hecho9", {"id": 9, "title": "Foo", "done": True})
        upd.assert_not_called()
