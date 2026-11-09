"""The user's own overdue tasks, and a separate count of everybody else's (G4-AC1, G4-AC2).

`read(client, user, zone, clock)` returns a `PartResult` holding a `TasksPart`, or a plain reason.
The own list and the others count come from separate reads, so the count can fail alone.

Task row fields, probed live on both systems (2026-11-06): `id`, `title`, `status` (todo, in_progress,
in_review, done, completed, cancelled), `due_date` (YYYY-MM-DD or null), `created_by` (a user id) and
`assignee_id`, which links to an Employee (the `/api/auth/me` `employee_id`, null for `team28c`), so for
that login `created_by` is the only own-task match. The server's `due_date=lt:<date>` filter works.
"""
from dataclasses import dataclass, field
from datetime import date

from .mcp import LoginExpired, McpError, PartResult, failure_message

CLOSED = ("done", "completed", "cancelled", "canceled", "closed")
OTHERS_UNAVAILABLE = "Count of other overdue tasks unavailable — your own list above is complete"


@dataclass
class OverdueTask:
    id: str
    title: str
    due_date: str          # YYYY-MM-DD
    days_overdue: int

    @property
    def overdue_text(self):
        return "%d day%s overdue" % (self.days_overdue, "" if self.days_overdue == 1 else "s")


@dataclass
class TasksPart:
    today: date
    own: list = field(default_factory=list)   # [OverdueTask], most overdue first
    others_count: int = None                  # None: the count couldn't be read

    @property
    def others_line(self):
        if self.others_count is None:
            return OTHERS_UNAVAILABLE
        n = self.others_count
        return "%d other overdue task%s visible to this login" % (n, " is" if n == 1 else "s are")


def _due(row):
    """The row's due date (first 10 characters) as a date, or None."""
    text = row.get("due_date")
    try:
        return date.fromisoformat(text[:10]) if isinstance(text, str) and text else None
    except ValueError:
        return None


def _is_open(row):
    return str(row.get("status") or "").strip().lower() not in CLOSED


def _is_mine(row, user_id, ids):
    """Own: created by the user's id, or assigned to the user's id or employee id (G4-AC1)."""
    created, assignee = row.get("created_by"), row.get("assignee_id")
    return (created is not None and str(created) == user_id) or (assignee is not None and str(assignee) in ids)


def _overdue(rows, today):
    """Open rows due before today, as (row, due date)."""
    found = []
    for row in rows:
        if isinstance(row, dict) and _is_open(row):
            due = _due(row)
            if due is not None and due < today:
                found.append((row, due))
    return found


def read(client, user, zone, clock):
    """Own overdue tasks plus the others count. Never raises on platform trouble."""
    today = clock.now().astimezone(zone).date()
    before = {"due_date": "lt:" + today.isoformat()}
    ids = {user.id} | ({user.employee_id} if user.employee_id else set())
    reads = [dict(before, assignee_id=user.id)]
    if user.employee_id and user.employee_id != user.id:
        reads.append(dict(before, assignee_id=user.employee_id))
    reads.append(dict(before, created_by=user.id))
    try:
        merged = {}
        for filters in reads:
            for row in client.list_all("Task.list", filters):
                if isinstance(row, dict) and row.get("id") is not None:
                    merged.setdefault(str(row["id"]), row)
    except McpError as exc:
        return PartResult.missing(failure_message(client.label, exc, "tasks"), isinstance(exc, LoginExpired))
    own = [OverdueTask(str(row["id"]), str(row.get("title") or "(no title)"), due.isoformat(), (today - due).days)
           for row, due in _overdue(merged.values(), today) if _is_mine(row, user.id, ids)]
    own.sort(key=lambda t: (t.due_date, t.title, t.id))
    try:
        everyone = client.list_all("Task.list", before)
        own_ids = {t.id for t in own}
        others = sum(1 for row, _ in _overdue(everyone, today) if str(row.get("id")) not in own_ids)
    except LoginExpired as exc:  # a dead token is not "count unavailable": the whole system fails (P-B-2)
        return PartResult.missing(failure_message(client.label, exc, "tasks"), True)
    except McpError:
        others = None
    return PartResult.complete(TasksPart(today, own, others))
