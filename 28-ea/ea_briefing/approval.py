"""The approval step: the only code that asks `McpClient.create_draft` to write (G5-AC2 to G5-AC4, SD-D-4, SD-D-6, SD-D-15).

After the briefing, each proposed reply (a message with a valid `Draft`) is offered on the user's terminal, oldest
first per system. The user chooses, per reply:

    s  save the proposed text        e  edit it, then save        Enter or anything else  skip        q  stop

* Offers, "Saving draft…", "Saved", failure lines and the summary go out through `say` (stderr, redacted by the
  caller), never stdout.
* A save is: re-read the original (`EmailMessage.get`; answered now -> "Already answered — draft not saved"), one
  `EmailMessage.create` as an unsent draft with the original's threading fields and a fresh `ApprovalToken`, then a
  read-back of the new id. A failed read-back, or a connection failure during the create, says "Draft may not be
  saved — check your Drafts folder". Nothing is retried, so no duplicate draft can follow.
* The end of input skips every remaining reply (nothing is saved, and nothing counts as "not saved"); `q`, and
  Ctrl-C, leave the reply they came on and every later one "not saved". A 401 stops that system's offers only.
* `e` needs a terminal (`stdin.isatty()`). The editor is `$EDITOR`, then `nano`, then typed lines; an empty result
  is a skip.
* Each reply ends in exactly one of saved, skipped or not saved. Every save that did not happen after the user
  chose it keeps the approved text in `ApprovalSummary.failed`, for the briefing file and the `--json` file, with
  its `kind` (refused, unconfirmed, answered, expired, interrupted) so the file words it as the PRD does.
* An error nobody planned for (an editor that fails, say) costs that one reply: it counts as not saved with the
  reason "unexpected error: <Type>" and the offers go on, so the summary, the file and the `--json` file still come.

The create arguments come from the slice 3 probe (plan, "Slice 3 probe evidence"): `company_id`, `from_email` and
`to` are required by the platform; `folder` and `is_draft` have no safe default. Change them here only.
"""
import os
import re
import shlex
import shutil
import subprocess
import tempfile
from dataclasses import dataclass, field
from email.utils import parseaddr

from .draft_writer import Draft
from .mail import flag, full_row
from .mcp import ApprovalToken, LoginExpired, McpError, ToolFailed, Unreachable

CHOICES = "[s]ave / [e]dit then save / [Enter] skip / [q] stop"
END_OF_TEXT = "."                      # the typed-lines fallback ends at a line holding only this
NO_DRAFTS = "No drafts to approve"
ANSWERED = "Already answered — draft not saved"
UNCONFIRMED = "Draft may not be saved — check your Drafts folder"
# FailedSave.kind: the platform refused the save, it can't be known whether a draft was saved, the original was
# answered meanwhile, the login expired before the create, Ctrl-C came while saving.
KIND_REFUSED, KIND_UNCONFIRMED, KIND_ANSWERED, KIND_EXPIRED, KIND_INTERRUPTED = (
    "refused", "unconfirmed", "answered", "expired", "interrupted")
_RE_PREFIX = re.compile(r"\s*re\s*:", re.IGNORECASE)


class EditorUnavailable(Exception):
    """No editor could be started: the step falls back to typed lines."""


@dataclass
class Proposal:
    """One reply that can be approved: the message, the checked draft, and the mailbox to save it into."""
    system: str          # "suryodaya" or "keystone"
    label: str           # "Suryodaya" or "Keystone"
    message: object      # mail.Message
    draft: Draft
    mailbox: object      # mail.Mailbox


@dataclass
class FailedSave:
    """A reply the user chose to save that was not (certainly) saved, with the text they approved."""
    system: str
    label: str
    message_id: str
    subject: str
    reason: str
    text: str
    kind: str = KIND_REFUSED   # how it failed: decides what the briefing file tells the user to do


@dataclass
class ApprovalSummary:
    saved: int = 0
    edited: int = 0       # of the saved ones, the replies whose text the user changed
    skipped: int = 0
    not_saved: int = 0    # failed saves, and replies left after a stop or an expired login
    failed: list = field(default_factory=list)   # [FailedSave]

    @property
    def total(self):
        return self.saved + self.skipped + self.not_saved

    def line(self):
        return "%d saved (%d edited), %d skipped, %d not saved" % (self.saved, self.edited, self.skipped,
                                                                    self.not_saved)


def proposals(results):
    """A `Proposal` for every valid `Draft` of every complete Mail part, systems in the order of `results`,
    oldest message first within a system."""
    found = []
    for result in results:
        if not result.mail.ok:
            continue
        part = result.mail.data
        for message in part.needs_answer:
            draft = part.drafts.get(message.id)
            if isinstance(draft, Draft):
                found.append(Proposal(result.name, result.label, message, draft, part.mailbox))
    return found


# ---- the editor ------------------------------------------------------------------------------------------

def external_editor(env, run=None, which=None):
    """`editor(text) -> edited text`: `$EDITOR` if it is installed, else `nano`; `EditorUnavailable` when neither
    can be started. An editor that exits with an error gives "" (nothing to save). `run` and `which` are the
    test seams (default `subprocess.run` and `shutil.which`, looked up when the editor is used)."""

    def edit(text):
        runner, finder = run or subprocess.run, which or shutil.which
        for name in (env.get("EDITOR"), "nano"):
            argv = shlex.split(name) if name and name.strip() else []
            if not argv or not finder(argv[0]):
                continue
            path = None
            try:
                fd, path = tempfile.mkstemp(prefix="briefing-reply-", suffix=".txt")
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    f.write(text)
                try:
                    done = runner(argv + [path])
                except OSError:
                    continue
                if done.returncode != 0:
                    return ""
                with open(path, encoding="utf-8", errors="replace") as f:
                    return f.read()
            finally:
                if path:
                    try:
                        os.unlink(path)
                    except OSError:
                        pass
        raise EditorUnavailable("no editor")

    return edit


# ---- the loop --------------------------------------------------------------------------------------------

class _Stopped(Exception):
    """Ctrl-C while a reply was being saved: that reply is already counted, the offers stop."""


def _is_terminal(stream):
    try:
        return bool(stream.isatty())
    except (AttributeError, ValueError):
        return False


def _typed_lines(stdin, say):
    say("Type the reply, then a line with only a full stop (.) to finish; nothing typed skips it:")
    lines = []
    while True:
        line = stdin.readline()
        if not line or line.strip() == END_OF_TEXT:
            return "\n".join(lines).rstrip()
        lines.append(line.rstrip("\n").rstrip("\r"))


def _subject(subject):
    subject = " ".join(str(subject or "").split())
    return subject if _RE_PREFIX.match(subject) else ("Re: " + subject).strip()


def _references(current, message_id):
    existing = " ".join(str(current or "").split())
    if message_id and message_id not in existing.split():
        existing = (existing + " " + message_id).strip()
    return existing


def _arguments(proposal, current, text):
    """The `EmailMessage.create` arguments, or (None, why not). Values the platform doesn't have are left out."""
    message, row = proposal.message, proposal.message.row

    def pick(name):
        return current.get(name) or row.get(name)

    company_id = pick("company_id")
    if not company_id:
        return None, "the message has no company_id to save the draft under"
    from_email = proposal.mailbox.address if proposal.mailbox else None
    if not from_email:
        return None, "the mailbox address is unknown"
    to = parseaddr(message.sender or "")[1] or (message.sender or "").strip()
    if not to:
        return None, "the message has no sender to reply to"
    mailbox_id = (proposal.mailbox.id if proposal.mailbox else None) or pick("mailbox_id")
    args = {"company_id": company_id, "mailbox_id": mailbox_id, "from_email": from_email, "to": to,
            "subject": _subject(message.subject), "body_text": text, "folder": "drafts", "is_draft": True,
            "thread_id": pick("thread_id"), "in_reply_to": message.message_id,
            "references": _references(pick("references"), message.message_id)}
    return {k: v for k, v in args.items() if v not in (None, "")}, None


def _new_id(payload):
    payload = payload.get("data") if isinstance(payload, dict) and isinstance(payload.get("data"), dict) else payload
    value = payload.get("id") if isinstance(payload, dict) else None
    return str(value) if value else None


class _Outcome:
    SAVED, FAILED, EXPIRED = "saved", "failed", "expired"

    def __init__(self, kind, reason=None, shown=None, failure=KIND_REFUSED):
        # `shown`: the fixed line for the user; None for a refused save, whose line `_not_saved_line` words
        # (it depends on whether the briefing file is rewritten). `failure`: the FailedSave kind.
        self.kind, self.reason, self.shown, self.failure = kind, reason, shown, failure


def _save(proposal, client, text):
    """Re-check, create, read back. Returns an `_Outcome`; nothing is retried."""
    message = proposal.message
    try:
        current = full_row(client.call("EmailMessage.get", {"id": message.id}))
    except LoginExpired:
        return _Outcome(_Outcome.EXPIRED, "login expired", failure=KIND_EXPIRED)
    except McpError as exc:
        return _Outcome(_Outcome.FAILED, "couldn't re-check the message (%s)" % (
            exc.reason if isinstance(exc, ToolFailed) else "can't be reached right now"))
    if current is not None and flag(current.get("is_answered")):
        return _Outcome(_Outcome.FAILED, "already answered", ANSWERED, KIND_ANSWERED)
    args, why = _arguments(proposal, current or {}, text)
    if args is None:
        return _Outcome(_Outcome.FAILED, why)
    try:
        created = client.create_draft(args, ApprovalToken())
    except LoginExpired:
        return _Outcome(_Outcome.EXPIRED, "login expired", failure=KIND_EXPIRED)
    except Unreachable:
        return _Outcome(_Outcome.FAILED, "may not be saved", UNCONFIRMED, KIND_UNCONFIRMED)
    except McpError as exc:
        return _Outcome(_Outcome.FAILED, exc.reason if isinstance(exc, ToolFailed) else str(exc) or type(exc).__name__)
    return _read_back(client, _new_id(created))


def _not_saved_line(reason, file_kept=True):
    where = " and kept in this run's briefing file" if file_kept else ""
    return "Draft not saved: %s — your text is shown below%s; save it by hand" % (reason, where)


def _read_back(client, new_id):
    unconfirmed = _Outcome(_Outcome.FAILED, "may not be saved", UNCONFIRMED, KIND_UNCONFIRMED)
    if not new_id:
        return unconfirmed
    try:
        row = full_row(client.call("EmailMessage.get", {"id": new_id}))
    except LoginExpired:
        unconfirmed.kind = _Outcome.EXPIRED   # the offers of this system stop; the draft itself may exist
        return unconfirmed
    except McpError:
        return unconfirmed
    if row is None:
        return unconfirmed
    folder = str(row.get("folder") or "").strip().lower()
    if (folder and folder != "drafts") or ("is_draft" in row and not flag(row.get("is_draft"))):
        return unconfirmed
    return _Outcome(_Outcome.SAVED)


def run(proposals, clients, stdin, editor, say, file_kept=True):
    """Offer each proposal and save what the user approves. Returns the `ApprovalSummary`.

    `clients`: {system name: McpClient}. `editor(text) -> text` may raise `EditorUnavailable`. `say(line)` shows
    a line to the user (stderr). The summary line is the last thing said. `file_kept`: the briefing file will be
    rewritten with the approved text of a failed save (false when its first save failed)."""
    summary = ApprovalSummary()
    if not proposals:
        say(NO_DRAFTS)
        return summary
    expired, mode, total = set(), None, len(proposals)
    for number, proposal in enumerate(proposals, 1):
        if mode == "stop":
            summary.not_saved += 1
            continue
        if mode == "end":
            summary.skipped += 1
            continue
        if proposal.system in expired:
            summary.not_saved += 1
            continue
        try:
            mode = _offer(proposal, number, total, clients, stdin, editor, say, summary, expired, file_kept)
        except KeyboardInterrupt:     # Ctrl-C at an offer or in the editor: stop
            say("Stopped — remaining drafts not saved")
            summary.not_saved += 1
            mode = "stop"
        except _Stopped:
            mode = "stop"
        except Exception as exc:      # nobody planned for this: it costs this reply, not the rest of the step
            reason = "unexpected error: " + type(exc).__name__
            say(_not_saved_line(reason, file_kept))
            say(proposal.draft.reply)
            _record_failure(proposal, reason, proposal.draft.reply, summary)
    say(summary.line())
    return summary


def _offer(proposal, number, total, clients, stdin, editor, say, summary, expired, file_kept):
    """One reply: show it, read the choice, act. Returns "stop", "end" or None (go on)."""
    message = proposal.message
    say("")
    say("Reply %d of %d — %s: %s (from %s)" % (number, total, proposal.label, " ".join(str(message.subject).split()),
                                              " ".join(str(message.sender).split())))
    say(proposal.draft.reply)
    say(CHOICES)
    try:
        line = stdin.readline()
    except (ValueError, OSError):   # a closed or broken stdin is the end of the input
        line = ""
    if not line:
        say("End of input — remaining drafts skipped")
        summary.skipped += 1
        return "end"
    choice = line.strip().lower()
    text = proposal.draft.reply
    if choice == "q":
        say("Stopped — remaining drafts not saved")
        summary.not_saved += 1
        return "stop"
    if choice == "e":
        if not _is_terminal(stdin):
            say("Can't edit without a terminal — skipped")
            summary.skipped += 1
            return None
        try:
            text = editor(text)
        except EditorUnavailable:
            text = _typed_lines(stdin, say)
        text = str(text).rstrip()   # editors add a final newline: that alone is no edit
        if not text:
            say("Empty text — skipped")
            summary.skipped += 1
            return None
    elif choice != "s":
        summary.skipped += 1
        return None
    _save_chosen(proposal, clients[proposal.system], text, text != proposal.draft.reply.rstrip(), say, summary, expired,
                 file_kept)
    return None


def _save_chosen(proposal, client, text, edited, say, summary, expired, file_kept):
    say("Saving draft…")
    try:
        outcome = _save(proposal, client, text)
    except KeyboardInterrupt:
        say("Interrupted while saving — " + UNCONFIRMED)
        _record_failure(proposal, "interrupted while saving — check your Drafts folder", text, summary,
                        KIND_INTERRUPTED)
        raise _Stopped() from None
    except Exception as exc:    # what happened to the draft is unknown: the same advice as an unconfirmed save
        reason = "unexpected error: " + type(exc).__name__
        say(UNCONFIRMED)
        _record_failure(proposal, reason, text, summary, KIND_UNCONFIRMED)
        return
    if outcome.kind == _Outcome.SAVED:
        say("Saved")
        summary.saved += 1
        summary.edited += 1 if edited else 0
        return
    if outcome.shown:
        say(outcome.shown)
    elif outcome.failure == KIND_REFUSED:
        say(_not_saved_line(outcome.reason, file_kept))
        say(text)
    if outcome.kind == _Outcome.EXPIRED:
        expired.add(proposal.system)
        say("%s: login expired — remaining drafts not saved; log in again and rerun" % proposal.label)
    _record_failure(proposal, outcome.reason, text, summary, outcome.failure)


def _record_failure(proposal, reason, text, summary, kind=KIND_REFUSED):
    summary.not_saved += 1
    summary.failed.append(FailedSave(proposal.system, proposal.label, proposal.message.id,
                                     " ".join(str(proposal.message.subject).split()), reason, text, kind))
