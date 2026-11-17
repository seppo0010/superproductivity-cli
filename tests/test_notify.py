import hashlib
import hmac
import unittest
from unittest.mock import patch

from sp_telegram import notify


class ExtractTasksFromWebhookData(unittest.TestCase):
    def test_single_task(self):
        data = {"task": {"id": 1, "title": "A"}}
        self.assertEqual(notify._extract_tasks_from_webhook_data(data), [{"id": 1, "title": "A"}])

    def test_multiple_tasks(self):
        data = {"tasks": [{"id": 1}, {"id": 2}]}
        self.assertEqual(notify._extract_tasks_from_webhook_data(data), [{"id": 1}, {"id": 2}])

    def test_neither(self):
        self.assertEqual(notify._extract_tasks_from_webhook_data({}), [])

    def test_task_not_a_dict_is_ignored(self):
        self.assertEqual(notify._extract_tasks_from_webhook_data({"task": "oops"}), [])


class VerifySignature(unittest.TestCase):
    def test_no_secret_configured_always_passes(self):
        with patch.object(notify.config, "WEBHOOK_SECRET", None):
            self.assertTrue(notify._verify_signature(b"anything", ""))

    def test_valid_signature(self):
        secret = "shh"
        body = b'{"event_name": "task.overdue"}'
        sig = hmac.new(secret.encode(), body, hashlib.sha256).hexdigest()
        with patch.object(notify.config, "WEBHOOK_SECRET", secret):
            self.assertTrue(notify._verify_signature(body, sig))

    def test_invalid_signature(self):
        with patch.object(notify.config, "WEBHOOK_SECRET", "shh"):
            self.assertFalse(notify._verify_signature(b"body", "wrong-signature"))

    def test_missing_signature(self):
        with patch.object(notify.config, "WEBHOOK_SECRET", "shh"):
            self.assertFalse(notify._verify_signature(b"body", ""))


class NotifyIfNew(unittest.TestCase):
    def test_skips_already_notified(self):
        notified = {"1": {"message_id": 99, "sent_at": "2026-01-01T00:00:00+00:00"}}
        with patch.object(notify, "_send_due_notification") as mock_send:
            notify._notify_if_new({"id": 1, "title": "T"}, notified)
        mock_send.assert_not_called()

    def test_skips_done_tasks(self):
        notified = {}
        with patch.object(notify, "_send_due_notification") as mock_send:
            notify._notify_if_new({"id": 1, "title": "T", "done": True}, notified)
        mock_send.assert_not_called()
        self.assertEqual(notified, {})

    def test_sends_and_records_new_task(self):
        notified = {}
        with patch.object(notify, "_send_due_notification", return_value=555):
            notify._notify_if_new({"id": 1, "title": "T"}, notified)
        self.assertEqual(notified["1"]["message_id"], 555)

    def test_send_failure_does_not_record(self):
        notified = {}
        with patch.object(notify, "_send_due_notification", return_value=None):
            notify._notify_if_new({"id": 1, "title": "T"}, notified)
        self.assertEqual(notified, {})


if __name__ == "__main__":
    unittest.main()


class EnsureProjectWebhooks(unittest.TestCase):
    def _run(self, existing):
        with patch.object(notify.config, "WEBHOOK_TARGET_URL", "http://hook/webhook"), \
                patch.object(notify.config, "WEBHOOK_SECRET", "s"), \
                patch.object(notify.vk, "_real_projects", return_value=[{"id": 1, "title": "A"}, {"id": 2, "title": "B"}]), \
                patch.object(notify.vk, "_vk_get", side_effect=lambda path: existing.get(path, [])), \
                patch.object(notify.vk, "_vk_put") as put:
            notify.ensure_project_webhooks(force=True)
        return put

    def test_creates_only_where_missing(self):
        existing = {
            "/projects/1/webhooks": [{"target_url": "http://hook/webhook", "events": ["task.updated", "task.created"]}],
            "/projects/2/webhooks": [{"target_url": "http://other/webhook", "events": ["task.updated"]}],
        }
        put = self._run(existing)
        put.assert_called_once_with("/projects/2/webhooks", {
            "target_url": "http://hook/webhook", "events": ["task.updated"], "secret": "s",
        })

    def test_wrong_events_on_our_target_still_creates(self):
        existing = {
            "/projects/1/webhooks": [{"target_url": "http://hook/webhook", "events": ["task.deleted"]}],
            "/projects/2/webhooks": [{"target_url": "http://hook/webhook", "events": ["task.updated"]}],
        }
        put = self._run(existing)
        self.assertEqual([c.args[0] for c in put.call_args_list], ["/projects/1/webhooks"])
