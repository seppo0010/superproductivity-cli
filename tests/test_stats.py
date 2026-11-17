import tempfile
import unittest
from datetime import date, datetime, timezone
from pathlib import Path
from unittest.mock import patch

from sp_telegram import notify, stats


def _state():
    return stats._empty_state()


class RecordCompletionIfNew(unittest.TestCase):
    def test_new_completion_recorded(self):
        state = _state()
        task = {"id": 1, "title": "[15m] Llamar", "project_id": 3, "done_at": "2026-11-17T10:00:00Z"}
        self.assertTrue(stats._record_completion_if_new(task, state))
        c = state["completions"][0]
        self.assertEqual((c["task_id"], c["project_id"], c["estimate_min"], c["recurring"]), (1, 3, 15, False))
        self.assertEqual(state["last_done_at"]["1"], "2026-11-17T10:00:00Z")

    def test_same_done_at_not_recorded_twice(self):
        state = _state()
        task = {"id": 1, "title": "A", "done_at": "2026-11-17T10:00:00Z"}
        stats._record_completion_if_new(task, state)
        self.assertFalse(stats._record_completion_if_new(task, state))
        self.assertEqual(len(state["completions"]), 1)

    def test_older_done_at_ignored(self):
        state = _state()
        stats._record_completion_if_new({"id": 1, "done_at": "2026-11-17T10:00:00Z"}, state)
        self.assertFalse(stats._record_completion_if_new({"id": 1, "done_at": "2026-11-16T10:00:00Z"}, state))

    def test_zero_or_missing_done_at_ignored(self):
        state = _state()
        self.assertFalse(stats._record_completion_if_new({"id": 1, "done_at": "0001-01-01T00:00:00Z"}, state))
        self.assertFalse(stats._record_completion_if_new({"id": 2, "title": "fork copy"}, state))
        self.assertEqual(state["completions"], [])

    def test_recurring_completed_again_recorded(self):
        state = _state()
        base = {"id": 7, "title": "Meds", "repeat_after": 86400, "done": False}
        stats._record_completion_if_new({**base, "done_at": "2026-11-16T10:00:00Z"}, state)
        self.assertTrue(stats._record_completion_if_new({**base, "done_at": "2026-11-17T10:00:00Z"}, state))
        self.assertEqual(len(state["completions"]), 2)
        self.assertTrue(state["completions"][1]["recurring"])


class RemoveLatestCompletion(unittest.TestCase):
    def test_removes_newest_and_keeps_last_done_at(self):
        state = _state()
        base = {"id": 7, "repeat_after": 86400}
        stats._record_completion_if_new({**base, "done_at": "2026-11-16T10:00:00Z"}, state)
        stats._record_completion_if_new({**base, "done_at": "2026-11-17T10:00:00Z"}, state)
        self.assertTrue(stats._remove_latest_completion(7, state))
        self.assertEqual([c["done_at"] for c in state["completions"]], ["2026-11-16T10:00:00Z"])
        # The undo's own task.updated webhook still carries this done_at.
        self.assertFalse(stats._record_completion_if_new({**base, "done_at": "2026-11-17T10:00:00Z"}, state))

    def test_unknown_task(self):
        self.assertFalse(stats._remove_latest_completion(99, _state()))


class Aggregation(unittest.TestCase):
    def setUp(self):
        # Midday UTC so the local date is the same in any timezone within ±11h.
        self.completions = [
            {"task_id": 1, "project_id": 1, "done_at": "2026-11-10T12:00:00Z", "estimate_min": 15},
            {"task_id": 2, "project_id": 2, "done_at": "2026-11-10T12:00:00Z", "estimate_min": None},
            {"task_id": 3, "project_id": 1, "done_at": "2026-11-12T12:00:00Z", "estimate_min": 60},
            {"task_id": 4, "project_id": 1, "done_at": "2026-10-01T12:00:00Z", "estimate_min": 5},
        ]

    def test_completions_per_day(self):
        per_day = stats.completions_per_day(self.completions, date(2026, 11, 10), date(2026, 11, 12))
        self.assertEqual(list(per_day.values()), [2, 0, 1])

    def test_in_range_by_project_and_minutes(self):
        in_range = stats.completions_in_range(self.completions, date(2026, 11, 1), date(2026, 11, 30))
        self.assertEqual(len(in_range), 3)
        self.assertEqual(stats.by_project(in_range), [(1, 2), (2, 1)])
        self.assertEqual(stats.estimated_minutes(in_range), 75)

    def test_snapshot_counts(self):
        today = date(2026, 11, 17)
        with patch.object(stats.vk, "_task_local_date", side_effect=[
            date(2026, 11, 15), date(2026, 11, 17), date(2026, 11, 20), None,
        ]):
            self.assertEqual(stats._snapshot_counts([{}, {}, {}, {}], today), {"pending": 2, "overdue": 1})

    def test_format_summary_mentions_totals_and_projects(self):
        text = stats.format_summary(
            self.completions, date(2026, 11, 1), date(2026, 11, 17), {1: "🌱 Vivir <x>"}, [], date(2026, 11, 17),
        )
        self.assertIn("Completadas: <b>3</b>", text)
        self.assertIn("🌱 Vivir &lt;x&gt;: 2", text)
        self.assertIn("Proyecto 2: 1", text)
        self.assertIn("1h15m", text)


class RenderChart(unittest.TestCase):
    def test_returns_png(self):
        per_day = {date(2026, 11, 10): 2, date(2026, 11, 11): 0, date(2026, 11, 12): 1}
        png = stats.render_chart(per_day, {"2026-11-11": {"pending": 4, "overdue": 2}})
        self.assertTrue(png.startswith(b"\x89PNG"))


class Persistence(unittest.TestCase):
    def test_record_and_undo_roundtrip(self):
        with tempfile.TemporaryDirectory() as tmp, \
                patch.object(stats.config, "STATE_DIR", Path(tmp)), \
                patch.object(stats.config, "STATS_STATE_FILE", Path(tmp) / "stats.json"):
            task = {"id": 5, "title": "A", "done_at": "2026-11-17T10:00:00Z"}
            self.assertEqual(stats.record_completions([task, task]), 1)
            stats.remove_latest_completion(5)
            self.assertEqual(stats._load_state()["completions"], [])


class WebhookRecordsCompletions(unittest.TestCase):
    def test_task_updated_feeds_stats(self):
        task = {"id": 1, "done_at": "2026-11-17T10:00:00Z"}
        with patch.object(notify.stats, "record_completions") as rec:
            notify._handle_webhook_payload({"event_name": "task.updated", "data": {"task": task}})
        rec.assert_called_once_with([task])


if __name__ == "__main__":
    unittest.main()
