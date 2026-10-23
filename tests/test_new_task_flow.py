import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from sp_telegram import new_task_flow


class NewTaskCancel(unittest.TestCase):
    """"Cancelar" on the estimate prompt deletes the just-created task, but
    only while it's still the chat's pending estimate."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.estimate_file = Path(tmp.name) / "pending_estimate_state.json"
        p = patch.object(new_task_flow.config, "PENDING_ESTIMATE_STATE_FILE", self.estimate_file)
        p.start()
        self.addCleanup(p.stop)
        self.calls = []
        p = patch.object(new_task_flow, "_telegram_call", side_effect=lambda m, **kw: self.calls.append((m, kw)))
        p.start()
        self.addCleanup(p.stop)
        self.delete = patch.object(new_task_flow.vk, "_vk_delete").start()
        self.addCleanup(patch.stopall)

    def _pending(self, task_id):
        self.estimate_file.write_text(json.dumps({"1": {"task_id": task_id, "message_id": 5}}))

    def test_deletes_pending_task(self):
        self._pending(42)
        new_task_flow._handle_new_task_cancel("cb", 1, 5, "42")
        self.delete.assert_called_once_with("/tasks/42")
        self.assertEqual(json.loads(self.estimate_file.read_text()), {})
        self.assertIn(("editMessageText", {"chat_id": 1, "message_id": 5, "text": "Cancelado"}), self.calls)

    def test_stale_button_does_not_delete(self):
        self._pending(43)
        new_task_flow._handle_new_task_cancel("cb", 1, 5, "42")
        self.delete.assert_not_called()

    def test_no_pending_does_not_delete(self):
        new_task_flow._handle_new_task_cancel("cb", 1, 5, "42")
        self.delete.assert_not_called()


if __name__ == "__main__":
    unittest.main()
