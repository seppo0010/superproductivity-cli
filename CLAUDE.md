# sp-cli-telegram

A Telegram bot/daemon backed by Vikunja (self-hosted task manager). No web UI, no separate CLI —
`telegram_daemon.py` is the only entrypoint, run as the systemd user service `sp-cli-telegram`
(unit at `systemd/sp-cli-telegram.service`, also installed to `~/.config/systemd/user/`). Restart
after any change: `systemctl --user restart sp-cli-telegram` (user-scoped, no sudo).

Config comes from env vars (`VIKUNJA_URL`, `VIKUNJA_TOKEN`, `VIKUNJA_WEBHOOK_SECRET`,
`TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID`, ...) loaded via `EnvironmentFile=~/.config/sp-cli/telegram.env`
in the unit — see `sp_telegram/config.py` for the full list and defaults. State is small JSON files
under `~/.config/sp-cli/` (or `$SP_CLI_STATE_DIR`), one file per concern (notify state, bot update
offset, pending flows, undo snapshots, availability config, calendar URLs).

## Package layout (`sp_telegram/`)

One module per concern; `telegram_daemon.py` just wires the threads together (webhook server,
Telegram poll loop, reconciliation loop) and validates required env vars.

- `config.py` — env-derived constants, state file paths, logging setup. Imported by everything else.
- `state.py` — `_load_json`/`_save_json` + the `_state_lock` shared across threads for read-modify-write safety.
- `vikunja.py` — Vikunja REST client (`_vk_get/put/post/delete`) plus the domain logic built on it:
  due-date parsing/formatting, task queries (today/tomorrow/overdue/by-date-range), projects, the
  `[15m]`/`[1h30m]` title-prefix estimate convention, priority levels, and the availability-window
  (occupancy schedule) config and free-time calculation.
- `ical.py` — fetches/parses configured iCal feeds (commonly a Google Calendar secret address, but
  any iCal feed works) and expands recurring events; caches parsed calendars in memory.
- `formatting.py` — renders tasks/events into Telegram message text (HTML parse mode) and inline keyboards.
- `telegram_api.py` — thin Telegram Bot API wrapper (`_telegram_call`) + due-notification message builder.
- `notify.py` — webhook receiver (push path for `task.overdue`/`tasks.overdue`), the reconciliation
  safety-net pass (main loop, catches missed webhooks), and the once-a-day digest.
- Bot flows (each a two-step callback: pick a task, then pick a value), one module per flow:
  `new_task_flow.py`, `punt_flow.py` (postpone due date), `estimate_priority_flow.py`, `time_entry_flow.py`
  (plain-text "HH:MM"/"D/M[/Y]" replies typed instead of pressing a button), `undo.py`.
- `callback.py` — dispatches inline-keyboard presses to the flow modules; also handles the simple
  one-shot actions (done/delete/snooze/tarjeta) inline.
- `commands.py` — dispatches slash commands (`/hoy`, `/carga`, `/disponibilidad`, `/calendario`, ...).
- `poll.py` — the `getUpdates` long-poll loop that feeds `callback.py`/`commands.py`.

## Conventions worth knowing before editing

- **23:59 local time = "no specific time" sentinel.** A task due "sometime today" with no explicit
  time is stored as 23:59 local (converted to UTC) since Vikunja has no native date-only due field.
  `_task_due_dt` returns `None` for these (never "overdue" by time, never shown with a clock time);
  `_task_local_date` still returns the calendar day. See the docstrings on both in `vikunja.py`.
- **Undo pattern**: every mutating flow snapshots the pre-change task into `UNDO_STATE_FILE` keyed by
  task id (`{"action": ..., "task": <pre-change task dict>}`) before applying the change, and attaches
  a "↩️ Deshacer" button. `undo.py` reads the snapshot back and knows how to reverse each action kind.
- **`_state_lock`** must wrap any read-modify-write of a state JSON file that more than one thread
  can touch (webhook thread, poll thread, main reconciliation loop) — see existing call sites for the pattern.
- Vikunja's task update PATCH resets any field omitted from the body, so `_vk_task_update` always
  does fetch-merge-write, never a naive partial update.

## Tests

`tests/` uses stdlib `unittest` (no pytest dependency). Run with:

```
python3 -m unittest discover -s tests
```

Covers the pure-logic pieces (date/time parsing, estimate parsing, availability windows, webhook
signature verification, message formatting) with mocked I/O — no real Vikunja/Telegram calls.
