"""Today's meetings in a system's own day (G3-AC1 to G3-AC4).

`read(client, user, zone, clock, naive_zone=None)` returns a `PartResult` holding a `MeetingsPart`, or a plain reason.
Server-side date filters only narrow the fetch; the overlap check here decides what is shown.

Event row fields, probed live on both systems (2026-11-06), are read in the helpers at the top of
this file only: `title`, `start_at`, `end_at`, `status`, `organizer_email`, `timezone`, the boolean
`all_day` and `attendees` as [{email, status}] (status `declined` is lowercase). The platform stores
offset-less LOCAL wall-clock text, date-only text for all-day events (exclusive end date), and its
`lt:`/`gt:` filters compare that text as a string.
"""
from dataclasses import dataclass, field
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from .mcp import LoginExpired, McpError, PartResult, failure_message

ALL_DAY_FIELDS = ("all_day",)
CANCELLED = ("cancelled", "canceled")


@dataclass
class Meeting:
    id: str
    title: str
    start: datetime          # aware, in the system's zone (all-day events: start of their first day)
    end: datetime
    all_day: bool
    organizer: str           # organizer_email, or "—"
    day: date                # the briefing day, for the time text
    clash: bool = False
    declined: bool = False

    @property
    def time_text(self):
        if self.all_day:
            return "all day"

        def clock(moment):
            return moment.strftime("%H:%M" if moment.date() == self.day else "%m-%d %H:%M")
        return "%s–%s %s" % (clock(self.start), clock(self.end), self.start.tzname())


@dataclass
class MeetingsPart:
    day: date
    meetings: list = field(default_factory=list)


# ---- row reading (the probed shapes live here)

def _server_filters(day):
    """SD-D-9, as probed: the server compares the stored text, which mixes local times, date-only
    all-day dates and offsets, so an instant would wrongly drop rows. A date-only window with a day of
    slack each side is a superset of the day; the overlap check decides."""
    return {"start_at": "lt:" + (day + timedelta(days=2)).isoformat(),
            "end_at": "gte:" + (day - timedelta(days=1)).isoformat()}


def _row_zone(row, default):
    """The zone offset-less times of this row are local to: its `timezone`, else `default`, the platform's
    own zone (never a display override)."""
    name = row.get("timezone")
    if isinstance(name, str) and name.strip():
        try:
            return ZoneInfo(name.strip())
        except (ZoneInfoNotFoundError, ValueError, OSError):
            pass
    return default


def _parse(text, naive_zone):
    """An ISO 8601 timestamp as an aware datetime; no offset means local time in `naive_zone`.
    None when unreadable."""
    if not isinstance(text, str) or not text:
        return None
    try:
        moment = datetime.fromisoformat(text.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return moment if moment.tzinfo else moment.replace(tzinfo=naive_zone)


def _date_part(text):
    try:
        return date.fromisoformat(text[:10]) if isinstance(text, str) else None
    except ValueError:
        return None


def _is_all_day(row):
    return any(bool(row.get(name)) for name in ALL_DAY_FIELDS)


def _is_cancelled(row):
    return str(row.get("status") or "").strip().lower() in CANCELLED


def _declined_by(row, email):
    wanted = (email or "").strip().lower()
    if not wanted:
        return False
    for who in row.get("attendees") or []:
        if isinstance(who, dict) and str(who.get("email") or "").strip().lower() == wanted \
                and str(who.get("status") or "").strip().lower() == "declined":
            return True
    return False


def _all_day_span(row):
    """First and last date of an all-day event, by date. A date-only end (the live shape) or an end of
    exactly midnight is exclusive; an end of 23:59:59 is the same day."""
    first = _date_part(row.get("start_at"))
    if first is None:
        return None
    end_text = row.get("end_at")
    last = _date_part(end_text) or first
    if last > first and isinstance(end_text, str) and end_text[10:19] in ("", "T00:00:00", " 00:00:00"):
        last -= timedelta(days=1)
    return first, max(first, last)


def _meeting(row, zone, naive_zone, day, day_start, day_end, email):
    """A Meeting if the event overlaps the day, else None. Times are read in the row's own zone, else
    `naive_zone`, and shown in `zone`."""
    title = str(row.get("title") or "").strip() or "(no title)"
    organizer = str(row.get("organizer_email") or "").strip() or "—"
    common = dict(id=str(row.get("id")), title=title, organizer=organizer, day=day)
    if _is_all_day(row):
        span = _all_day_span(row)
        if span is None or not span[0] <= day <= span[1]:
            return None
        start = datetime.combine(span[0], time.min, tzinfo=zone)
        return Meeting(start=start, end=datetime.combine(span[1] + timedelta(days=1), time.min, tzinfo=zone),
                       all_day=True, declined=_declined_by(row, email), **common)
    local = _row_zone(row, naive_zone)
    start, end = _parse(row.get("start_at"), local), _parse(row.get("end_at"), local)
    if start is None:
        return None
    end = max(end, start) if end is not None else start
    overlaps = start < day_end and (end > day_start or (start == end and start >= day_start))
    if not overlaps:
        return None
    return Meeting(start=start.astimezone(zone), end=end.astimezone(zone), all_day=False,
                   declined=_declined_by(row, email), **common)


def _mark_clashes(timed):
    for i, a in enumerate(timed):
        for b in timed[i + 1:]:
            if a.start < b.end and b.start < a.end:
                a.clash = b.clash = True


def read(client, user, zone, clock, naive_zone=None):
    """Today's meetings for this login in `zone`. Offset-less times with no row `timezone` are local to
    `naive_zone`, the platform's own zone (default: `zone`). Never raises on platform trouble."""
    naive_zone = naive_zone or zone
    day = clock.now().astimezone(zone).date()
    day_start = datetime.combine(day, time.min, tzinfo=zone)
    day_end = datetime.combine(day + timedelta(days=1), time.min, tzinfo=zone)
    try:
        rows = client.list_all("CalendarEvent.list", _server_filters(day))
    except McpError as exc:
        return PartResult.missing(failure_message(client.label, exc, "meetings"), isinstance(exc, LoginExpired))
    found = []
    for row in rows:
        if not isinstance(row, dict) or _is_cancelled(row):
            continue
        meeting = _meeting(row, zone, naive_zone, day, day_start, day_end, user.email)
        if meeting is not None:
            found.append(meeting)
    _mark_clashes([m for m in found if not m.all_day])
    found.sort(key=lambda m: (not m.all_day, m.start, m.title, m.id))
    return PartResult.complete(MeetingsPart(day, found))
