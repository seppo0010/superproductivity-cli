import unittest
from datetime import date, datetime, time as dtime, timezone
from unittest.mock import patch

from sp_telegram import vikunja as vk


class ParseVikunjaTs(unittest.TestCase):
    def test_valid(self):
        dt = vk._parse_vikunja_ts("2026-08-21T14:30:00Z")
        self.assertEqual(dt, datetime(2026, 8, 21, 14, 30, 0, tzinfo=timezone.utc))

    def test_empty(self):
        self.assertIsNone(vk._parse_vikunja_ts(""))

    def test_zero_sentinel(self):
        self.assertIsNone(vk._parse_vikunja_ts("0001-01-01T00:00:00Z"))

    def test_unparseable(self):
        self.assertIsNone(vk._parse_vikunja_ts("not-a-date"))


class DayDueRoundtrip(unittest.TestCase):
    def test_day_to_due_iso_is_no_specific_time(self):
        day = date(2026, 8, 21)
        iso = vk._day_to_due_iso(day)
        task = {"due_date": iso}
        # The 23:59-local sentinel means "no specific time" — _task_due_dt
        # must treat it as unset even though a due_date is present.
        self.assertIsNone(vk._task_due_dt(task))
        self.assertEqual(vk._task_local_date(task), day)

    def test_task_due_dt_for_explicit_time(self):
        iso = vk._local_time_to_iso(date(2026, 8, 21), 9, 0)
        task = {"due_date": iso}
        due = vk._task_due_dt(task)
        self.assertIsNotNone(due)
        self.assertEqual(due.astimezone().strftime("%H:%M"), "09:00")

    def test_no_due_date(self):
        task = {"due_date": ""}
        self.assertIsNone(vk._task_due_dt(task))
        self.assertIsNone(vk._task_local_date(task))


class EstimateParsing(unittest.TestCase):
    def test_minutes_only(self):
        self.assertEqual(vk._parse_estimate_minutes("[15m] Buy milk"), 15)

    def test_hours_and_minutes(self):
        self.assertEqual(vk._parse_estimate_minutes("[1h30m] Big task"), 90)

    def test_hours_only(self):
        self.assertEqual(vk._parse_estimate_minutes("[2h] Task"), 120)

    def test_zero(self):
        self.assertEqual(vk._parse_estimate_minutes("[0] Free task"), 0)

    def test_no_prefix(self):
        self.assertIsNone(vk._parse_estimate_minutes("No prefix here"))

    def test_unparseable_prefix(self):
        self.assertIsNone(vk._parse_estimate_minutes("[whatever] Task"))

    def test_format_minutes(self):
        self.assertEqual(vk._format_minutes(0), "0m")
        self.assertEqual(vk._format_minutes(45), "45m")
        self.assertEqual(vk._format_minutes(60), "1h")
        self.assertEqual(vk._format_minutes(90), "1h30m")

    def test_set_estimate_adds_prefix(self):
        self.assertEqual(vk._set_estimate("Buy milk", 15), "[15m] Buy milk")

    def test_set_estimate_replaces_prefix(self):
        self.assertEqual(vk._set_estimate("[15m] Buy milk", 30), "[30m] Buy milk")

    def test_set_estimate_zero(self):
        self.assertEqual(vk._set_estimate("Buy milk", 0), "[0] Buy milk")

    def test_set_estimate_no_rest(self):
        self.assertEqual(vk._set_estimate("[15m]", 30), "[30m]")


class NextUnestimatedTask(unittest.TestCase):
    def test_skips_estimated_and_picks_soonest_due(self):
        tasks = [
            {"id": 1, "title": "[15m] Estimated", "due_date": vk._day_to_due_iso(date(2026, 1, 1))},
            {"id": 2, "title": "Later, unestimated", "due_date": vk._day_to_due_iso(date(2026, 1, 5))},
            {"id": 3, "title": "Sooner, unestimated", "due_date": vk._day_to_due_iso(date(2026, 1, 2))},
        ]
        with patch("sp_telegram.vikunja._vk_get", return_value=tasks):
            task = vk._next_unestimated_task()
        self.assertEqual(task["id"], 3)

    def test_undated_task_sorts_after_dated_ones(self):
        tasks = [
            {"id": 1, "title": "No due date", "due_date": ""},
            {"id": 2, "title": "Has a due date", "due_date": vk._day_to_due_iso(date(2026, 1, 2))},
        ]
        with patch("sp_telegram.vikunja._vk_get", return_value=tasks):
            task = vk._next_unestimated_task()
        self.assertEqual(task["id"], 2)

    def test_none_when_everything_estimated(self):
        tasks = [{"id": 1, "title": "[15m] Done deal", "due_date": ""}]
        with patch("sp_telegram.vikunja._vk_get", return_value=tasks):
            self.assertIsNone(vk._next_unestimated_task())


class ParseDayArg(unittest.TestCase):
    def test_hoy(self):
        self.assertEqual(vk._parse_day_arg("hoy"), date.today())

    def test_today_english(self):
        self.assertEqual(vk._parse_day_arg("today"), date.today())

    def test_manana(self):
        from datetime import timedelta
        self.assertEqual(vk._parse_day_arg("mañana"), date.today() + timedelta(days=1))

    def test_iso_date(self):
        self.assertEqual(vk._parse_day_arg("2026-08-21"), date(2026, 8, 21))

    def test_short_form(self):
        self.assertEqual(vk._parse_day_arg("21/8"), date(date.today().year, 8, 21))

    def test_invalid(self):
        self.assertIsNone(vk._parse_day_arg("not a date"))

    def test_invalid_short_form(self):
        self.assertIsNone(vk._parse_day_arg("32/13"))


class ParseTimeRange(unittest.TestCase):
    def test_valid(self):
        window = vk._parse_time_range("09:00-18:00")
        self.assertEqual(window, (dtime(9, 0), dtime(18, 0)))

    def test_end_before_start_invalid(self):
        self.assertIsNone(vk._parse_time_range("18:00-09:00"))

    def test_end_equals_start_invalid(self):
        self.assertIsNone(vk._parse_time_range("09:00-09:00"))

    def test_unparseable(self):
        self.assertIsNone(vk._parse_time_range("not a range"))


class ParseAvailabilityTarget(unittest.TestCase):
    def test_general(self):
        self.assertEqual(vk._parse_availability_target("general"), ("general", "general"))

    def test_weekday(self):
        self.assertEqual(vk._parse_availability_target("martes"), ("weekday", "1"))

    def test_weekday_accent_variant(self):
        self.assertEqual(vk._parse_availability_target("miercoles"), ("weekday", "2"))
        self.assertEqual(vk._parse_availability_target("miércoles"), ("weekday", "2"))

    def test_date(self):
        self.assertEqual(vk._parse_availability_target("2026-08-21"), ("date", "2026-08-21"))

    def test_invalid(self):
        self.assertIsNone(vk._parse_availability_target("not a target"))


class MergeIntervals(unittest.TestCase):
    def test_merges_overlapping(self):
        a = datetime(2026, 8, 21, 9, 0)
        b = datetime(2026, 8, 21, 10, 0)
        c = datetime(2026, 8, 21, 9, 30)
        d = datetime(2026, 8, 21, 11, 0)
        merged = vk._merge_intervals([(a, b), (c, d)])
        self.assertEqual(merged, [(a, d)])

    def test_keeps_disjoint(self):
        a = datetime(2026, 8, 21, 9, 0)
        b = datetime(2026, 8, 21, 10, 0)
        c = datetime(2026, 8, 21, 11, 0)
        d = datetime(2026, 8, 21, 12, 0)
        merged = vk._merge_intervals([(c, d), (a, b)])
        self.assertEqual(merged, [(a, b), (c, d)])


class FreeWindowsFor(unittest.TestCase):
    def test_no_window_configured(self):
        with patch.object(vk, "_load_json", return_value={"weekday": {}, "date": {}, "general": None}):
            free, free_minutes, elapsed = vk._free_windows_for(date(2026, 8, 21), [])
        self.assertIsNone(free)
        self.assertIsNone(free_minutes)
        self.assertFalse(elapsed)

    def test_subtracts_busy_event(self):
        day = date(2026, 8, 21)  # a future, non-today date
        cfg = {"weekday": {}, "date": {}, "general": ["09:00", "18:00"]}
        events = [{
            "all_day": False, "busy": True,
            "start": datetime(2026, 8, 21, 10, 0),
            "end": datetime(2026, 8, 21, 11, 0),
        }]
        with patch.object(vk, "_load_json", return_value=cfg):
            free, free_minutes, elapsed = vk._free_windows_for(day, events)
        self.assertEqual(free_minutes, 8 * 60)  # 9h window - 1h busy
        self.assertFalse(elapsed)

    def test_free_event_does_not_subtract(self):
        day = date(2026, 8, 21)
        cfg = {"weekday": {}, "date": {}, "general": ["09:00", "18:00"]}
        events = [{
            "all_day": False, "busy": False,
            "start": datetime(2026, 8, 21, 10, 0),
            "end": datetime(2026, 8, 21, 11, 0),
        }]
        with patch.object(vk, "_load_json", return_value=cfg):
            _, free_minutes, _ = vk._free_windows_for(day, events)
        self.assertEqual(free_minutes, 9 * 60)


class SplitEstimatesByWindow(unittest.TestCase):
    def test_splits_inside_outside_missing(self):
        window = (dtime(9, 0), dtime(18, 0))
        inside_task = {"title": "[30m] inside", "due_date": vk._local_time_to_iso(date(2026, 8, 21), 10, 0)}
        outside_task = {"title": "[15m] outside", "due_date": vk._local_time_to_iso(date(2026, 8, 21), 22, 0)}
        missing_task = {"title": "no estimate"}
        inside, outside, missing = vk._split_estimates_by_window(
            [inside_task, outside_task, missing_task], window
        )
        self.assertEqual(inside, 30)
        self.assertEqual(outside, 15)
        self.assertEqual(missing, 1)

    def test_none_window_counts_everything_inside(self):
        task = {"title": "[10m] whenever", "due_date": vk._local_time_to_iso(date(2026, 8, 21), 23, 0)}
        inside, outside, missing = vk._split_estimates_by_window([task], None)
        self.assertEqual(inside, 10)
        self.assertEqual(outside, 0)
        self.assertEqual(missing, 0)


if __name__ == "__main__":
    unittest.main()
