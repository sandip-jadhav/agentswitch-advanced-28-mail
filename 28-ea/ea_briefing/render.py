"""Results -> Markdown text, status line and the `--json` dict (G1-AC1, G6-AC4). Pure: no I/O.

It is never given a token or key. Redaction of what content might still carry is `store.redact`'s job.
Slice 1 draws the Meetings and Tasks parts; slice 2 the Mail entries (summary, proposed reply, skipped,
couldn't-be-opened and draft-already-saved lines); slice 3 the Approval section (`approval_section`, appended
after the status line once the approval step has run) and the `approval` key of the `--json` dict. Text that came from
a platform or a model is collapsed to one line, and a reply is quoted line by line with `> `, so none of
it can pose as a heading or the status line.
"""
from dataclasses import dataclass
from datetime import timezone
from zoneinfo import ZoneInfo

from .approval import ANSWERED, KIND_ANSWERED, KIND_INTERRUPTED, KIND_UNCONFIRMED, UNCONFIRMED
from .draft_writer import UNAVAILABLE, DraftFailed
from .mcp import PartResult

PARTS = ("mail", "meetings", "tasks")
PART_TITLES = {"mail": "Mail", "meetings": "Meetings", "tasks": "Tasks"}
NO_MAIL = "No mail needs an answer"
NO_MEETINGS = "No meetings today"
NO_OWN_TASKS = "No overdue tasks of yours"


@dataclass
class SystemResult:
    """What one system gave: the three parts, each a `PartResult`."""
    name: str          # "suryodaya" or "keystone"
    label: str         # "Suryodaya" or "Keystone"
    mail: PartResult
    meetings: PartResult
    tasks: PartResult
    zone: ZoneInfo = None   # the display zone of the system's mail times; None reads as UTC


def _one_line(text):
    """Collapse all whitespace, line breaks included, so platform text stays on its own line."""
    return " ".join(str(text).split())


def _reason(text):
    """A reason as a line of its own, unable to pose as a heading or the status line."""
    line = _one_line(text)
    return "\\" + line if line.startswith(("#", "Status:")) else line


def _received_text(message, zone):
    """`2026-11-05 14:30 IST`, in the system's zone (UTC without one)."""
    if message.received is None:
        return "time unknown"
    return message.received.astimezone(zone or timezone.utc).strftime("%Y-%m-%d %H:%M %Z")


def _quote(text):
    """A text of several lines as quoted lines: nothing in it can start a heading or the status line."""
    return ["  > " + line.rstrip() if line.strip() else "  >" for line in str(text).replace("\r", "").split("\n")]


def _outcome(drafts, message):
    return drafts.get(message.id) or DraftFailed(UNAVAILABLE)


def _entry_lines(message, outcome, zone):
    marked = " · needs your decision" if outcome.needs_decision else ""
    lines = ["- **%s** · from %s · received %s%s" % (_one_line(message.subject), _one_line(message.sender),
                                                    _received_text(message, zone), marked)]
    if outcome.summary:
        lines.append("  Summary: " + _one_line(outcome.summary))
    else:
        lines.append("  Summary: (no summary — first line shown) " + _one_line(message.first_line()))
    if isinstance(outcome, DraftFailed):
        lines.append("  No draft for this message (%s) — reply by hand" % _one_line(outcome.reason))
    else:
        lines += ["  Proposed reply:"] + _quote(outcome.reply)
    return lines


def _mail_lines(part, zone=None):
    if not part.ok:
        return [_reason(part.reason)]
    mail, lines = part.data, []
    for message in mail.needs_answer:
        lines += _entry_lines(message, _outcome(mail.drafts, message), zone)
    if not mail.needs_answer and not mail.unopened:
        lines.append(NO_MAIL)
    for message in mail.unopened:
        lines.append("1 message couldn't be opened (subject: %s) — skipped; open it in the mailbox, and report "
                     "it if it repeats" % _one_line(message.subject))
    for saved in mail.already_drafted:
        lines += ["- **%s** · draft already saved" % _one_line(saved.message.subject)] + _quote(saved.text)
    if mail.skipped:
        lines += ["", "**Skipped — no reply expected**", ""]
        lines += ["- %s · %s" % (_one_line(s.message.subject), _one_line(s.reason)) for s in mail.skipped]
    return lines


def _meeting_line(meeting):
    marks = (["clash"] if meeting.clash else []) + (["declined"] if meeting.declined else [])
    return " · ".join(["- " + meeting.time_text, _one_line(meeting.title), _one_line(meeting.organizer)] + marks)


def _meetings_lines(part):
    if not part.ok:
        return [_reason(part.reason)]
    if not part.data.meetings:
        return [NO_MEETINGS]
    return [_meeting_line(m) for m in part.data.meetings]


def _task_line(task):
    return "- %s · due %s · %s" % (_one_line(task.title), task.due_date, task.overdue_text)


def _tasks_lines(part):
    if not part.ok:
        return [_reason(part.reason)]
    lines = [_task_line(t) for t in part.data.own] or [NO_OWN_TASKS]
    return lines + ["", part.data.others_line]


_LINES = {"meetings": _meetings_lines, "tasks": _tasks_lines}   # mail also needs the zone: see markdown


def _complete_word(part):
    return "complete" if part.ok else "missing"


def status_line(results, elapsed):
    """`Status: <System>: Mail …, Meetings …, Tasks … · <System>: … · 12.3 s`"""
    systems = ["%s: %s" % (r.label, ", ".join("%s %s" % (PART_TITLES[p], _complete_word(getattr(r, p)))
                                              for p in PARTS)) for r in results]
    return "Status: " + " · ".join(systems + ["%.1f s" % elapsed])


def markdown(results, elapsed, day):
    """The whole briefing text: title, one section per system, one status line last."""
    out = ["# Daily briefing — %s" % day.isoformat(), ""]
    for result in results:
        out += ["## " + result.label, ""]
        for name in PARTS:
            part = getattr(result, name)
            lines = _mail_lines(part, result.zone) if name == "mail" else _LINES[name](part)
            out += ["### " + PART_TITLES[name], ""] + lines + [""]
    out.append(status_line(results, elapsed))
    return "\n".join(out) + "\n"


def approval_section(summary):
    """The Approval section for the briefing file, to append after the status line: the summary line and, for
    every save the user chose that did not happen, the text they approved, quoted. "" when the step offered
    nothing. A save that may have happened, or that was refused because the message was answered, is worded as
    the PRD words it (check the Drafts folder, do not retry; no draft for an answered message); only a refused or
    expired save is "save it by hand". `summary` is an `approval.ApprovalSummary`."""
    if summary.total == 0:
        return ""
    lines = ["", "## Approval", "", summary.line(), ""]
    for failed in summary.failed:
        lines.append("- **%s** · %s · not saved: %s" % (_one_line(failed.subject), _one_line(failed.label),
                                                         _one_line(failed.reason)))
        if failed.kind in (KIND_UNCONFIRMED, KIND_INTERRUPTED):
            lines.append("  %s; do not retry, to avoid duplicates. Your approved text, kept here for reference:"
                         % UNCONFIRMED)
        elif failed.kind == KIND_ANSWERED:
            lines.append("  %s. Your approved text, kept here for reference:" % ANSWERED)
        else:
            lines.append("  Your approved text, kept here so you can save it by hand:")
        lines += _quote(failed.text)
        lines.append("")
    return "\n".join(lines).rstrip("\n") + "\n"


def nothing_fetched(results):
    """True when every part of every requested system is missing ("no mailbox" included)."""
    return not any(getattr(r, p).ok for r in results for p in PARTS)


def _json_meeting(meeting):
    return {"id": meeting.id, "title": meeting.title, "time": meeting.time_text, "all_day": meeting.all_day,
            "organizer": meeting.organizer, "clash": meeting.clash, "declined": meeting.declined}


def _json_part(name, part):
    if not part.ok:
        return {"complete": False, "reason": _one_line(part.reason)}
    if name == "meetings":
        return {"complete": True, "events": [_json_meeting(m) for m in part.data.meetings]}
    if name == "tasks":
        return {"complete": True,
                "own_overdue": [{"id": t.id, "title": t.title, "due_date": t.due_date,
                                 "days_overdue": t.days_overdue} for t in part.data.own],
                "others_count": part.data.others_count}
    mail = part.data
    return {"complete": True,
            "needs_answer": [{"id": m.id, "draft_status": _outcome(mail.drafts, m).status} for m in mail.needs_answer],
            "skipped": [{"id": s.message.id, "reason": _one_line(s.reason)} for s in mail.skipped],
            "unopened": [m.id for m in mail.unopened],
            "draft_saved": [d.message.id for d in mail.already_drafted]}


def _json_approval(summary):
    return {"saved": summary.saved, "edited": summary.edited, "skipped": summary.skipped,
            "not_saved": summary.not_saved,
            "failed": [{"system": f.system, "id": f.message_id, "subject": _one_line(f.subject),
                        "kind": f.kind, "reason": _one_line(f.reason), "text": f.text} for f in summary.failed]}


def json_result(results, elapsed, day, model=None, approval=None):
    """The `--json` dict (G6-AC4): complete or reason per part, events, own tasks, others count, the mail
    id lists with each reply's draft status, `model` (the draft writer's counts) when given, and `approval`
    (the approval step's summary, an `approval.ApprovalSummary`) when the step ran."""
    data = {"date": day.isoformat(), "elapsed_seconds": round(elapsed, 1),
            "systems": {r.name: {p: _json_part(p, getattr(r, p)) for p in PARTS} for r in results}}
    if model is not None:
        data["model"] = dict(model)
    if approval is not None:
        data["approval"] = _json_approval(approval)
    return data
