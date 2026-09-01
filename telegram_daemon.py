#!/usr/bin/env python3
"""Telegram daemon for sp-cli, backed by Vikunja.

Runs three things in one process:
  • A webhook receiver (background thread) that Vikunja POSTs to on
    task.overdue / tasks.overdue events (a user-level webhook you register
    yourself in Vikunja — see README). This is the fast path for "a task
    just became overdue" notifications.
  • Main thread: a safety-net reconciliation pass that re-checks for any
    overdue task not yet notified, in case a webhook delivery was missed
    (daemon downtime, network blip). Runs at most every
    CHECK_INTERVAL_SECONDS, but wakes up sooner when a task is already
    scheduled to become due before then.
  • Background thread: long-polls Telegram for button presses and commands
    and applies them to Vikunja via its REST API.

Prerequisites:
  • VIKUNJA_URL, VIKUNJA_TOKEN set in the environment (API token from
    Vikunja → Settings → API Tokens)
  • VIKUNJA_WEBHOOK_SECRET set to the secret used when you registered the
    user-level webhook (Vikunja → user settings → Webhooks) pointing at
    http://<this-host>:VIKUNJA_WEBHOOK_PORT/webhook, events task.overdue
    and tasks.overdue
  • TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID set in the environment

Google Calendar (optional): add a calendar's secret iCal address (Google
Calendar → calendar settings → "Integrate calendar" → "Secret address in
iCal format") via the /calendario agregar <url> bot command — no env var
needed. Events count toward the day's occupancy window unless marked Free
(the Busy/Free toggle on the event) or all-day. Fetched feeds are cached
in memory for GOOGLE_CALENDAR_CACHE_SECONDS (default 24h, since calendars
don't change often); /calendario actualizar flushes that cache on demand.

Implementation is split across the sp_telegram package (config, state,
vikunja, ical, formatting, telegram_api, notify, and the flow/command
modules) — this file just wires the threads together.
"""

from __future__ import annotations

import sys
import threading
import time

import requests

from sp_telegram import config
from sp_telegram.notify import _run_webhook_server, check_daily_digest, check_due_tasks
from sp_telegram.poll import poll_telegram_updates
from sp_telegram.telegram_api import _telegram_call


def main() -> None:
    if not config.TOKEN or not config.CHAT_ID:
        config.log.error("TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID must be set in the environment")
        sys.exit(1)
    if not config.VIKUNJA_TOKEN:
        config.log.error("VIKUNJA_TOKEN must be set in the environment")
        sys.exit(1)
    if not config.WEBHOOK_SECRET:
        config.log.warning("VIKUNJA_WEBHOOK_SECRET not set — incoming webhooks will not be signature-verified")

    try:
        _telegram_call(
            "setMyCommands",
            commands=[
                {"command": "hoy", "description": "Tareas de hoy"},
                {"command": "tomorrow", "description": "Tareas de mañana"},
                {"command": "day", "description": "Tareas de una fecha (YYYY-MM-DD)"},
                {"command": "carga", "description": "Carga estimada de los próximos días"},
                {"command": "disponibilidad", "description": "Ver o configurar ventana horaria disponible"},
                {"command": "calendario", "description": "Ver o agregar calendarios de Google (iCal)"},
                {"command": "hecho", "description": "Marcar una tarea de hoy como hecha"},
                {"command": "borrar", "description": "Borrar una tarea de hoy"},
                {"command": "tarjeta", "description": 'Etiquetar "Para tarjeta" y poner estimado en 0'},
                {"command": "punt", "description": "Posponer una tarea vencida o de hoy"},
                {"command": "estimar", "description": "Poner o cambiar la estimación de una tarea de hoy"},
                {"command": "prioridad", "description": "Poner o cambiar la prioridad de una tarea de hoy"},
            ],
        )
    except (requests.RequestException, RuntimeError) as e:
        config.log.warning("Could not register bot commands: %s", e)

    threading.Thread(target=poll_telegram_updates, daemon=True).start()
    threading.Thread(target=_run_webhook_server, daemon=True).start()

    while True:
        next_due_in = check_due_tasks()
        check_daily_digest()
        sleep_seconds = config.CHECK_INTERVAL_SECONDS
        if next_due_in is not None:
            sleep_seconds = min(config.CHECK_INTERVAL_SECONDS, next_due_in)
        time.sleep(sleep_seconds)


if __name__ == "__main__":
    main()
