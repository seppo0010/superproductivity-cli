import tempfile
import unittest
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import patch

from sp_telegram import formatting, punt_flow
from sp_telegram import vikunja as vk


class DueCountdownLine(unittest.TestCase):
    # Local noon, so offsets never land on the 23:59 "sin hora" sentinel.
    NOW = datetime(2026, 11, 9, 12, 0).astimezone()

    def _timed(self, **delta):
        iso = (self.NOW + timedelta(**delta)).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
        return formatting._due_countdown_line(iso, now=self.NOW)

    def _day(self, day):
        return formatting._due_countdown_line(vk._day_to_due_iso(day), now=self.NOW)

    def test_minutes(self):
        self.assertEqual(self._timed(minutes=10), "⏳ Vence en 10 minutos")

    def test_one_hour_is_singular(self):
        self.assertEqual(self._timed(hours=1), "⏳ Vence en 1 hora")

    def test_timed_within_a_day_uses_hours(self):
        self.assertEqual(self._timed(hours=5), "⏳ Vence en 5 horas")

    def test_timed_next_day_after_24h(self):
        self.assertEqual(self._timed(hours=25), "⏳ Vence mañana")

    def test_mistyped_month_stands_out_from_intended_date(self):
        self.assertEqual(self._day(date(2026, 11, 11)), "⏳ Vence en 2 días")
        self.assertEqual(self._day(date(2026, 12, 11)), "⏳ Vence en aprox. 1 mes")

    def test_timeless_today_and_tomorrow_ignore_hours(self):
        self.assertEqual(self._day(date(2026, 11, 9)), "⏳ Vence hoy")
        self.assertEqual(self._day(date(2026, 11, 10)), "⏳ Vence mañana")

    def test_weeks(self):
        self.assertEqual(self._day(date(2026, 11, 30)), "⏳ Vence en aprox. 3 semanas")

    def test_months(self):
        self.assertEqual(self._day(date(2027, 3, 9)), "⏳ Vence en aprox. 4 meses")

    def test_rolled_over_to_next_year_reads_as_one_year(self):
        self.assertEqual(self._day(date(2027, 11, 8)), "⏳ Vence en aprox. 1 año")

    def test_past_timed_is_flagged(self):
        self.assertEqual(self._timed(hours=-3), "⚠️ Ya venció: fue hace 3 horas")

    def test_past_days_are_flagged(self):
        self.assertEqual(self._day(date(2026, 11, 8)), "⚠️ Ya venció: fue ayer")
        self.assertEqual(self._day(date(2026, 11, 4)), "⚠️ Ya venció: fue hace 5 días")


class DayCommand(unittest.TestCase):
    def test_today(self):
        self.assertEqual(formatting._day_command(date.today()), "/today")

    def test_tomorrow(self):
        self.assertEqual(formatting._day_command(date.today() + timedelta(days=1)), "/tomorrow")

    def test_other_days_keep_dated_command(self):
        day = date.today() + timedelta(days=2)
        self.assertEqual(formatting._day_command(day), f"/dia{day:%Y%m%d}")
        day = date.today() - timedelta(days=1)
        self.assertEqual(formatting._day_command(day), f"/dia{day:%Y%m%d}")

    def test_load_message_uses_short_commands_for_first_two_days(self):
        start = date.today()
        with patch.object(formatting.vk, "_free_windows_for", return_value=([], None, False)):
            msg = formatting._format_load_message({}, start, 3)
        self.assertIn("/today", msg)
        self.assertIn("/tomorrow", msg)
        self.assertIn(f"/dia{start + timedelta(days=2):%Y%m%d}", msg)
        self.assertNotIn(f"/dia{start:%Y%m%d}", msg)


class DueLabel(unittest.TestCase):
    def test_timed(self):
        iso = vk._local_time_to_iso(date(2026, 12, 11), 14, 30)
        self.assertEqual(formatting._due_label(iso), "2026-12-11 14:30")

    def test_sentinel_says_sin_hora(self):
        self.assertEqual(formatting._due_label(vk._day_to_due_iso(date(2026, 12, 11))), "2026-12-11, sin hora")


class ApplyPuntConfirmation(unittest.TestCase):
    def _apply(self, repeat_after=0, fork=None):
        task = {"id": 5, "title": "Foo", "repeat_after": repeat_after}
        iso = vk._day_to_due_iso(date.today() + timedelta(days=2))
        with tempfile.TemporaryDirectory() as d, \
                patch.object(punt_flow.config, "UNDO_STATE_FILE", Path(d) / "undo.json"), \
                patch.object(punt_flow.vk, "_postpone_task", return_value=fork):
            return punt_flow._apply_punt(task, 5, iso, "LABEL")

    def test_includes_countdown(self):
        self.assertEqual(self._apply(), "📅 Foo — pospuesta a LABEL\n⏳ Vence en 2 días")

    def test_fork_note_goes_after_countdown(self):
        text = self._apply(repeat_after=86400, fork={"id": 9})
        self.assertEqual(text, f"📅 Foo — pospuesta a LABEL\n⏳ Vence en 2 días{punt_flow._FORK_NOTE}")


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


class FormatDayMessageHideFutureEvents(unittest.TestCase):
    def _events(self):
        now = datetime.now()
        def ev(title, start, end, all_day=False):
            return {"title": title, "start": start, "end": end, "all_day": all_day, "busy": True}
        return [
            ev("Ongoing", now - timedelta(minutes=10), now + timedelta(minutes=10)),
            ev("Later", now + timedelta(minutes=30), now + timedelta(minutes=60)),
            ev("AllDay", now.replace(hour=0, minute=0), now.replace(hour=0, minute=0) + timedelta(days=1), True),
        ]

    def test_future_events_hidden(self):
        with patch("sp_telegram.vikunja._real_projects", return_value=[]):
            msg, _ = formatting._format_day_message(
                [], "hoy", day=date.today(), events=self._events(), hide_future_events=True,
            )
        self.assertIn("Ongoing", msg)
        self.assertIn("AllDay", msg)
        self.assertNotIn("Later", msg)

    def test_future_events_shown_by_default(self):
        with patch("sp_telegram.vikunja._real_projects", return_value=[]):
            msg, _ = formatting._format_day_message([], "hoy", day=date.today(), events=self._events())
        self.assertIn("Later", msg)


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
        # Shown on 2026-09-23 only because its fixed repeat interval projects
        # it there (see vk._recurring_projections) — marking it done here
        # would really complete the earlier real occurrence, so no /hecho.
        real_task = {"id": 7, "title": "Real", "project_id": 1, "due_date": "2026-09-19T10:00:00Z"}
        projected_task = {"id": 8, "title": "Projected", "project_id": 1, "due_date": "2026-09-23T10:00:00Z",
                          "_projected": True}
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

    def test_subdaily_recurring_command_includes_occurrence_time(self):
        task = dict(self.RECURRING, repeat_after=8 * 3600)
        local = vk._parse_vikunja_ts(task["due_date"]).astimezone().strftime("%Y%m%d%H%M")
        self.assertEqual(formatting._hecho_command(task), f" /hecho7_{local}")

    def test_subdaily_stale_same_day_occurrence_is_rejected(self):
        task = dict(self.RECURRING, repeat_after=8 * 3600, due_date="2026-10-17T11:00:00Z")
        token = formatting._occurrence_token(task)
        advanced = dict(task, due_date="2026-10-17T19:00:00Z")  # next occurrence, same local day
        upd, tg = self._run(f"/hecho7_{token}", advanced)
        upd.assert_not_called()
        self.assertIn("ya no está pendiente", tg.call_args.kwargs["text"])
        upd, _ = self._run(f"/hecho7_{token}", task)
        upd.assert_called_once_with(7, {"done": True})

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
