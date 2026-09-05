"""Slash-command handling: /hoy, /mañana, /day, /carga, /calendario,
/disponibilidad, /hecho, /borrar, /tarjeta, /estimar, /sinestimar, /prioridad,
/punt, and the plain-message fallback that starts the new-task or
time-entry flow."""

from __future__ import annotations

import html
from datetime import date, timedelta

import requests

from . import config
from . import formatting
from . import ical
from . import vikunja as vk
from .estimate_priority_flow import _handle_estimate_text
from .new_task_flow import _handle_new_task_emoji_text, _start_new_task
from .state import _load_json, _save_json, _state_lock
from .telegram_api import _telegram_call
from .time_entry_flow import _handle_time_entry


def _handle_message(message: dict) -> None:
    chat_id = message.get("chat", {}).get("id")
    if str(chat_id) != str(config.CHAT_ID):
        return
    text = (message.get("text") or "").strip()
    if not text:
        return

    parts = text.split(maxsplit=1)
    command = parts[0].split("@", 1)[0]
    arg = parts[1].strip() if len(parts) > 1 else ""

    if command in ("/hoy", "/today"):
        try:
            tasks = vk._today_tasks()
            overdue = vk._overdue_tasks()
        except requests.RequestException as e:
            config.log.error("Could not fetch today's tasks: %s", e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        events = ical._calendar_events_for_day(date.today())
        text, keyboard = formatting._format_day_message(tasks, "hoy", overdue=overdue, day=date.today(), events=events)
        _telegram_call(
            "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard,
        )
        config.log.info(
            "Sent today's task list (%d task(s), %d overdue) to chat %s",
            len(tasks), len(overdue), chat_id,
        )
        return

    if command in ("/mañana", "/tomorrow"):
        try:
            tasks = vk._tomorrow_tasks()
        except requests.RequestException as e:
            config.log.error("Could not fetch tomorrow's tasks: %s", e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        tomorrow = date.today() + timedelta(days=1)
        events = ical._calendar_events_for_day(tomorrow)
        text, keyboard = formatting._format_day_message(tasks, "mañana", day=tomorrow, events=events)
        _telegram_call(
            "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard,
        )
        config.log.info("Sent tomorrow's task list (%d task(s)) to chat %s", len(tasks), chat_id)
        return

    if command in ("/day", "/dia"):
        if not arg:
            _telegram_call(
                "sendMessage", chat_id=chat_id, text="¿Qué día?",
                reply_markup=formatting._day_picker_keyboard(),
            )
            return

        target = vk._parse_day_arg(arg)
        if target is None:
            _telegram_call(
                "sendMessage", chat_id=chat_id,
                text="Usá /day DD/MM (o YYYY-MM-DD, 'hoy', 'mañana'). Ej: /day 20/08",
            )
            return

        try:
            tasks = vk._tasks_for_date(target)
        except requests.RequestException as e:
            config.log.error("Could not fetch tasks for %s: %s", target, e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        label = target.strftime("%Y-%m-%d")
        events = ical._calendar_events_for_day(target)
        text, keyboard = formatting._format_day_message(tasks, label, day=target, events=events)
        _telegram_call(
            "sendMessage", chat_id=chat_id, text=text, parse_mode="HTML", reply_markup=keyboard,
        )
        config.log.info("Sent task list for %s (%d task(s)) to chat %s", label, len(tasks), chat_id)
        return

    if command in ("/carga", "/semana"):
        days = 14
        if arg:
            try:
                days = int(arg)
            except ValueError:
                _telegram_call(
                    "sendMessage", chat_id=chat_id,
                    text="Usá /carga [días]. Ej: /carga o /carga 21",
                )
                return
        days = max(1, min(days, 30))

        start = date.today()
        try:
            by_date = vk._tasks_by_date(start, days)
        except requests.RequestException as e:
            config.log.error("Could not fetch tasks for /carga: %s", e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        events_by_date = ical._calendar_events_by_date(start, days)
        _telegram_call(
            "sendMessage", chat_id=chat_id,
            text=formatting._format_load_message(by_date, start, days, events_by_date=events_by_date), parse_mode="HTML"
        )
        config.log.info("Sent %d-day load overview to chat %s", days, chat_id)
        return

    if command in ("/calendario", "/cal"):
        urls = ical._calendar_urls()
        emails = ical._calendar_emails()
        args = arg.split(maxsplit=1) if arg else []

        calendario_help = (
            "Calendarios — /calendario agregar &lt;url-ics&gt; (la dirección secreta en formato iCal: "
            "Google Calendar → Configuración del calendario → Integrar calendario) · "
            "/calendario borrar &lt;número&gt;.\n"
            "Emails para RSVP — /calendario email agregar &lt;dirección&gt; · "
            "/calendario email borrar &lt;número&gt; (detectan eventos que rechazaste, en cualquiera "
            "de tus calendarios; solo hace falta más de uno si asistís con más de una identidad).\n"
            "/calendario actualizar fuerza releer los feeds ahora."
        )

        if not args:
            lines = ["<b>Calendarios configurados</b>", ""]
            if urls:
                for i, u in enumerate(urls, 1):
                    lines.append(f"{i}. {html.escape(u)}")
            else:
                lines.append("(ninguno)")
            lines.append("")
            lines.append("<b>Emails para RSVP</b>")
            if emails:
                for i, e in enumerate(emails, 1):
                    lines.append(f"{i}. {html.escape(e)}")
            else:
                lines.append("(ninguno)")
            lines.append("")
            lines.append(calendario_help)
            _telegram_call("sendMessage", chat_id=chat_id, text="\n".join(lines), parse_mode="HTML")
            return

        if args[0] == "agregar":
            if len(args) < 2 or not args[1].strip():
                _telegram_call("sendMessage", chat_id=chat_id, text="Usá /calendario agregar <url-ics>.")
                return
            url = args[1].strip()
            with _state_lock:
                cfg = _load_json(config.GOOGLE_CALENDAR_STATE_FILE, {"urls": [], "emails": []})
                if url not in cfg["urls"]:
                    cfg["urls"].append(url)
                    _save_json(config.GOOGLE_CALENDAR_STATE_FILE, cfg)
            _telegram_call("sendMessage", chat_id=chat_id, text="✓ Calendario agregado.")
            config.log.info("Added calendar for chat %s", chat_id)
            return

        if args[0] == "borrar":
            if len(args) < 2:
                _telegram_call(
                    "sendMessage", chat_id=chat_id,
                    text="Usá /calendario borrar <número> (ver /calendario para la lista).",
                )
                return
            try:
                idx = int(args[1].strip()) - 1
            except ValueError:
                idx = -1
            if idx < 0 or idx >= len(urls):
                _telegram_call(
                    "sendMessage", chat_id=chat_id,
                    text="No entendí el número. Usá /calendario para ver la lista.",
                )
                return
            with _state_lock:
                cfg = _load_json(config.GOOGLE_CALENDAR_STATE_FILE, {"urls": [], "emails": []})
                if 0 <= idx < len(cfg["urls"]):
                    cfg["urls"].pop(idx)
                    _save_json(config.GOOGLE_CALENDAR_STATE_FILE, cfg)
            _telegram_call("sendMessage", chat_id=chat_id, text="✓ Calendario eliminado.")
            config.log.info("Removed calendar %d for chat %s", idx, chat_id)
            return

        if args[0] == "email":
            # Same agregar/borrar-by-index interface as the top-level
            # calendar commands above, just scoped to this second
            # collection. Emails are shared across all calendars, not
            # per-calendar: only relevant if you attend meetings under
            # more than one identity (e.g. a personal Gmail plus a work
            # account) — an address that never shows up as an ATTENDEE
            # just won't match, in any feed.
            sub = args[1].split(maxsplit=1) if len(args) > 1 else []

            if sub and sub[0] == "agregar":
                if len(sub) < 2 or "@" not in sub[1]:
                    _telegram_call("sendMessage", chat_id=chat_id, text="Usá /calendario email agregar <dirección>.")
                    return
                email = sub[1].strip()
                with _state_lock:
                    cfg = _load_json(config.GOOGLE_CALENDAR_STATE_FILE, {"urls": [], "emails": []})
                    cfg_emails = cfg.setdefault("emails", [])
                    if email.lower() not in {e.lower() for e in cfg_emails}:
                        cfg_emails.append(email)
                        _save_json(config.GOOGLE_CALENDAR_STATE_FILE, cfg)
                _telegram_call("sendMessage", chat_id=chat_id, text="✓ Email agregado.")
                config.log.info("Added calendar RSVP email for chat %s", chat_id)
                return

            if sub and sub[0] == "borrar":
                if len(sub) < 2:
                    _telegram_call(
                        "sendMessage", chat_id=chat_id,
                        text="Usá /calendario email borrar <número> (ver /calendario para la lista).",
                    )
                    return
                try:
                    idx = int(sub[1].strip()) - 1
                except ValueError:
                    idx = -1
                if idx < 0 or idx >= len(emails):
                    _telegram_call(
                        "sendMessage", chat_id=chat_id,
                        text="No entendí el número. Usá /calendario para ver la lista.",
                    )
                    return
                with _state_lock:
                    cfg = _load_json(config.GOOGLE_CALENDAR_STATE_FILE, {"urls": [], "emails": []})
                    if 0 <= idx < len(cfg.get("emails", [])):
                        cfg["emails"].pop(idx)
                        _save_json(config.GOOGLE_CALENDAR_STATE_FILE, cfg)
                _telegram_call("sendMessage", chat_id=chat_id, text="✓ Email eliminado.")
                config.log.info("Removed calendar RSVP email %d for chat %s", idx, chat_id)
                return

            _telegram_call(
                "sendMessage", chat_id=chat_id,
                text="Usá /calendario email agregar <dirección> o /calendario email borrar <número>.",
            )
            return

        if args[0] == "actualizar":
            ical._clear_ics_cache()
            _telegram_call("sendMessage", chat_id=chat_id, text="✓ Cache de calendarios vaciada, se releen en la próxima consulta.")
            config.log.info("Flushed calendar ICS cache for chat %s", chat_id)
            return

        _telegram_call("sendMessage", chat_id=chat_id, text=calendario_help, parse_mode="HTML")
        return

    if command in ("/disponibilidad", "/disp"):
        if not arg:
            _telegram_call(
                "sendMessage", chat_id=chat_id, text=vk._format_availability_config(), parse_mode="HTML"
            )
            return

        args = arg.split(maxsplit=1)

        if args[0] == "borrar":
            if len(args) < 2:
                _telegram_call(
                    "sendMessage", chat_id=chat_id,
                    text="Usá /disponibilidad borrar <general|día|fecha>. Ej: /disponibilidad borrar martes",
                )
                return
            target = vk._parse_availability_target(args[1])
            if target is None:
                _telegram_call(
                    "sendMessage", chat_id=chat_id,
                    text="No entendí. Usá 'general', un día de la semana, o YYYY-MM-DD.",
                )
                return
            kind, key = target
            with _state_lock:
                cfg = _load_json(config.AVAILABILITY_STATE_FILE, {"weekday": {}, "date": {}, "general": None})
                if kind == "general":
                    cfg["general"] = None
                else:
                    cfg.setdefault(kind, {}).pop(key, None)
                _save_json(config.AVAILABILITY_STATE_FILE, cfg)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"✓ Disponibilidad de {args[1]} eliminada.")
            config.log.info("Cleared availability override %s=%s for chat %s", kind, key, chat_id)
            return

        if len(args) != 2:
            _telegram_call(
                "sendMessage", chat_id=chat_id,
                text=(
                    "Usá /disponibilidad <general|día|fecha> <HH:MM-HH:MM>. "
                    "Ej: /disponibilidad general 09:00-18:00, /disponibilidad martes 09:00-13:00, "
                    "/disponibilidad 2026-08-21 10:00-16:00"
                ),
            )
            return

        target = vk._parse_availability_target(args[0])
        window = vk._parse_time_range(args[1])
        if target is None or window is None:
            _telegram_call(
                "sendMessage", chat_id=chat_id,
                text=(
                    "No entendí. Usá /disponibilidad <general|día|fecha> <HH:MM-HH:MM>. "
                    "Ej: /disponibilidad general 09:00-18:00, /disponibilidad martes 09:00-13:00, "
                    "/disponibilidad 2026-08-21 10:00-16:00"
                ),
            )
            return

        kind, key = target
        window_iso = [window[0].strftime("%H:%M"), window[1].strftime("%H:%M")]
        with _state_lock:
            cfg = _load_json(config.AVAILABILITY_STATE_FILE, {"weekday": {}, "date": {}, "general": None})
            if kind == "general":
                cfg["general"] = window_iso
            else:
                cfg.setdefault(kind, {})[key] = window_iso
            _save_json(config.AVAILABILITY_STATE_FILE, cfg)

        label = vk._WEEKDAY_DISPLAY[int(key)] if kind == "weekday" else key
        _telegram_call(
            "sendMessage", chat_id=chat_id, text=f"✓ Disponibilidad de {label}: {vk._format_time_range(window)}"
        )
        config.log.info("Set availability %s=%s to %s for chat %s", kind, key, window_iso, chat_id)
        return

    if command == "/hecho":
        try:
            tasks = vk._today_tasks()
        except requests.RequestException as e:
            config.log.error("Could not fetch today's tasks: %s", e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        if not tasks:
            _telegram_call("sendMessage", chat_id=chat_id, text="🎉 No hay tareas pendientes hoy.")
            return

        _telegram_call(
            "sendMessage", chat_id=chat_id, text="¿Qué tarea marcamos como hecha?",
            reply_markup=formatting._task_picker_keyboard(tasks, "done"),
        )
        config.log.info("Sent /hecho task picker (%d task(s)) to chat %s", len(tasks), chat_id)
        return

    if command == "/borrar":
        try:
            tasks = vk._today_tasks()
        except requests.RequestException as e:
            config.log.error("Could not fetch today's tasks: %s", e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        if not tasks:
            _telegram_call("sendMessage", chat_id=chat_id, text="🎉 No hay tareas pendientes hoy.")
            return

        _telegram_call(
            "sendMessage", chat_id=chat_id, text="¿Qué tarea borramos?",
            reply_markup=formatting._task_picker_keyboard(tasks, "delete"),
        )
        config.log.info("Sent /borrar task picker (%d task(s)) to chat %s", len(tasks), chat_id)
        return

    if command == "/tarjeta":
        try:
            tasks = vk._today_tasks()
        except requests.RequestException as e:
            config.log.error("Could not fetch today's tasks: %s", e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        if not tasks:
            _telegram_call("sendMessage", chat_id=chat_id, text="🎉 No hay tareas pendientes hoy.")
            return

        _telegram_call(
            "sendMessage", chat_id=chat_id, text=f'¿Qué tarea marcamos "{vk._TARJETA_LABEL_TITLE}"?',
            reply_markup=formatting._task_picker_keyboard(tasks, "tarjeta"),
        )
        config.log.info("Sent /tarjeta task picker (%d task(s)) to chat %s", len(tasks), chat_id)
        return

    if command == "/estimar":
        try:
            tasks = vk._today_tasks()
        except requests.RequestException as e:
            config.log.error("Could not fetch today's tasks: %s", e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        if not tasks:
            _telegram_call("sendMessage", chat_id=chat_id, text="🎉 No hay tareas pendientes hoy.")
            return

        _telegram_call(
            "sendMessage", chat_id=chat_id, text="¿Qué tarea querés estimar?",
            reply_markup=formatting._task_picker_keyboard(tasks, "estim"),
        )
        config.log.info("Sent /estimar task picker (%d task(s)) to chat %s", len(tasks), chat_id)
        return

    if command == "/sinestimar":
        try:
            task = vk._next_unestimated_task()
        except requests.RequestException as e:
            config.log.error("Could not fetch next unestimated task: %s", e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        if task is None:
            _telegram_call("sendMessage", chat_id=chat_id, text="🎉 No hay tareas sin estimar.")
            return

        sent = _telegram_call(
            "sendMessage", chat_id=chat_id, text=f"⏱ {task['title']}\n¿Cuánto estimás que dura?",
            reply_markup=vk._estimate_duration_keyboard(task["id"]),
        )
        with _state_lock:
            estimate_state = _load_json(config.PENDING_ESTIMATE_STATE_FILE, {})
            estimate_state[str(chat_id)] = {"task_id": task["id"], "message_id": sent["message_id"]}
            _save_json(config.PENDING_ESTIMATE_STATE_FILE, estimate_state)
        config.log.info("Sent /sinestimar prompt for task %s to chat %s", task["id"], chat_id)
        return

    if command == "/prioridad":
        try:
            tasks = vk._today_tasks()
        except requests.RequestException as e:
            config.log.error("Could not fetch today's tasks: %s", e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        if not tasks:
            _telegram_call("sendMessage", chat_id=chat_id, text="🎉 No hay tareas pendientes hoy.")
            return

        _telegram_call(
            "sendMessage", chat_id=chat_id, text="¿A qué tarea le cambiamos la prioridad?",
            reply_markup=formatting._task_picker_keyboard(tasks, "prio"),
        )
        config.log.info("Sent /prioridad task picker (%d task(s)) to chat %s", len(tasks), chat_id)
        return

    if command in ("/punt", "/postergar"):
        try:
            tasks = vk._overdue_tasks() + vk._today_tasks()
        except requests.RequestException as e:
            config.log.error("Could not fetch tasks for /punt: %s", e)
            _telegram_call("sendMessage", chat_id=chat_id, text=f"Error: {e}")
            return

        if not tasks:
            _telegram_call("sendMessage", chat_id=chat_id, text="🎉 No hay tareas vencidas ni de hoy para posponer.")
            return

        _telegram_call(
            "sendMessage", chat_id=chat_id, text="¿Qué tarea posponemos?",
            reply_markup=formatting._task_picker_keyboard(
                tasks, "punt", label_fn=lambda t, pm: formatting._punt_button_label(t, pm, date.today())
            ),
        )
        config.log.info("Sent /punt task picker (%d task(s)) to chat %s", len(tasks), chat_id)
        return

    if text.startswith("/"):
        return

    pending_task_state = _load_json(config.PENDING_TASK_STATE_FILE, {})
    pending_task = pending_task_state.get(str(chat_id))
    if pending_task and "project_ids" not in pending_task:
        _handle_new_task_emoji_text(chat_id, text, pending_task)
        return

    pending_time_state = _load_json(config.PENDING_TIME_STATE_FILE, {})
    pending_time = pending_time_state.get(str(chat_id))
    if pending_time:
        _handle_time_entry(chat_id, text, pending_time)
        return

    pending_estimate_state = _load_json(config.PENDING_ESTIMATE_STATE_FILE, {})
    pending_estimate = pending_estimate_state.get(str(chat_id))
    if pending_estimate:
        _handle_estimate_text(chat_id, text, pending_estimate)
        return

    _start_new_task(chat_id, text)
