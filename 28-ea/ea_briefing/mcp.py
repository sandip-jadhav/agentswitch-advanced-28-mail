"""The MCP client and the safety boundary of the briefing (SD-D-5, G5-AC1).

Request shape copied from `33-red-team/replay/mcp_client.py`: JSON-RPC `tools/call` POSTed to
`<base>/api/mcp` with `Authorization: Bearer`, `Accept: application/json, text/event-stream`;
JSON or `data:` event-stream replies. Added here: typed errors, paging and a read allowlist.

Every safety claim rests on two methods. `McpClient.call`: a tool outside READ_TOOLS raises `ToolNotAllowed`
before the transport is touched. `McpClient.create_draft`: the one write, `EmailMessage.create` of an unsent
draft, raises `ToolNotAllowed` before the transport unless it gets a fresh single-use `ApprovalToken` (which only
the approval loop mints, once per reply the user chose to save), `folder="drafts"` with `is_draft=True`, and no
argument outside CREATE_ARGS. The tool name is not an argument of `create_draft`, and `call` and READ_TOOLS stay
read-only. `_post`, the one method that reaches the transport, has its own guard: a READ_TOOLS name, or
CREATE_TOOL while `create_draft` is spending a token.

The transport is a callable `transport(method, url, headers, body, timeout) -> (status,
content_type, text)`. It returns HTTP error statuses as values and raises `OSError` (which
includes `urllib.error.URLError` and timeouts) when the host can't be reached. A protocol error
(`http.client.HTTPException`, e.g. `BadStatusLine`) counts as unreachable too.
"""
import http.client
import itertools
import json
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any

DEFAULT_TIMEOUT = 15  # seconds per platform request

# ---- Paging, as probed live on both systems (2026-11-06): every list tool takes `limit` (0-1000, default
# 20) and `offset` (the schemas forbid other arguments) and reply {"data": [...], "total": n, "limit": n,
# "offset": n} with NO `has_more` (so list_all stops on `total`). If the platform changes, change only
# these lines.
PAGE_SIZE = 100
LIMIT_PARAM = "limit"
OFFSET_PARAM = "offset"
MAX_PAGES = 1000

CREATE_TOOL = "EmailMessage.create"   # the one write: an unsent draft (SD-D-6)
DRAFTS_FOLDER = "drafts"
# The only arguments a create may carry: exactly what `approval._arguments` builds. Anything else (`status`,
# `sent_at`, `server_folder`, `is_read`, `labels`, ...) could turn a draft into something else.
CREATE_ARGS = frozenset({"company_id", "mailbox_id", "from_email", "to", "subject", "body_text", "folder",
                         "is_draft", "thread_id", "in_reply_to", "references"})

# The reads the briefing needs. Nothing else may be called through `call`; the one write is `create_draft`.
READ_TOOLS = frozenset({
    "Mailbox.list", "EmailAccount.list", "EmailMessage.list", "EmailMessage.get",
    "CalendarEvent.list",
    "EventAttendee.list",  # the design's attendee fallback (SD-D-9): only if event rows carry no attendees
    "Task.list",
})


class McpError(Exception):
    """A platform call failed in a way that becomes a plain message for the user."""


class LoginExpired(McpError):
    """HTTP 401: the token is missing, wrong or expired."""


class Unreachable(McpError):
    """Connection failure or timeout."""


class ToolFailed(McpError):
    """An MCP error, an isError reply, an unexpected HTTP status or an unreadable reply."""

    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


class ToolNotAllowed(Exception):
    """A programming error: code asked for a tool outside the allowlist. Deliberately not an
    McpError, so no reader turns it into a harmless 'missing part'."""


class ApprovalToken:
    """Proof that the user chose to save one reply. Minted by the approval loop per approved save, accepted
    once by `McpClient.create_draft`, then spent (also when the call fails: a failed save is never retried)."""

    def __init__(self):
        self.used = False


@dataclass
class PartResult:
    """One part of a system's briefing: its data, or a plain reason it is missing."""
    ok: bool
    data: Any = None
    reason: str = None
    expired: bool = False   # a missing part whose cause was LoginExpired (a 401 fails the whole system)

    @classmethod
    def complete(cls, data):
        return cls(True, data, None)

    @classmethod
    def missing(cls, reason, expired=False):
        return cls(False, None, reason, expired)


def failure_message(label, exc, part=None):
    """The PRD's plain-language line for a failed call, for one system (and one part)."""
    if isinstance(exc, LoginExpired):
        return "%s: login expired — log in again" % label
    if isinstance(exc, Unreachable):
        return "%s: can't be reached right now — try again later" % label
    reason = exc.reason if isinstance(exc, ToolFailed) else str(exc) or type(exc).__name__
    who = "%s %s" % (label, part) if part else label
    return "%s: couldn't be read (reason: %s) — report it if it repeats" % (who, reason)


def urllib_transport(method, url, headers, body, timeout):
    """The real transport: urllib, HTTP errors returned as (status, type, text)."""
    request = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as r:
            return r.status, r.headers.get("Content-Type", ""), r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        return e.code, e.headers.get("Content-Type", "") if e.headers else "", e.read().decode("utf-8", "replace")
    except http.client.HTTPException as e:  # BadStatusLine, RemoteDisconnected, IncompleteRead: a broken connection
        raise OSError("%s: %s" % (type(e).__name__, e) if str(e) else type(e).__name__) from None


def decode_body(content_type, text):
    """Parse a JSON or text/event-stream body. Returns the JSON value, or raises ValueError."""
    if "text/event-stream" in (content_type or "") or text.lstrip().startswith(("data:", "event:")):
        messages = []
        for line in text.splitlines():
            if line.startswith("data:"):
                chunk = line[5:].strip()
                if chunk and chunk != "[DONE]":
                    try:
                        messages.append(json.loads(chunk))
                    except ValueError:
                        pass
        replies = [m for m in messages if isinstance(m, dict) and ("result" in m or "error" in m)]
        if not replies:
            raise ValueError("no reply in event stream")
        return replies[-1]
    return json.loads(text)


class McpClient:
    def __init__(self, base_url, token, transport=None, timeout=DEFAULT_TIMEOUT, page_size=PAGE_SIZE,
                 label="System"):
        self.base_url = base_url.rstrip("/")
        self.label = label  # the system's display name, used in the readers' failure messages
        self._token = token
        self._transport = transport or urllib_transport
        self.timeout = timeout
        self.page_size = page_size
        self._ids = itertools.count(1)
        self._spending = None   # the token `create_draft` is spending right now

    def call(self, name, arguments=None):
        """Call one allowed read tool and return its decoded payload."""
        if name not in READ_TOOLS:
            raise ToolNotAllowed("tool %r is not on the read allowlist" % (name,))
        return self._post(name, arguments)

    def create_draft(self, arguments, token):
        """Save one unsent draft (`EmailMessage.create`) and return the decoded reply. Refused, before the
        transport is touched, unless `token` is a fresh `ApprovalToken` and the arguments say
        `folder="drafts"` and `is_draft=True` and carry nothing outside CREATE_ARGS. The token is spent by the
        attempt, whatever it returns."""
        if not isinstance(token, ApprovalToken) or token.used:
            raise ToolNotAllowed("a draft is saved only with a fresh approval token")
        arguments = dict(arguments or {})
        if arguments.get("folder") != DRAFTS_FOLDER or arguments.get("is_draft") is not True:
            raise ToolNotAllowed("only an unsent draft in the drafts folder may be created")
        extra = sorted(set(arguments) - CREATE_ARGS)
        if extra:
            raise ToolNotAllowed("a draft may not carry %s" % ", ".join(extra))
        token.used = True
        self._spending = token
        try:
            return self._post(CREATE_TOOL, arguments)
        finally:
            self._spending = None

    def _post(self, name, arguments):
        if name not in READ_TOOLS and not (name == CREATE_TOOL and self._spending is not None
                                           and self._spending.used):
            raise ToolNotAllowed("tool %r may not be posted" % (name,))
        body = json.dumps({"jsonrpc": "2.0", "id": next(self._ids), "method": "tools/call",
                           "params": {"name": name, "arguments": dict(arguments or {})}}).encode()
        headers = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
                   "Authorization": "Bearer " + str(self._token)}
        try:
            status, content_type, text = self._transport("POST", self.base_url + "/api/mcp", headers,
                                                         body, self.timeout)
        except (OSError, http.client.HTTPException) as e:
            raise Unreachable(type(e).__name__ + (": " + str(e) if str(e) else "")) from None
        if status == 401:
            raise LoginExpired()
        if not 200 <= status < 300:
            raise ToolFailed("HTTP %d" % status)
        try:
            reply = decode_body(content_type, text)
        except ValueError:
            raise ToolFailed("unreadable reply from the platform") from None
        return self._payload(reply)

    @staticmethod
    def _payload(reply):
        if not isinstance(reply, dict):
            raise ToolFailed("unexpected reply from the platform")
        if "error" in reply:
            err = reply["error"]
            raise ToolFailed(str(err.get("message", err)) if isinstance(err, dict) else str(err))
        result = reply.get("result")
        if not isinstance(result, dict) or "content" not in result:
            return result
        text = "\n".join(c.get("text", "") for c in result.get("content") or []
                         if isinstance(c, dict) and c.get("type") == "text")
        if result.get("isError"):
            raise ToolFailed(text or "the tool reported an error")
        try:
            return json.loads(text)
        except ValueError:
            return text

    def list_all(self, name, arguments=None):
        """All rows of a list tool, page after page. `arguments` are the filters."""
        rows, offset, previous = [], 0, None
        for _ in range(MAX_PAGES):
            args = dict(arguments or {})
            args[LIMIT_PARAM], args[OFFSET_PARAM] = self.page_size, offset
            payload = self.call(name, args)
            if isinstance(payload, list):  # a bare list is one page; a full page may be followed by more
                page, more = payload, len(payload) >= self.page_size
            else:
                page = payload.get("data") if isinstance(payload, dict) else None
                if not isinstance(page, list):
                    raise ToolFailed("unexpected list reply from the platform")
                more = payload.get("has_more")
            if page and page == previous:
                raise ToolFailed("paging did not advance (the server repeated a page)")
            rows.extend(page)
            if more is None:
                total = payload.get("total")
                more = len(page) >= self.page_size and (total is None or len(rows) < total)
            if not page or not more:
                return rows
            previous, offset = page, offset + len(page)
        raise ToolFailed("paging did not finish")
