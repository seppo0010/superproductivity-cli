"""Subte (Buenos Aires underground) status check for calendar trips.

A calendar event whose description has a `subte: D` line (several lines
comma-separated: `subte: D, C`) is a trip you plan to take by subte, and its
start time is when you have to leave. SUBTE_PRE_MINUTES before that, the
line status is checked and you're alerted only if there's a problem (so
there's time to leave earlier by bus); at the start time it's checked again
and a message is always sent — "no delays, time to go" or the problem.

Status comes from Emova's public status page, which is fed by an
unauthenticated SignalR 2 hub (no documented API; the same channel the page
itself uses). Emova drops connections from Tor exit nodes, so this is a
plain direct request.
"""

from __future__ import annotations

import html
import json
import re
from datetime import date, datetime, timedelta
from html.parser import HTMLParser
from typing import Optional

import requests

from . import config
from . import ical
from .state import _load_json, _save_json, _state_lock
from .telegram_api import _telegram_call

SIGNALR_BASE = "https://aplicacioneswp.metrovias.com.ar/estadolineasEMOVA/signalr"
SIGNALR_HUB = "SignalREmova"
SIGNALR_EVENT = "estadoLineas"

# Substrings (lowercased, accent-stripped) that make a line's status text an
# actual service problem. Anything else that isn't "Normal" (closed stations
# for works, extended hours...) is informational: shown, but not alarming.
_PROBLEM_RE = re.compile(
    r"\b(demora|interrump|interrupc|limitad|sin servicio|suspend|frecuencia"
    r"|medida de fuerza|paro\b|reducid|incidente|no funciona)"
)

_TAG_RE = re.compile(r"(?im)^\s*subte\s*:\s*(.+?)\s*$")


class _StatusHTMLParser(HTMLParser):
    """Pairs each `<img alt="Linea X">` with the `<p>` status text after it."""

    def __init__(self):
        super().__init__()
        self.statuses: dict = {}
        self._line: Optional[str] = None
        self._in_p = False
        self._text: list = []

    def handle_starttag(self, tag, attrs):
        if tag == "img":
            alt = dict(attrs).get("alt") or ""
            if alt.lower().startswith("linea "):
                self._line = _normalize_line(alt[len("linea "):])
        elif tag == "p" and self._line:
            self._in_p = True
            self._text = []

    def handle_data(self, data):
        if self._in_p:
            self._text.append(data)

    def handle_endtag(self, tag):
        if tag == "p" and self._in_p:
            self.statuses[self._line] = " ".join("".join(self._text).split())
            self._in_p = False
            self._line = None


def _parse_status_html(markup: str) -> dict:
    parser = _StatusHTMLParser()
    parser.feed(markup)
    return parser.statuses


def _normalize_line(name: str) -> str:
    name = name.strip()
    return name.upper() if len(name) == 1 else name.capitalize()


def fetch_line_statuses(timeout: int = 15) -> dict:
    """{"A": "Normal", "D": "...", "Premetro": ...} straight from Emova.
    Raises requests.RequestException / ValueError on failure."""
    session = requests.Session()
    params = {
        "clientProtocol": "2.0",
        "connectionData": json.dumps([{"name": SIGNALR_HUB}], separators=(",", ":")),
    }
    resp = session.get(f"{SIGNALR_BASE}/negotiate", params=params, timeout=timeout)
    resp.raise_for_status()
    token = resp.json().get("ConnectionToken")
    if not token:
        raise ValueError("Emova negotiate returned no ConnectionToken")
    transport = {**params, "transport": "serverSentEvents", "connectionToken": token}
    stream = session.get(f"{SIGNALR_BASE}/connect", params=transport, stream=True, timeout=(timeout, timeout))
    try:
        stream.raise_for_status()
        stream.encoding = "utf-8"
        session.get(f"{SIGNALR_BASE}/start", params=transport, timeout=timeout).raise_for_status()
        for line in stream.iter_lines(decode_unicode=True):
            if not line or not line.startswith("data:"):
                continue
            try:
                payload = json.loads(line[len("data:"):].strip())
            except json.JSONDecodeError:
                continue
            if not isinstance(payload, dict):
                continue
            for msg in payload.get("M", []):
                if msg.get("H", "").lower() == SIGNALR_HUB.lower() and msg.get("M") == SIGNALR_EVENT:
                    args = msg.get("A") or []
                    statuses = _parse_status_html(args[0]) if args and isinstance(args[0], str) else {}
                    if statuses:
                        return statuses
        raise ValueError("Emova stream ended without a line status snapshot")
    finally:
        stream.close()


def parse_subte_tag(description: str) -> list:
    """Line names from a `subte: D, C` line in an event description (Google
    Calendar may hand the description over as HTML, so tags are stripped)."""
    if not description:
        return []
    text = re.sub(r"(?i)<br\s*/?>|</p>|</div>", "\n", description)
    text = html.unescape(re.sub(r"<[^>]+>", "", text))
    m = _TAG_RE.search(text)
    if not m:
        return []
    return [_normalize_line(p) for p in re.split(r"[,\s]+", m.group(1)) if p]


def _strip_accents(s: str) -> str:
    return s.translate(str.maketrans("áéíóúü", "aeiouu"))


def classify(status_text: Optional[str]) -> str:
    """"ok" or "problem". An unknown line (None) counts as a problem."""
    if status_text is None:
        return "problem"
    t = _strip_accents(status_text.lower())
    if t == "normal":
        return "ok"
    return "problem" if _PROBLEM_RE.search(t) else "ok"


def _lines_report(lines: list, statuses: Optional[dict]) -> tuple:
    """(has_problem, message lines) for the given subte lines."""
    if statuses is None:
        return True, ["⚠️ No pude verificar el estado del subte."]
    problem = False
    out = []
    for line in lines:
        text = statuses.get(line)
        if classify(text) == "problem":
            problem = True
            out.append(f"🚨 Línea {html.escape(line)}: {html.escape(text or 'sin información')}")
        elif text and text.lower() != "normal":
            out.append(f"ℹ️ Línea {html.escape(line)}: {html.escape(text)}")
    return problem, out


def _send(text: str) -> None:
    try:
        _telegram_call("sendMessage", chat_id=config.CHAT_ID, text=text, parse_mode="HTML")
    except (requests.RequestException, RuntimeError) as e:
        config.log.error("Failed to send subte message: %s", e)


def check_subte_trips(now: Optional[datetime] = None) -> Optional[int]:
    """Runs the pre-departure and departure checks due right now for today's
    subte-tagged events. Returns seconds until the next checkpoint today (so
    the main loop can wake up on time), or None if there's none left."""
    now = now or datetime.now()
    today = now.date()
    pre = timedelta(minutes=config.SUBTE_PRE_MINUTES)
    grace = timedelta(minutes=config.SUBTE_GRACE_MINUTES)

    trips = []
    for e in ical._calendar_events_for_day(today, max_age=config.SUBTE_CALENDAR_MAX_AGE):
        if e["all_day"] or e["start"].date() != today:
            continue
        lines = parse_subte_tag(e.get("description", ""))
        if lines:
            trips.append((e, lines))

    statuses_cache: list = []  # fetched at most once per pass, lazily

    def statuses() -> Optional[dict]:
        if not statuses_cache:
            try:
                statuses_cache.append(fetch_line_statuses())
            except (requests.RequestException, ValueError) as err:
                config.log.warning("Could not fetch subte status from Emova: %s", err)
                statuses_cache.append(None)
        return statuses_cache[0]

    next_in = None
    with _state_lock:
        state = _load_json(config.SUBTE_STATE_FILE, {})
        # Keys are "<uid>|<start iso>"; drop anything from a previous day.
        state = {k: v for k, v in state.items() if k.rsplit("|", 1)[-1][:10] == today.isoformat()}

        for e, lines in trips:
            start = e["start"]
            key = f"{e.get('uid', '')}|{start.isoformat()}"
            entry = state.setdefault(key, {})
            title = html.escape(e["title"])
            when = start.strftime("%H:%M")
            lines_label = ", ".join(lines)

            if start - pre <= now < start and not entry.get("pre"):
                problem, report = _lines_report(lines, statuses())
                if problem:
                    _send("\n".join(report) + f"\n\nConsiderá salir ya en colectivo — «{title}» a las {when}.")
                entry["pre"] = True
                entry["pre_problem"] = problem
            if start <= now < start + grace and not entry.get("depart"):
                problem, report = _lines_report(lines, statuses())
                if problem:
                    head = "Sigue el problema en el subte." if entry.get("pre_problem") else "Apareció un problema en el subte."
                    _send(f"{head}\n" + "\n".join(report) + f"\n\n«{title}» ({when}).")
                else:
                    _send(f"✅ Subte {html.escape(lines_label)} sin demoras. Hora de salir para «{title}»."
                          + ("\n" + "\n".join(report) if report else ""))
                entry["depart"] = True
                entry["pre"] = True

            for checkpoint, done in ((start - pre, entry.get("pre")), (start, entry.get("depart"))):
                if not done and checkpoint > now:
                    seconds = (checkpoint - now).total_seconds()
                    if next_in is None or seconds < next_in:
                        next_in = seconds

        _save_json(config.SUBTE_STATE_FILE, state)

    return None if next_in is None else int(next_in) + 1
