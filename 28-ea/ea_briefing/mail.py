"""The Mail part of one system: reads, classifies, never drafts and never writes (G2-AC1 to G2-AC6, SD-D-7).

`read(client, user)` returns a `PartResult` holding a `MailPart`. Steps, in order (plan R3, P-B-24 to P-B-29):

1. `Mailbox.list` and `EmailAccount.list`: both empty is "no mailbox for this login" (G2-AC5). They may
   return other users' rows too: a row whose `user_id` names someone else is neither the login's address
   (the "sent by you" rule) nor the mailbox drafts are saved into.
2. The unread inbox and the drafts folder, every page. A failure of either makes the whole part missing,
   because without the drafts a second draft could be proposed.
3. Per inbox row: my draft replying to it -> "draft already saved"; else a read, draft, non-inbox or
   answered row -> not listed (the server only narrows the fetch: the slice 1 probe found it compares
   stored text, so every flag is checked again here); else the skip rules, first match gives the reason.
4. `EmailMessage.get` for each remaining candidate, up to GET_WORKERS at once (each is a blocking round-trip,
   and drafting waits for this part); a failed read lists it as "couldn't be opened".
5. Everything is oldest `received_at` first. No model is involved: the rules decide, never the model.
6. The part keeps the login's mailbox (`MailPart.mailbox`: id and address), which the approval step copies into
   `mailbox_id` and `from_email` of a saved draft (slice 3; the Mailbox row's `email` field, probe 2026-11-07).

The field names, filter arguments and id argument below were confirmed by the live probe (plan task 15,
2026-11-06): they exist on both systems. Slice 3's live save confirmed the `created_by` and `in_reply_to` of a
saved draft: the server stamps `created_by` with the login's id and keeps `in_reply_to`. Change them here only.
"""
import re
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, field
from datetime import datetime, timezone
from email.utils import parseaddr

from .mcp import LoginExpired, McpError, PartResult, failure_message

# ---- EmailMessage fields (confirmed by the probe) ------------------------------------------------------------
F_ID = "id"
F_MESSAGE_ID = "message_id"
F_FOLDER = "folder"
F_SENDER = "from_email"
F_SUBJECT = "subject"
F_BODY = "body_text"
F_SNIPPET = "snippet"
F_RECEIVED = "received_at"            # ISO 8601; an offset-less time is read as UTC
F_IS_READ, F_IS_DRAFT, F_IS_ANSWERED = "is_read", "is_draft", "is_answered"
F_CREATED_BY = "created_by"           # the creating user's id
F_IN_REPLY_TO = "in_reply_to"         # the original's message_id
F_UNSUBSCRIBE = "list_unsubscribe"
F_CATEGORY = "split_category"         # "news" marks a newsletter
F_HEADERS = "raw_headers"             # header text, or a mapping of header -> value
MAILBOX_ADDRESS_FIELDS = ("address", "email", "email_address")   # on Mailbox / EmailAccount rows
F_MAILBOX_ID, F_MAILBOX_USER = "id", "user_id"   # Mailbox rows (probe, 2026-11-07): id, company_id, email, user_id, ...

INBOX_QUERY = {F_FOLDER: "inbox", F_IS_READ: False, F_IS_DRAFT: False}
DRAFTS_QUERY = {F_FOLDER: "drafts"}
GET_ID_ARG = "id"                     # EmailMessage.get(id=<row id>)
GET_WORKERS = 8                       # EmailMessage.get calls in flight at once (they block on the network)

# ---- the fixed skip rules (design, Data flow: Mail step 5) ----------------------------------
NO_REPLY_SENDER = re.compile(r"no-?reply|do-?not-?reply|mailer-daemon|notifications?@", re.IGNORECASE)
REASON_OWN = "sent by you"
REASON_NO_REPLY = "no-reply sender"
REASON_NEWSLETTER = "newsletter"
REASON_AUTOMATED = "automated notification"
FIRST_LINE_WORDS = 25


@dataclass(eq=False)
class Message:
    """One mail message, from its inbox row and (for candidates) its full read."""
    id: str
    message_id: str
    subject: str
    sender: str
    received_at: str          # as the platform sent it
    received: datetime        # aware (UTC when the platform sent no offset); None when unreadable
    snippet: str
    body: str
    row: dict                 # the row as read, for the approval step's threading fields (slice 3)

    def first_line(self):
        """The first sentence of the snippet (else the body), at most 25 words: the "(no summary — first
        line shown)" text, in the message's own language."""
        words = " ".join((self.snippet or self.body or "").split())
        sentence = re.split(r"(?<=[.!?])\s", words, maxsplit=1)[0]
        parts = sentence.split()
        if len(parts) > FIRST_LINE_WORDS:
            return " ".join(parts[:FIRST_LINE_WORDS]) + "…"
        return sentence


@dataclass
class Skipped:
    message: Message
    reason: str


@dataclass
class SavedDraft:
    """A message that already has a draft reply of mine: shown, never proposed again."""
    message: Message
    text: str


@dataclass
class Mailbox:
    """The login's mailbox as the approval step needs it for `mailbox_id` and `from_email`.
    `id` is None when only an EmailAccount row exists (its id is not a mailbox id)."""
    id: str = None
    address: str = None


@dataclass
class MailPart:
    needs_answer: list = field(default_factory=list)     # [Message], oldest first: the drafting queue
    skipped: list = field(default_factory=list)          # [Skipped]
    unopened: list = field(default_factory=list)         # [Message] whose full read failed
    already_drafted: list = field(default_factory=list)  # [SavedDraft]
    drafts: dict = field(default_factory=dict)           # message id -> Draft | DraftFailed, set by the runner
    mailbox: Mailbox = None                              # the login's mailbox (slice 3: the approval step's create)


def _text(value):
    return "" if value is None else str(value)


def flag(value):
    """A stored flag read as a boolean: true, 1, or the text 'true'/'1'/'t'/'yes'."""
    if isinstance(value, str):
        return value.strip().lower() in ("true", "1", "t", "yes")
    return bool(value)


def _parse_time(value):
    if not isinstance(value, str) or not value.strip():
        return None
    text = value.strip()
    if text[-1] in "zZ":
        text = text[:-1] + "+00:00"
    try:
        when = datetime.fromisoformat(text)
    except ValueError:
        return None
    return when if when.tzinfo else when.replace(tzinfo=timezone.utc)


def _message(row, full=None):
    """A Message from a listed row, with the opened row's fields winning where it has them."""
    merged = dict(row)
    for key, value in (full or {}).items():
        if value not in (None, ""):
            merged[key] = value
    received = _text(merged.get(F_RECEIVED))
    return Message(id=_text(row.get(F_ID)), message_id=_text(merged.get(F_MESSAGE_ID)),
                   subject=_text(merged.get(F_SUBJECT)), sender=_text(merged.get(F_SENDER)),
                   received_at=received, received=_parse_time(received),
                   snippet=_text(row.get(F_SNIPPET) or merged.get(F_SNIPPET)),
                   body=_text(merged.get(F_BODY) or merged.get(F_SNIPPET)), row=merged)


_LATEST = datetime.max.replace(tzinfo=timezone.utc)


def _oldest_first(items, message_of=lambda m: m):
    return sorted(items, key=lambda i: message_of(i).received or _LATEST)  # sorted() is stable


def _address(sender):
    parsed = parseaddr(sender or "")[1]
    return (parsed or _text(sender)).strip().lower()


def _header_lines(headers):
    if isinstance(headers, dict):
        return ["%s: %s" % (k, v) for k, v in headers.items()]
    return _text(headers).splitlines()


def _skip_reason(row, own_addresses):
    """The first matching skip rule's reason, or None."""
    sender = _address(row.get(F_SENDER))
    if sender in own_addresses:
        return REASON_OWN
    if NO_REPLY_SENDER.search(sender):
        return REASON_NO_REPLY
    if _text(row.get(F_UNSUBSCRIBE)).strip() or _text(row.get(F_CATEGORY)).strip().lower() == "news":
        return REASON_NEWSLETTER
    for line in _header_lines(row.get(F_HEADERS)):
        name, _, value = line.partition(":")
        name, value = name.strip().lower(), value.strip().lower()
        first = value.split()[0] if value else ""
        if name == "auto-submitted" and first and first != "no":
            return REASON_AUTOMATED
        if name == "precedence" and first in ("bulk", "list", "junk"):
            return REASON_AUTOMATED
    return None


def _is_own_row(row, user):
    """A Mailbox / EmailAccount row of this login: the list tools may also return other users' rows, so a
    row whose `user_id` names someone else is not the login's; a row with no `user_id` is taken as its own."""
    if not isinstance(row, dict):
        return False
    owner = _text(row.get(F_MAILBOX_USER)).strip()
    return not owner or owner == user.id


def _own_addresses(mailboxes, accounts, user):
    addresses = {(user.email or "").strip().lower()}
    for row in list(mailboxes) + list(accounts):
        if _is_own_row(row, user):
            addresses.update(_text(row.get(f)).strip().lower() for f in MAILBOX_ADDRESS_FIELDS)
    addresses.discard("")
    return addresses


def _row_address(row):
    for name in MAILBOX_ADDRESS_FIELDS:
        value = _text(row.get(name)).strip()
        if value:
            return value
    return None


def _mailbox_of(mailboxes, accounts, user):
    """The mailbox to save drafts into: the Mailbox row carrying this login's `user_id`, else the first one
    with no `user_id`; with no such row, the address of the login's first EmailAccount row and no id. A row
    owned by another user is never used."""
    rows = [r for r in mailboxes if _is_own_row(r, user)]
    if rows:
        mine = [r for r in rows if _text(r.get(F_MAILBOX_USER)).strip() == user.id]
        row = (mine or rows)[0]
        return Mailbox(_text(row.get(F_MAILBOX_ID)) or None, _row_address(row))
    for row in accounts:
        if _is_own_row(row, user):
            return Mailbox(None, _row_address(row))
    return Mailbox()


def full_row(payload):
    """The row an `EmailMessage.get` reply holds: the reply itself, or under `data`."""
    if isinstance(payload, dict) and isinstance(payload.get("data"), dict):
        payload = payload["data"]
    return payload if isinstance(payload, dict) else None


def _saved_drafts(drafts, user):
    """{original message_id: the draft row} for drafts this login created in reply to a message."""
    saved = {}
    for draft in drafts:
        if isinstance(draft, dict) and _text(draft.get(F_CREATED_BY)) == user.id and draft.get(F_IN_REPLY_TO):
            saved.setdefault(_text(draft[F_IN_REPLY_TO]), draft)
    return saved


def read(client, user):
    """The Mail part for this login. Never raises on platform trouble."""
    try:
        mailboxes = client.list_all("Mailbox.list")
        accounts = client.list_all("EmailAccount.list")
        if not mailboxes and not accounts:
            return PartResult.missing("%s: no mailbox for this login — ask the course team to set one up"
                                      % client.label)
        inbox = client.list_all("EmailMessage.list", dict(INBOX_QUERY))
        drafts = client.list_all("EmailMessage.list", dict(DRAFTS_QUERY))
        return PartResult.complete(_classify(client, user, mailboxes, accounts, inbox, drafts))
    except LoginExpired as exc:
        return PartResult.missing(failure_message(client.label, exc, "mail"), True)
    except McpError as exc:
        return PartResult.missing(failure_message(client.label, exc, "mail"))


def _classify(client, user, mailboxes, accounts, inbox, drafts):
    saved, own = _saved_drafts(drafts, user), _own_addresses(mailboxes, accounts, user)
    part, candidates = MailPart(), []
    part.mailbox = _mailbox_of(mailboxes, accounts, user)
    messages = [(_message(r), r) for r in inbox if isinstance(r, dict) and r.get(F_ID)]
    for message, row in _oldest_first(messages, lambda pair: pair[0]):
        draft = saved.get(message.message_id) if message.message_id else None
        if draft is not None:
            part.already_drafted.append(SavedDraft(message, _text(draft.get(F_BODY) or draft.get(F_SNIPPET))))
        elif (flag(row.get(F_IS_READ)) or flag(row.get(F_IS_DRAFT)) or flag(row.get(F_IS_ANSWERED))
              or _text(row.get(F_FOLDER)).strip().lower() not in ("", "inbox")):
            continue
        else:
            reason = _skip_reason(row, own)
            if reason:
                part.skipped.append(Skipped(message, reason))
            else:
                candidates.append(message)
    for message, full in zip(candidates, _open_all(client, candidates)):
        if full is None:
            part.unopened.append(message)
        else:
            part.needs_answer.append(_message(message.row, full))
    part.needs_answer = _oldest_first(part.needs_answer)
    return part


def _open(client, message):
    """The full row of one message, or None when it could not be read. LoginExpired is passed on."""
    try:
        return full_row(client.call("EmailMessage.get", {GET_ID_ARG: message.id}))
    except LoginExpired:
        raise
    except McpError:
        return None


def _open_all(client, candidates):
    """`_open` for every candidate, up to GET_WORKERS at once, in the candidates' order. Every read finishes
    before an error (a LoginExpired, a ToolNotAllowed) is passed on."""
    if len(candidates) <= 1:
        return [_open(client, m) for m in candidates]
    with ThreadPoolExecutor(max_workers=min(GET_WORKERS, len(candidates))) as pool:
        futures = [pool.submit(_open, client, m) for m in candidates]
    return [f.result() for f in futures]
