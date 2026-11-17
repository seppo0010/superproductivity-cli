"""Undo handling: every mutating flow snapshots the pre-change task into
UNDO_STATE_FILE keyed by task id; the "↩️ Deshacer" button reads it back."""

from __future__ import annotations

import requests

from . import config
from . import stats
from . import vikunja as vk
from .state import _load_json, _save_json, _state_lock
from .telegram_api import _telegram_call


def _handle_undo(callback_id: str, chat_id, message_id, payload: str) -> None:
    with _state_lock:
        state = _load_json(config.UNDO_STATE_FILE, {})
        entry = state.pop(payload, None)
        _save_json(config.UNDO_STATE_FILE, state)

    if not entry:
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text="Ya no se puede deshacer", show_alert=True,
        )
        return

    prev_task = entry["task"]
    try:
        if entry["action"] == "delete":
            body = {
                "title": prev_task["title"],
                "description": prev_task.get("description") or "",
                "due_date": prev_task.get("due_date") or "",
                "priority": prev_task.get("priority") or 0,
            }
            created = vk._vk_put(f"/projects/{prev_task['project_id']}/tasks", body)
            for label in prev_task.get("labels") or []:
                vk._vk_put(f"/tasks/{created['id']}/labels", {"label_id": label["id"]})
            result_text = f"↩️ {prev_task['title']} — restaurada"
        elif entry["action"] == "tarjeta":
            vk._vk_task_update(prev_task["id"], {"title": prev_task["title"]})
            prev_label_ids = {l["id"] for l in prev_task.get("labels") or []}
            label_obj = vk._find_label_by_title(vk._TARJETA_LABEL_TITLE)
            if label_obj is not None and label_obj["id"] not in prev_label_ids:
                vk._vk_delete(f"/tasks/{prev_task['id']}/labels/{label_obj['id']}")
            result_text = f"↩️ {prev_task['title']} — deshecho"
        elif entry["action"] == "avance":
            # Delete the progress entry (and its stats completion), then put
            # the remainder back on the original: a recurring task drops the
            # forked copy and returns to this occurrence's due date; a
            # one-off gets its pre-progress title (estimate) back.
            vk._vk_delete(f"/tasks/{entry['progress_id']}")
            stats.remove_latest_completion(entry["progress_id"])
            if entry.get("fork_id"):
                vk._vk_delete(f"/tasks/{entry['fork_id']}")
                vk._vk_task_update(prev_task["id"], {"due_date": prev_task.get("due_date") or ""})
            else:
                vk._vk_task_update(prev_task["id"], {"title": prev_task["title"]})
            result_text = f"↩️ {prev_task['title']} — avance deshecho"
        elif entry["action"] == "estimate":
            vk._vk_task_update(prev_task["id"], {"title": prev_task["title"]})
            result_text = f"↩️ {prev_task['title']} — deshecho"
        elif entry["action"] == "priority":
            vk._vk_task_update(prev_task["id"], {"priority": prev_task.get("priority") or 0})
            result_text = f"↩️ {prev_task['title']} — deshecho"
        else:
            # done/snooze*/punt only ever touch these two fields, so restoring
            # both from the pre-action snapshot reverses any of them — plus,
            # when postponing forked a recurring task, deleting the copy.
            if entry.get("fork_id"):
                vk._vk_delete(f"/tasks/{entry['fork_id']}")
            vk._vk_task_update(prev_task["id"], {
                "done": prev_task.get("done", False),
                "due_date": prev_task.get("due_date") or "",
            })
            if entry["action"] == "done":
                stats.remove_latest_completion(prev_task["id"])
            result_text = f"↩️ {prev_task['title']} — deshecho"
    except (requests.RequestException, RuntimeError) as e:
        config.log.error("Could not undo action for task %s: %s", payload, e)
        _telegram_call(
            "answerCallbackQuery", callback_query_id=callback_id,
            text=f"Error: {e}", show_alert=True,
        )
        return

    try:
        _telegram_call("editMessageText", chat_id=chat_id, message_id=message_id, text=result_text)
    except (requests.RequestException, RuntimeError) as e:
        config.log.error("Could not edit Telegram message: %s", e)
    _telegram_call("answerCallbackQuery", callback_query_id=callback_id, text="Deshecho")
    config.log.info("Undid '%s' for task %s", entry["action"], payload)
