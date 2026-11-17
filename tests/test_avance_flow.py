import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from sp_telegram import avance_flow, stats, undo


class _TmpState(unittest.TestCase):
    """Points every state file at a temp dir for the duration of a test."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        tmp = Path(self._tmp.name)
        cfg = avance_flow.config
        self._patches = [
            patch.object(cfg, "STATE_DIR", tmp),
            patch.object(cfg, "STATS_STATE_FILE", tmp / "stats.json"),
            patch.object(cfg, "UNDO_STATE_FILE", tmp / "undo.json"),
            patch.object(cfg, "PENDING_ESTIMATE_STATE_FILE", tmp / "pending_estimate.json"),
        ]
        for p in self._patches:
            p.start()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self._tmp.cleanup()


class ProgressTitle(unittest.TestCase):
    def test_replaces_estimate_and_appends_suffix(self):
        self.assertEqual(avance_flow._progress_title("[1h] 📞 Llamar", 20), "[20m] 📞 Llamar (avance)")

    def test_adds_estimate_when_missing(self):
        self.assertEqual(avance_flow._progress_title("Leer", 45), "[45m] Leer (avance)")


class ApplyProgress(_TmpState):
    def _run(self, task, minutes, fork=None):
        vk = avance_flow.vk
        progress = {"id": 900}

        def fake_update(task_id, changes):
            if task_id == 900:
                return {"id": 900, "title": "p", "done_at": "2026-11-17T12:00:00Z", **changes}
            return {"id": task_id, **changes}

        with patch.object(vk, "_vk_put", return_value=progress) as put, \
                patch.object(vk, "_vk_task_update", side_effect=fake_update) as update, \
                patch.object(vk, "_postpone_task", return_value=fork) as postpone:
            result = avance_flow._apply_progress(task, minutes)
        return result, put, update, postpone

    def test_one_off_lowers_estimate(self):
        task = {"id": 5, "project_id": 3, "title": "[1h] Informe", "due_date": "2026-11-17T20:00:00Z"}
        (text, remaining_id), put, update, postpone = self._run(task, 20)

        self.assertEqual(put.call_args.args[0], "/projects/3/tasks")
        self.assertEqual(put.call_args.args[1]["title"], "[20m] Informe (avance)")
        update.assert_any_call(900, {"done": True})
        update.assert_any_call(5, {"title": "[40m] Informe"})
        postpone.assert_not_called()
        self.assertEqual(remaining_id, 5)
        self.assertIn("Quedan 40m", text)

        state = stats._load_state()
        self.assertEqual(state["progress_ids"], [900])
        self.assertTrue(state["completions"][0]["partial"])
        self.assertEqual(state["completions"][0]["estimate_min"], None)  # fake title "p"
        undo_entry = avance_flow._load_json(avance_flow.config.UNDO_STATE_FILE, {})["5"]
        self.assertEqual((undo_entry["action"], undo_entry["progress_id"]), ("avance", 900))
        self.assertNotIn("fork_id", undo_entry)

    def test_recurring_forks_remainder(self):
        task = {"id": 7, "project_id": 3, "title": "[1h] Estudiar", "repeat_after": 86400,
                "due_date": "2026-11-17T20:00:00Z"}
        (text, remaining_id), _put, update, postpone = self._run(task, 15, fork={"id": 901})

        forked_task, due = postpone.call_args.args
        self.assertEqual((forked_task["id"], forked_task["title"], due), (7, "[45m] Estudiar", "2026-11-17T20:00:00Z"))
        # The recurring task itself is never retitled (its estimate stays [1h]).
        self.assertEqual([c.args[0] for c in update.call_args_list], [900])
        self.assertEqual(remaining_id, 901)
        undo_entry = avance_flow._load_json(avance_flow.config.UNDO_STATE_FILE, {})["7"]
        self.assertEqual(undo_entry["fork_id"], 901)

    def test_no_estimate_leaves_original(self):
        task = {"id": 5, "project_id": 3, "title": "Informe"}
        (text, remaining_id), _put, update, postpone = self._run(task, 20)
        self.assertEqual([c.args[0] for c in update.call_args_list], [900])
        postpone.assert_not_called()
        self.assertIn("Sin estimado", text)


class NeedsConfirmation(unittest.TestCase):
    def test_covers_estimate(self):
        self.assertTrue(avance_flow._needs_confirmation({"title": "[30m] A"}, 30))
        self.assertFalse(avance_flow._needs_confirmation({"title": "[30m] A"}, 20))
        self.assertFalse(avance_flow._needs_confirmation({"title": "A"}, 600))


class ResultKeyboard(unittest.TestCase):
    def test_punt_buttons_target_remaining_task(self):
        kb = avance_flow._result_keyboard({"id": 7, "title": "[1h] A"}, 15, 901)["inline_keyboard"]
        datas = [b["callback_data"] for row in kb for b in row]
        self.assertIn("puntdue:901:today", datas)
        self.assertEqual(datas[-1], "undo:7")
        self.assertNotIn("hcancel:0", datas)

    def test_only_undo_without_remaining_work(self):
        kb = avance_flow._result_keyboard({"id": 7, "title": "A"}, 15, 7)["inline_keyboard"]
        self.assertEqual(kb, [[{"text": "↩️ Deshacer", "callback_data": "undo:7"}]])


class UndoAvance(_TmpState):
    def _undo(self, entry):
        with undo._state_lock:
            undo._save_json(undo.config.UNDO_STATE_FILE, {"5": entry})
        with patch.object(undo.vk, "_vk_delete") as delete, \
                patch.object(undo.vk, "_vk_task_update") as update, \
                patch.object(undo, "_telegram_call"):
            undo._handle_undo("cb", 1, 2, "5")
        return delete, update

    def test_one_off_restores_title_and_drops_stats(self):
        state = stats._load_state()
        state["progress_ids"] = [900]
        stats._record_completion_if_new({"id": 900, "done_at": "2026-11-17T12:00:00Z"}, state)
        stats._save_json(stats.config.STATS_STATE_FILE, state)

        delete, update = self._undo({"action": "avance", "progress_id": 900,
                                     "task": {"id": 5, "title": "[1h] Informe", "due_date": "x"}})
        delete.assert_called_once_with("/tasks/900")
        update.assert_called_once_with(5, {"title": "[1h] Informe"})
        self.assertEqual(stats._load_state()["completions"], [])

    def test_recurring_deletes_fork_and_restores_due(self):
        delete, update = self._undo({"action": "avance", "progress_id": 900, "fork_id": 901,
                                     "task": {"id": 5, "title": "[1h] A", "due_date": "2026-11-17T20:00:00Z"}})
        self.assertEqual([c.args[0] for c in delete.call_args_list], ["/tasks/900", "/tasks/901"])
        update.assert_called_once_with(5, {"due_date": "2026-11-17T20:00:00Z"})


class StatsPartial(unittest.TestCase):
    def test_partial_counts_minutes_not_tasks(self):
        completions = [
            {"task_id": 1, "project_id": 1, "done_at": "2026-11-17T12:00:00Z", "estimate_min": 20, "partial": True},
            {"task_id": 2, "project_id": 1, "done_at": "2026-11-17T12:00:00Z", "estimate_min": 40},
        ]
        text = stats.format_summary(completions, date(2026, 11, 1), date(2026, 11, 17), {}, [], date(2026, 11, 17))
        self.assertIn("Completadas: <b>1</b>", text)
        self.assertIn("Avances parciales: 1", text)
        self.assertIn("1h", text)
        per_day = stats.completions_per_day(stats.finished(completions), date(2026, 11, 17), date(2026, 11, 17))
        self.assertEqual(list(per_day.values()), [1])


if __name__ == "__main__":
    unittest.main()
