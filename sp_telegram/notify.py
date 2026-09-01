"""Overdue-task notifications: the webhook receiver (push path), the
reconciliation safety net (main-thread poll), and the once-a-day digest."""

from __future__ import annotations

import hashlib
import hmac
import http.server
import json
from datetime import date, datetime, timezone
from typing import Optional

import requests

from . import config
from . import formatting
from . import ical
from . import vikunja as vk
from .state import _load_json, _save_json, _state_lock
from .telegram_api import _send_due_notification, _telegram_call


# ─── Overdue notification state (shared between webhook + reconciliation) ──
#
# `notified` maps str(task_id) -> {"message_id": int, "sent_at": iso str}, so
# reconciliation can tell how long a due notification has gone unacknowledged
# (not done, not punted/snoozed) and re-post it after RESEND_AFTER_SECONDS.

def _notify_if_new(task: dict, notified: dict) -> None:
    key = str(task["id"])
    if key in notified or task.get("done"):
        return
    message_id = _send_due_notification(task)
    if message_id is not None:
        notified[key] = {"message_id": message_id, "sent_at": datetime.now(timezone.utc).isoformat()}


def _resend_if_stale(task: dict, notified: dict) -> None:
    key = str(task["id"])
    entry = notified.get(key)
    if entry is None or task.get("done"):
        return
    sent_at = datetime.fromisoformat(entry["sent_at"])
    if (datetime.now(timezone.utc) - sent_at).total_seconds() < config.RESEND_AFTER_SECONDS:
        return
    try:
        _telegram_call("deleteMessage", chat_id=config.CHAT_ID, message_id=entry["message_id"])
    except (requests.RequestException, RuntimeError) as e:
        config.log.warning("Could not delete stale due notification for task %s: %s", task["id"], e)
    message_id = _send_due_notification(task)
    if message_id is not None:
        notified[key] = {"message_id": message_id, "sent_at": datetime.now(timezone.utc).isoformat()}
    else:
        del notified[key]


# ─── Webhook receiver (background thread, push path) ────────────────────────

def _verify_signature(raw_body: bytes, signature: str) -> bool:
    if not config.WEBHOOK_SECRET:
        return True
    mac = hmac.new(config.WEBHOOK_SECRET.encode(), raw_body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(mac, signature or "")


def _extract_tasks_from_webhook_data(data: dict) -> list:
    if isinstance(data.get("task"), dict):
        return [data["task"]]
    if isinstance(data.get("tasks"), list):
        return data["tasks"]
    return []


def _handle_webhook_payload(payload: dict) -> None:
    event = payload.get("event_name", "")
    data = payload.get("data") or {}
    config.log.info("Webhook event received: %s", event)

    if event not in ("task.overdue", "tasks.overdue"):
        return

    tasks = _extract_tasks_from_webhook_data(data)
    if not tasks:
        config.log.warning("Overdue webhook event %s had no recognizable task(s) in payload: %s", event, json.dumps(data)[:2000])
        return

    with _state_lock:
        state = _load_json(config.NOTIFY_STATE_FILE, {"notified": {}})
        notified = state.get("notified", {})
        for t in tasks:
            _notify_if_new(t, notified)
        _save_json(config.NOTIFY_STATE_FILE, {"notified": notified})


class _WebhookHandler(http.server.BaseHTTPRequestHandler):
    def do_POST(self):
        if self.path != "/webhook":
            self.send_response(404)
            self.end_headers()
            return
        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length)
        signature = self.headers.get("X-Vikunja-Signature", "")
        if not _verify_signature(raw, signature):
            config.log.warning("Webhook signature mismatch, ignoring request")
            self.send_response(401)
            self.end_headers()
            return
        self.send_response(200)
        self.end_headers()
        try:
            _handle_webhook_payload(json.loads(raw))
        except (json.JSONDecodeError, KeyError, TypeError):
            config.log.exception("Could not parse webhook payload")

    def log_message(self, format, *args):
        config.log.info("webhook: " + format, *args)


def _run_webhook_server() -> None:
    server = http.server.ThreadingHTTPServer((config.WEBHOOK_HOST, config.WEBHOOK_PORT), _WebhookHandler)
    config.log.info("Webhook receiver listening on %s:%d", config.WEBHOOK_HOST, config.WEBHOOK_PORT)
    server.serve_forever()


# ─── Reconciliation pass (main thread, safety net) ──────────────────────────

def check_due_tasks() -> Optional[int]:
    """Safety net for missed webhook deliveries: sends notifications for any
    newly-overdue task not already notified. Returns the number of seconds
    until the next task with a known due time becomes due (so the caller can
    wake up sooner instead of waiting the full poll interval), or None if
    there's no upcoming due time to wait for."""
    try:
        tasks: list = vk._vk_get("/tasks", {"filter": "done = false"})
    except requests.RequestException as e:
        config.log.error("Could not fetch tasks: %s", e)
        return None

    now = datetime.now(timezone.utc)
    overdue = {}
    next_due_in = None
    for t in tasks:
        due_dt = vk._task_due_dt(t)
        if due_dt is None:
            continue
        if due_dt <= now:
            overdue[t["id"]] = t
        else:
            seconds = (due_dt - now).total_seconds()
            if next_due_in is None or seconds < next_due_in:
                next_due_in = seconds

    with _state_lock:
        state = _load_json(config.NOTIFY_STATE_FILE, {"notified": {}})
        notified = state.get("notified", {})

        new_ids = [tid for tid in overdue if str(tid) not in notified]
        for tid, task in overdue.items():
            if str(tid) in notified:
                _resend_if_stale(task, notified)
            else:
                _notify_if_new(task, notified)

        stale_keys = [k for k in notified if int(k) not in overdue]
        for k in stale_keys:
            del notified[k]

        _save_json(config.NOTIFY_STATE_FILE, {"notified": notified})

    if new_ids:
        config.log.info("Reconciliation notified %d newly overdue task(s)", len(new_ids))
    else:
        config.log.info("Reconciliation checked %d active task(s), none newly overdue", len(tasks))

    if next_due_in is None:
        return None
    return int(next_due_in) + 1


# ─── Daily digest (main thread, checked alongside reconciliation) ──────────

def check_daily_digest() -> None:
    """Sends the day's task list once per day, the first time the loop runs
    at or after DAILY_DIGEST_HOUR local time."""
    now = datetime.now()
    if now.hour < config.DAILY_DIGEST_HOUR:
        return

    today = now.strftime("%Y-%m-%d")
    state = _load_json(config.DAILY_DIGEST_STATE_FILE, {"last_sent_date": None})
    if state.get("last_sent_date") == today:
        return

    try:
        tasks = vk._today_tasks()
    except requests.RequestException as e:
        config.log.error("Could not fetch today's tasks for daily digest: %s", e)
        return

    events = ical._calendar_events_for_day(date.today())

    try:
        _telegram_call(
            "sendMessage", chat_id=config.CHAT_ID,
            text=formatting._format_day_message(tasks, "hoy", day=date.today(), events=events), parse_mode="HTML"
        )
    except (requests.RequestException, RuntimeError) as e:
        config.log.error("Failed to send daily digest: %s", e)
        return

    _save_json(config.DAILY_DIGEST_STATE_FILE, {"last_sent_date": today})
    config.log.info("Sent daily digest (%d task(s))", len(tasks))
