"""Test doubles shared by every suite: FakeTransport, FakeServer, FakeClock, fixtures and row builders.

The seams (plan R2, "test seam"):

* transport: a callable ``transport(method, url, headers, body, timeout) -> (status, content_type, text)``.
  ``McpClient`` and ``auth`` take it. It may raise ``OSError`` (connection refused, timeout) for
  "can't be reached". ``FakeTransport`` is one such callable that serves several fake servers by
  base URL and records every call, including calls to hosts it does not serve.
* clock: an object with ``now() -> aware datetime``, ``monotonic() -> float`` and
  ``local_date() -> date`` (the machine-local date used for file names). ``FakeClock`` is one.

Later modules (runner, mail, store, cli) use this file as it is; extend it, don't fork it.
Slice 2 adds message_row, serve_mail, FakeModelServer (a fake chat/completions endpoint at a third base
URL, MODEL_BASE) and FakeDraftWriter; slice 3 adds FakeEditor, TtyInput, the threading fields of
message_row and an EmailMessage.create handler in serve_mail (the created draft joins the served rows).
"""
import io
import json
import threading
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

FIXTURES = Path(__file__).parent / "fixtures"

SURYODAYA_BASE = "https://agentswitch.theschoolofai.in"
KEYSTONE_BASE = "https://class.agentswitch.theschoolofai.in"

# 2026-11-06 06:00 UTC: 11:30 on 6 Nov in IST, 01:00 on 6 Nov in New York (EST). Both systems'
# "today" is 2026-11-06, and a 22:00 Eastern meeting today is already 7 Nov in IST.
FIXED_NOW = datetime(2026, 11, 6, 6, 0, 0, tzinfo=timezone.utc)

# Paging parameter names the fake server understands: the client's own constants, so a probe
# correction in mcp.py carries over to the fakes.
from ea_briefing.mcp import LIMIT_PARAM, OFFSET_PARAM  # noqa: E402

# A third base URL: the fake model gateway. Scenario tests point BRIEFING_LLM_URL (or OPENAI_BASE_URL) at it.
MODEL_BASE = "https://model.example.test"
MODEL_URL = MODEL_BASE + "/v1"
MODEL_NAME = "fake-model"
MODEL_KEY = "model-key-secret-3333"

TOKEN_SURYODAYA = "tok-suryodaya-secret-1111"
TOKEN_KEYSTONE = "tok-keystone-secret-2222"

ME_DEFAULT = {"id": "u-100", "employee_id": "e-100", "email": "team28c@example.test",
              "name": "Team Twenty-Eight"}


def load_fixture(name):
    with open(FIXTURES / name, encoding="utf-8") as f:
        return json.load(f)


# --------------------------------------------------------------------------- row builders

def event_row(id, title, start, end, *, all_day=False, status="confirmed",
              organizer="boss@example.test", attendees=None, zone_name=None):
    """A CalendarEvent row. start/end are ISO strings ('2026-11-06T03:30:00Z'). `zone_name` is the
    row's `timezone` field; the live platform sets it on many rows and stores offset-less local times."""
    return {"id": id, "title": title, "start_at": start, "end_at": end, "all_day": all_day,
            "status": status, "organizer_email": organizer, "attendees": attendees or [],
            "timezone": zone_name}


def attendee(email, status):
    return {"email": email, "status": status}


def task_row(id, title, due, *, status="open", created_by="u-900", assignee_id="u-901"):
    """A Task row. due is 'YYYY-MM-DD' or None. Defaults belong to somebody else."""
    return {"id": id, "title": title, "status": status, "due_date": due,
            "created_by": created_by, "assignee_id": assignee_id}


def message_row(id, subject, sender, received_at, *, folder="inbox", is_read=False, is_draft=False,
                is_answered=False, body=None, message_id=None, in_reply_to=None, created_by="u-900",
                list_unsubscribe=None, split_category=None, raw_headers="", snippet=None,
                company_id="co-1", mailbox_id="mb-1", thread_id=None, references=None):
    """An EmailMessage row with the field names the design assumes (see mail.py's field constants).
    `received_at` is an ISO string. Defaults make an unread, unanswered inbox message from somebody else.
    The threading fields are the ones the slice 3 probe found on live inbox rows: `company_id`, `mailbox_id`
    (the id of MAILBOX_ROW), `thread_id`, and `references` (null on inbox rows)."""
    body = body if body is not None else "Hello, please reply to: " + subject
    return {"id": id, "message_id": message_id if message_id is not None else "<%s@mail.test>" % id,
            "folder": folder, "from_email": sender, "subject": subject,
            "snippet": snippet if snippet is not None else body[:80], "body_text": body,
            "received_at": received_at, "is_read": is_read, "is_draft": is_draft, "is_answered": is_answered,
            "in_reply_to": in_reply_to, "created_by": created_by, "list_unsubscribe": list_unsubscribe,
            "split_category": split_category, "raw_headers": raw_headers,
            "company_id": company_id, "mailbox_id": mailbox_id,
            "thread_id": thread_id if thread_id is not None else "th-" + id, "references": references}


# The arguments EmailMessage.create accepts and requires, as the slice 3 probe read them from tools/list
# (additional properties are refused; `company_id`, `from_email` and `to` are required).
CREATE_REQUIRED = ("company_id", "from_email", "to")
CREATE_ALLOWED = frozenset(CREATE_REQUIRED) | {
    "message_id", "mailbox_id", "thread_id", "in_reply_to", "references", "from_name", "cc", "bcc", "subject",
    "body_html", "body_text", "snippet", "folder", "server_folder", "is_read", "is_starred", "is_draft",
    "is_answered", "status", "labels", "importance", "received_at", "sent_at"}


def serve_mail(server, rows, filters=True, creator=ME_DEFAULT["id"]):
    """Serve `rows` (inbox, drafts and all) as EmailMessage.list (server-side filters honoured, as the
    live platform's equality filters are; `filters=False` ignores them), EmailMessage.get (by `id`) and
    EmailMessage.create. A created row is stored with `created_by` = `creator` (what the real server is
    assumed to stamp, unconfirmed until slice 3's live run) and joins the served rows, so a later read, or
    a rerun of the briefing, sees it. `server.created` records the arguments of every create."""
    store = list(rows)
    server.rows("EmailMessage.list", store, filters=filters, copy=False)
    server.created = []
    server.mail_rows = store   # the live list: a test changes a row in place to change the world

    def get(arguments):
        for row in store:
            if row.get("id") == arguments.get("id"):
                return row
        raise ToolError("EmailMessage not found")

    def create(arguments):
        unknown = sorted(set(arguments) - CREATE_ALLOWED)
        if unknown:
            raise ToolError("unknown arguments: " + ", ".join(unknown))
        missing = [k for k in CREATE_REQUIRED if not arguments.get(k)]
        if missing:
            raise ToolError("missing required: " + ", ".join(missing))
        new_id = "new-%d" % (len(server.created) + 1)
        server.created.append(dict(arguments))
        row = {"id": new_id, "message_id": "<%s@mail.test>" % new_id, "created_by": creator,
               "folder": "inbox", "is_draft": False, "is_read": False, "is_answered": False,
               "received_at": None, "in_reply_to": None, "references": None, "list_unsubscribe": None,
               "split_category": None, "raw_headers": "", "snippet": (arguments.get("body_text") or "")[:80]}
        row.update(arguments)
        store.append(row)
        return row

    server.handler("EmailMessage.get", get)
    server.handler("EmailMessage.create", create)
    return server


# --------------------------------------------------------------------------- clock

class FakeClock:
    def __init__(self, now=FIXED_NOW, local_tz=timezone.utc):
        self._now = now
        self._mono = 0.0
        self._tz = local_tz
        self._lock = threading.Lock()

    def now(self):
        with self._lock:
            return self._now

    def monotonic(self):
        with self._lock:
            return self._mono

    def local_date(self):
        return self.now().astimezone(self._tz).date()

    def advance(self, seconds):
        from datetime import timedelta
        with self._lock:
            self._now += timedelta(seconds=seconds)
            self._mono += seconds


# --------------------------------------------------------------------------- server

class ToolError(Exception):
    """Raise from a handler: the tool reply carries isError=true with this message."""


class HttpStatus(Exception):
    """Raise from a handler: the whole HTTP answer has this status and text."""

    def __init__(self, status, text="error"):
        super().__init__(text)
        self.status = status
        self.text = text


_FILTER_OPS = {"lt": lambda a, b: a < b, "gt": lambda a, b: a > b,
               "lte": lambda a, b: a <= b, "gte": lambda a, b: a >= b}


def _matches(row, key, wanted):
    """The live platform compares the stored TEXT with the filter text (probed 2026-11-06: counts match
    plain string comparison, not instants), so this does too."""
    value = row.get(key)
    if isinstance(wanted, str) and ":" in wanted and wanted.split(":", 1)[0] in _FILTER_OPS:
        op, bound = wanted.split(":", 1)
        return value is not None and _FILTER_OPS[op](str(value), bound)
    return value == wanted


class FakeServer:
    """One fake AgentSwitch system: /api/mcp, /api/auth/me and /api/auth/login."""

    def __init__(self, name, token, me=None):
        self.name = name
        self.token = token          # the bearer token it accepts (None: accepts any)
        # a dict, or an exception (HttpStatus / OSError) raised when /api/auth/me is read
        self.me = me if isinstance(me, Exception) else dict(me if me is not None else ME_DEFAULT)
        self._handlers = {}         # tool name -> fn(arguments) -> payload
        self._fail_when = []        # (tool name, predicate(arguments), exception)
        self._expired = False
        self._down = None
        self.event_stream = False   # answer tools/call as text/event-stream
        self.login_handler = lambda body: {"token": "login-token-" + self.name, "user": {}}

    # ---- configuring
    def rows(self, tool, rows, filters=True, copy=True):
        """Serve `rows` as a paged list tool, shaped as the live platform's (probed 2026-11-06): {data, total,
        limit, offset}, with no has_more. Honours limit/offset and, when `filters`, lt:/gt:/lte:/gte: prefixes and equality on every other argument.
        `copy=False` serves the caller's own list, so rows added to it later are served too."""
        rows = list(rows) if copy else rows

        def handler(arguments):
            args = dict(arguments)
            limit = int(args.pop(LIMIT_PARAM, 100))
            offset = int(args.pop(OFFSET_PARAM, 0))
            picked = rows
            if filters:
                for key, wanted in args.items():
                    picked = [r for r in picked if _matches(r, key, wanted)]
            page = picked[offset:offset + limit]
            return {"data": page, "total": len(picked), "limit": limit, "offset": offset}

        self._handlers[tool] = handler
        return self

    def handler(self, tool, fn):
        self._handlers[tool] = fn
        return self

    def fail(self, tool, exc):
        """Every call of `tool` raises `exc` (ToolError, HttpStatus or an OSError)."""
        return self.fail_when(tool, lambda arguments: True, exc)

    def fail_when(self, tool, predicate, exc):
        self._fail_when.append((tool, predicate, exc))
        return self

    def expire(self):
        """The token is rejected: HTTP 401 everywhere (except login)."""
        self._expired = True
        return self

    def down(self, exc=None):
        """The host can't be reached: the transport raises on every call."""
        self._down = exc or ConnectionRefusedError("connection refused")
        return self

    # ---- serving
    def handle(self, call):
        if self._down is not None:
            raise self._down
        if call.path == "/api/auth/login":
            if call.method != "POST":
                return 405, "application/json", "{}"
            try:
                payload = self.login_handler(call.json or {})
            except HttpStatus as e:
                return e.status, "application/json", json.dumps({"error": e.text})
            return 200, "application/json", json.dumps(payload)
        if not self._authorized(call):
            return 401, "application/json", json.dumps({"error": "unauthorized"})
        if call.path == "/api/auth/me":
            me = self.me
            if isinstance(me, Exception):
                if isinstance(me, HttpStatus):
                    return me.status, "application/json", json.dumps({"error": me.text})
                raise me
            return 200, "application/json", json.dumps(me)
        if call.path == "/api/mcp":
            return self._mcp(call)
        return 404, "text/plain", "not found"

    def _authorized(self, call):
        if self._expired:
            return False
        if self.token is None:
            return True
        return call.headers.get("Authorization") == "Bearer " + self.token

    def _mcp(self, call):
        body = call.json or {}
        if body.get("method") != "tools/call":
            return self._reply(body, error={"code": -32601, "message": "method not found"})
        name = call.tool
        arguments = call.arguments or {}
        for tool, predicate, exc in self._fail_when:
            if tool == name and predicate(arguments):
                return self._raise(body, exc)
        handler = self._handlers.get(name)
        if handler is None:
            return self._reply(body, result={"content": [{"type": "text", "text": "unknown tool " + str(name)}],
                                             "isError": True})
        try:
            payload = handler(arguments)
        except Exception as exc:  # ToolError, HttpStatus, OSError from a handler
            return self._raise(body, exc)
        return self._reply(body, result={"content": [{"type": "text", "text": json.dumps(payload)}],
                                         "isError": False})

    def _raise(self, body, exc):
        if isinstance(exc, ToolError):
            return self._reply(body, result={"content": [{"type": "text", "text": str(exc)}], "isError": True})
        if isinstance(exc, HttpStatus):
            return exc.status, "application/json", json.dumps({"error": exc.text})
        raise exc

    def _reply(self, body, result=None, error=None):
        message = {"jsonrpc": "2.0", "id": body.get("id")}
        if error is not None:
            message["error"] = error
        else:
            message["result"] = result
        text = json.dumps(message)
        if self.event_stream:
            return 200, "text/event-stream", "event: message\ndata: " + text + "\n\n"
        return 200, "application/json", text


# --------------------------------------------------------------------------- transport

class Call:
    """One recorded request."""

    def __init__(self, method, url, headers, body, timeout):
        self.method, self.url, self.headers, self.timeout = method, url, dict(headers or {}), timeout
        self.body = body
        parsed = urlparse(url)
        self.path = parsed.path
        try:
            self.json = json.loads(body.decode() if isinstance(body, bytes) else body) if body else None
        except ValueError:
            self.json = None
        params = self.json.get("params") or {} if isinstance(self.json, dict) else {}
        self.tool = params.get("name") if self.path == "/api/mcp" else None
        self.arguments = params.get("arguments") if self.path == "/api/mcp" else None

    @property
    def base(self):
        parsed = urlparse(self.url)
        return parsed.scheme + "://" + parsed.netloc


class FakeTransport:
    """Callable transport that serves FakeServers by base URL and records every call."""

    def __init__(self):
        self.servers = {}
        self.calls = []
        self._lock = threading.Lock()

    def server(self, base_url, name=None, token=None, me=None):
        """Get (or create) the fake server answering at `base_url`."""
        base_url = base_url.rstrip("/")
        if base_url not in self.servers:
            self.servers[base_url] = FakeServer(name or base_url, token, me)
        return self.servers[base_url]

    def add_server(self, base_url, server):
        """Register any object with `handle(call) -> (status, content_type, text)` at `base_url`."""
        self.servers[base_url.rstrip("/")] = server
        return server

    def __call__(self, method, url, headers, body, timeout):
        call = Call(method, url, headers, body, timeout)
        with self._lock:
            self.calls.append(call)
        for base, server in self.servers.items():
            if url == base or url.startswith(base + "/"):
                return server.handle(call)
        raise ConnectionRefusedError("no fake server at " + url)

    # ---- inspecting
    def tool_calls(self, name=None, base=None):
        found = [c for c in self.calls if c.tool is not None]
        if name is not None:
            found = [c for c in found if c.tool == name]
        if base is not None:
            found = [c for c in found if c.base == base.rstrip("/")]
        return found

    def tool_names(self):
        return {c.tool for c in self.calls if c.tool is not None}

    def calls_to(self, base):
        base = base.rstrip("/")
        return [c for c in self.calls if c.url == base or c.url.startswith(base + "/")]


# --------------------------------------------------------------------------- the model gateway

def model_reply(**overrides):
    """The JSON object a well-behaved model returns (design: Architecture, draft_writer). Override keys."""
    reply = {"summary": "Asks a question and wants an answer.", "summary_language": "English",
             "needs_decision": False, "message_language": "English", "reply_language": "English",
             "greeting": "Hello,", "body": "Thanks for your message. I will get back to you soon."}
    reply.update(overrides)
    return reply


class FakeModelServer:
    """A fake OpenAI-compatible gateway: answers POST <base>/v1/chat/completions.

    `responder(request_json) -> dict | str | Exception`: a dict or str becomes the assistant message
    content (a dict as JSON text); an `HttpStatus` answers that status; any other Exception is raised
    (an OSError is "can't connect"). Every request is kept in `requests` (parsed body) and `calls`.
    """

    def __init__(self, responder=None):
        self.responder = responder or (lambda request: model_reply())
        self.requests = []
        self.calls = []
        self._lock = threading.Lock()

    def down(self, exc=None):
        exc = exc or ConnectionRefusedError("connection refused")
        self.responder = lambda request: exc
        return self

    def handle(self, call):
        if call.method != "POST" or not call.path.endswith("/chat/completions"):
            return 404, "text/plain", "not found"
        request = call.json or {}
        with self._lock:
            self.requests.append(request)
            self.calls.append(call)
        out = self.responder(request)
        if isinstance(out, HttpStatus):
            return out.status, "application/json", json.dumps({"error": out.text})
        if isinstance(out, Exception):
            raise out
        content = out if isinstance(out, str) else json.dumps(out)
        return 200, "application/json", json.dumps(
            {"id": "chatcmpl-1", "object": "chat.completion", "model": request.get("model"),
             "choices": [{"index": 0, "finish_reason": "stop",
                          "message": {"role": "assistant", "content": content}}]})


class FakeGlcServer:
    """A fake glc gateway as the live probe found it (plan task 15): NO `/v1/chat/completions` (404);
    its own `POST <base>/v1/chat` takes `{model, system, messages, max_tokens, temperature}` and answers
    `{"provider", "model", "text", ...}` where `text` may wrap the JSON in a code fence. Like glc it drops
    `system`-role messages inside `messages`: only the `system` field carries the prompt. Same responder
    contract as FakeModelServer. `requests` holds the bodies of /v1/chat calls as glc keeps them (system
    messages dropped; the responder gets these); `sent` the bodies as they arrived; `calls` every call
    (including the 404 ones)."""

    def __init__(self, responder=None, fence=True):
        self.responder = responder or (lambda request: model_reply())
        self.fence = fence
        self.requests = []
        self.sent = []
        self.calls = []
        self._lock = threading.Lock()

    def handle(self, call):
        with self._lock:
            self.calls.append(call)
        if call.method != "POST" or not call.path.endswith("/v1/chat"):
            return 404, "application/json", json.dumps({"detail": "Not Found"})
        sent = call.json or {}
        request = dict(sent, messages=[m for m in sent.get("messages", []) if m.get("role") != "system"])
        with self._lock:
            self.sent.append(sent)
            self.requests.append(request)
        out = self.responder(request)
        if isinstance(out, HttpStatus):
            return out.status, "application/json", json.dumps({"detail": out.text})
        if isinstance(out, Exception):
            raise out
        text = out if isinstance(out, str) else json.dumps(out)
        if self.fence and not isinstance(out, str):
            text = "```json\n" + text + "\n```"
        return 200, "application/json", json.dumps(
            {"provider": "gemini_1", "model": request.get("model"), "text": text, "tool_calls": [],
             "stop_reason": "end_turn", "input_tokens": 1, "output_tokens": 1})


def prompt_text(request):
    """All message contents of a chat/completions request, joined: what the model was shown."""
    return "\n".join(str(m.get("content", "")) for m in request.get("messages", []))


class FakeDraftWriter:
    """Stands in for draft_writer.DraftWriter: `write(message, user, signature)` and `stats()`.

    `outcome`: None (a valid Draft for every message), a dict {message.id: outcome}, or a callable
    `(message, user, signature) -> Draft | DraftFailed`; an Exception instance (or class) is raised.
    `clock` + `seconds`: each call advances the FakeClock, so the runner's 45 s deadline can be crossed
    without sleeping. `calls` records (message id, signature) in call order; `max_concurrent` is the
    most calls running at once (use `hold` to keep calls open: an Event they wait on). The fake does not
    enforce the deadline it is given (the real writer does); it records it in `deadlines`.
    """

    def __init__(self, outcome=None, clock=None, seconds=0, hold=None, stats=None):
        self.outcome, self.clock, self.seconds, self.hold = outcome, clock, seconds, hold
        self._stats = stats or {"calls": 0, "failures": 0, "unreachable": False}
        self.calls = []
        self.deadlines = []     # the `deadline` each call was given
        self.max_concurrent = 0
        self._running = 0
        self._lock = threading.Lock()

    def write(self, message, user, signature, deadline=None):
        from ea_briefing.draft_writer import Draft
        with self._lock:
            self.calls.append((message.id, signature))
            self.deadlines.append(deadline)
            self._running += 1
            self.max_concurrent = max(self.max_concurrent, self._running)
        try:
            if self.hold is not None:
                self.hold.wait(5)
            if self.clock is not None and self.seconds:
                self.clock.advance(self.seconds)
            out = self.outcome
            if isinstance(out, dict):
                out = out.get(message.id)
            if callable(out) and not isinstance(out, BaseException) and not isinstance(out, type):
                out = out(message, user, signature)
            if isinstance(out, BaseException) or (isinstance(out, type) and issubclass(out, BaseException)):
                raise out
            if out is None:
                out = Draft("Hello,\n\nThanks, I will reply soon.\n\n" + (signature or user.name),
                            summary="Summary of: " + message.subject)
            return out
        finally:
            with self._lock:
                self._running -= 1

    def stats(self):
        return dict(self._stats)


# --------------------------------------------------------------------------- the approval step's input

class TtyInput(io.StringIO):
    """Scripted stdin that says it is a terminal (`isatty()` is true), so the `e` choice is allowed.
    `before_read(n)` runs before the n-th `readline` (1-based): a test uses it to change the world while
    the user is "reading" an offer. `reads` counts the lines asked for."""

    def __init__(self, text="", before_read=None):
        super().__init__(text)
        self.before_read = before_read
        self.reads = 0

    def isatty(self):
        return True

    def readline(self, *args):
        self.reads += 1
        if self.before_read is not None:
            self.before_read(self.reads)
        return super().readline(*args)


class FakeEditor:
    """Stands in for the editor the approval step opens: `editor(text) -> the edited text`.

    `results` is the scripted answer per call, in order: a string (the text after editing), or the word
    `"missing"` for an editor that can't be started (the step must then fall back to typed lines;
    imported when called, so this file loads before approval.py exists), or an Exception instance to raise
    (a KeyboardInterrupt is Ctrl-C). `texts` records what each call was given. Out of results, the text is
    returned unchanged."""

    def __init__(self, *results):
        self.results = list(results)
        self.texts = []

    def __call__(self, text):
        self.texts.append(text)
        if not self.results:
            return text
        out = self.results.pop(0)
        if out == "missing":
            from ea_briefing.approval import EditorUnavailable
            raise EditorUnavailable("no editor")
        if isinstance(out, BaseException):
            raise out
        return out


# --------------------------------------------------------------------------- the default world

READ_TOOLS = {"Mailbox.list", "EmailAccount.list", "EmailMessage.list", "EmailMessage.get",
              "CalendarEvent.list", "EventAttendee.list", "Task.list"}

MAILBOX_ROW = {"id": "mb-1", "address": "team28c@example.test"}


def default_world(mailbox=False, messages=None):
    """Both systems up, tokens TOKEN_SURYODAYA / TOKEN_KEYSTONE, fixture events and tasks.

    `mailbox=True` gives both systems a mailbox, otherwise Mailbox.list and EmailAccount.list
    are empty ("no mailbox for this login"). With a mailbox the inbox holds `messages` ({system name:
    [rows]}, default none). Tests change what they need on the returned servers.
    Returns (transport, suryodaya_server, keystone_server).
    """
    transport = FakeTransport()
    events, tasks = load_fixture("events.json"), load_fixture("tasks.json")
    servers = []
    for name, base, token in (("suryodaya", SURYODAYA_BASE, TOKEN_SURYODAYA),
                              ("keystone", KEYSTONE_BASE, TOKEN_KEYSTONE)):
        server = transport.server(base, name, token, load_fixture("me_%s.json" % name))
        server.rows("Mailbox.list", [MAILBOX_ROW] if mailbox else [])
        server.rows("EmailAccount.list", [MAILBOX_ROW] if mailbox else [])
        if mailbox:
            serve_mail(server, (messages or {}).get(name, []))
        server.rows("CalendarEvent.list", events[name])
        server.rows("Task.list", tasks[name])
        servers.append(server)
    return transport, servers[0], servers[1]
