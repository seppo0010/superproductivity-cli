"""iCal calendar integration (commonly a Google Calendar secret address, but
any iCal feed works): fetches/parses the configured feed(s) and expands
recurring events into concrete occurrences."""

from __future__ import annotations

import threading
import time
from datetime import date, datetime, time as dtime, timedelta

import icalendar
import recurring_ical_events
import requests

from . import config
from .state import _load_json

_ics_cache: dict = {}
_ics_cache_lock = threading.Lock()


def _calendar_urls() -> list:
    return _load_json(config.GOOGLE_CALENDAR_STATE_FILE, {"urls": [], "emails": []}).get("urls", [])


def _calendar_emails() -> list:
    """Your own email address(es), used to find "your own" ATTENDEE entry
    on an event so declined invites (RSVP) can be filtered out. Not tied to
    a particular calendar — a feed that doesn't list one of these as an
    attendee simply won't match, so it's safe to check all of them against
    every configured calendar. Only relevant if you attend meetings under
    more than one address (e.g. a personal Gmail plus a work account);
    empty means nothing gets filtered on RSVP status."""
    return _load_json(config.GOOGLE_CALENDAR_STATE_FILE, {"urls": [], "emails": []}).get("emails", [])


def _declined_by(component, emails: list) -> bool:
    """True if any of `emails` is an ATTENDEE on this event with
    PARTSTAT=DECLINED. Google's private iCal feed still includes events
    you've declined, so this is the only way to filter them back out."""
    if not emails:
        return False
    attendees = component.get("attendee")
    if attendees is None:
        return False
    if not isinstance(attendees, list):
        attendees = [attendees]
    emails_lower = {e.strip().lower() for e in emails}
    for att in attendees:
        addr = str(att).lower()
        if addr.startswith("mailto:"):
            addr = addr[len("mailto:"):]
        if addr in emails_lower and str(att.params.get("PARTSTAT", "")).upper() == "DECLINED":
            return True
    return False


def _fetch_calendar(url: str) -> "icalendar.Calendar":
    """Fetches and parses a calendar feed, caching the *parsed* result for
    GOOGLE_CALENDAR_CACHE_SECONDS. Google's private iCal export includes a
    calendar's entire history, so re-parsing the feed text (not just
    re-fetching it over the network) on every command was the real cost —
    caching only the raw bytes still left that parse on the hot path."""
    now = time.time()
    with _ics_cache_lock:
        cached = _ics_cache.get(url)
        if cached and now - cached[0] < config.GOOGLE_CALENDAR_CACHE_SECONDS:
            return cached[1]
    resp = requests.get(url, timeout=15)
    resp.raise_for_status()
    cal = icalendar.Calendar.from_ical(resp.content)
    with _ics_cache_lock:
        _ics_cache[url] = (now, cal)
    return cal


def _clear_ics_cache() -> None:
    with _ics_cache_lock:
        _ics_cache.clear()


def _calendar_events_for_range(start: date, end: date) -> list:
    """Events overlapping [start, end) across all configured calendars,
    normalized to local wall-clock datetimes and sorted by start time. Skips
    (and logs) any calendar whose feed can't be fetched/parsed rather than
    failing the whole call."""
    events = []
    emails = _calendar_emails()
    for url in _calendar_urls():
        try:
            cal = _fetch_calendar(url)
            occurrences = recurring_ical_events.of(cal).between(start, end)
        except Exception as e:
            config.log.warning("Could not fetch/parse calendar %s: %s", url, e)
            continue
        for component in occurrences:
            if _declined_by(component, emails):
                continue
            dtstart = component.get("dtstart").dt
            dtend_prop = component.get("dtend")
            dtend = dtend_prop.dt if dtend_prop else dtstart
            all_day = not isinstance(dtstart, datetime)
            if all_day:
                start_dt = datetime.combine(dtstart, dtime.min)
                end_dt = datetime.combine(dtend, dtime.min)
            else:
                start_dt = dtstart.astimezone().replace(tzinfo=None) if dtstart.tzinfo else dtstart
                end_dt = dtend.astimezone().replace(tzinfo=None) if dtend.tzinfo else dtend
            transp = str(component.get("transp", "OPAQUE")).upper()
            events.append({
                "title": str(component.get("summary", "(sin título)")),
                "start": start_dt,
                "end": end_dt,
                "all_day": all_day,
                "busy": transp != "TRANSPARENT",
            })
    events.sort(key=lambda e: e["start"])
    return events


def _calendar_events_for_day(day: date) -> list:
    return _calendar_events_for_range(day, day + timedelta(days=1))


def _calendar_events_by_date(start: date, days: int) -> dict:
    """Events within [start, start+days-1], grouped by local calendar day."""
    end = start + timedelta(days=days)
    by_date: dict = {}
    for e in _calendar_events_for_range(start, end):
        by_date.setdefault(e["start"].date(), []).append(e)
    return by_date
