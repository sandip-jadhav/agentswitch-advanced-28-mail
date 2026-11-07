"""mcp.py: request shape, replies, typed errors, paging and the read allowlist (P-B-18, G5-AC1)."""
import http.client
import json
import unittest
import urllib.error
import urllib.request
from unittest import mock

from ea_briefing import mcp
from ea_briefing.mcp import (ApprovalToken, LoginExpired, McpClient, PartResult, ToolFailed, ToolNotAllowed,
                             Unreachable, failure_message)
from tests import fakes
from tests.fakes import FakeTransport, HttpStatus, ToolError

BASE = "https://fake.test"
TOKEN = "tok-secret-123"


def make(rows=None, page_size=100, **server_kw):
    transport = FakeTransport()
    server = transport.server(BASE, "fake", TOKEN)
    if rows is not None:
        server.rows("Task.list", rows, **server_kw)
    return McpClient(BASE, TOKEN, transport, page_size=page_size), transport, server


class RequestShape(unittest.TestCase):
    def test_tools_call_is_posted_to_api_mcp_with_bearer_token(self):
        client, transport, server = make([])
        client.call("Task.list", {"status": "open"})
        (call,) = transport.calls
        self.assertEqual((call.method, call.url), ("POST", BASE + "/api/mcp"))
        self.assertEqual(call.headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(call.headers["Accept"], "application/json, text/event-stream")
        self.assertEqual(call.headers["Content-Type"], "application/json")
        self.assertEqual(call.json["jsonrpc"], "2.0")
        self.assertEqual(call.json["method"], "tools/call")
        self.assertEqual(call.json["params"], {"name": "Task.list", "arguments": {"status": "open"}})
        self.assertEqual(call.timeout, 15)

    def test_trailing_slash_on_the_base_url_is_harmless(self):
        transport = FakeTransport()
        transport.server(BASE, "fake", TOKEN).rows("Task.list", [])
        McpClient(BASE + "/", TOKEN, transport).call("Task.list", {})
        self.assertEqual(transport.calls[0].url, BASE + "/api/mcp")

    def test_the_client_carries_the_system_label_for_failure_messages(self):
        self.assertEqual(McpClient(BASE, TOKEN, FakeTransport(), label="Keystone").label, "Keystone")

    def test_request_ids_differ(self):
        client, transport, _ = make([])
        client.call("Task.list", {})
        client.call("Task.list", {})
        self.assertNotEqual(transport.calls[0].json["id"], transport.calls[1].json["id"])


class Replies(unittest.TestCase):
    def test_json_reply_is_decoded_to_the_tool_payload(self):
        client, _, server = make()
        server.handler("Mailbox.list", lambda a: {"data": [{"id": "m1"}], "total": 1, "has_more": False})
        self.assertEqual(client.call("Mailbox.list", {})["data"], [{"id": "m1"}])

    def test_event_stream_reply_is_decoded_too(self):
        client, _, server = make([{"id": "t1"}])
        server.event_stream = True
        self.assertEqual(client.call("Task.list", {})["data"], [{"id": "t1"}])

    def test_text_that_is_not_json_is_returned_as_text(self):
        client, _, server = make()
        server.handler("Mailbox.list", lambda a: "plain words")
        self.assertEqual(client.call("Mailbox.list", {}), "plain words")


class TypedErrors(unittest.TestCase):
    def test_http_401_is_login_expired(self):
        client, _, server = make([])
        server.expire()
        with self.assertRaises(LoginExpired):
            client.call("Task.list", {})

    def test_wrong_token_is_login_expired(self):
        transport = FakeTransport()
        transport.server(BASE, "fake", TOKEN).rows("Task.list", [])
        with self.assertRaises(LoginExpired):
            McpClient(BASE, "another-token", transport).call("Task.list", {})

    def test_connection_problems_and_timeouts_are_unreachable(self):
        for exc in (ConnectionRefusedError("refused"), TimeoutError("timed out"),
                    urllib.error.URLError("dns failure")):
            client, _, server = make([])
            server.down(exc)
            with self.assertRaises(Unreachable, msg=repr(exc)):
                client.call("Task.list", {})

    def test_a_protocol_error_from_the_transport_is_unreachable(self):
        for exc in (http.client.BadStatusLine("garbage"), http.client.RemoteDisconnected("closed"),
                    http.client.IncompleteRead(b"x")):
            client, _, server = make([])
            server.down(exc)
            with self.assertRaises(Unreachable, msg=repr(exc)):
                client.call("Task.list", {})

    def test_the_real_transport_turns_http_client_errors_into_oserror(self):
        with mock.patch.object(urllib.request, "urlopen", side_effect=http.client.BadStatusLine("garbage")):
            with self.assertRaises(OSError):
                mcp.urllib_transport("GET", "https://x.test/", {}, None, 1)

    def test_unknown_host_in_the_transport_is_unreachable(self):
        with self.assertRaises(Unreachable):
            McpClient("https://nowhere.test", TOKEN, FakeTransport()).call("Task.list", {})

    def test_is_error_reply_is_tool_failed_with_the_platform_reason(self):
        client, _, server = make([])
        server.fail("Task.list", ToolError("table is locked"))
        with self.assertRaises(ToolFailed) as cm:
            client.call("Task.list", {})
        self.assertEqual(cm.exception.reason, "table is locked")

    def test_jsonrpc_error_is_tool_failed(self):
        reply = json.dumps({"jsonrpc": "2.0", "id": 1, "error": {"code": -32602, "message": "bad arguments"}})
        client = McpClient(BASE, TOKEN, lambda *a: (200, "application/json", reply))
        with self.assertRaises(ToolFailed) as cm:
            client.call("Task.list", {})
        self.assertEqual(cm.exception.reason, "bad arguments")

    def test_unknown_tool_on_the_server_is_tool_failed(self):
        client, _, _ = make()
        with self.assertRaises(ToolFailed) as cm:
            client.call("Task.list", {})  # the fake has no such tool: isError reply
        self.assertIn("unknown tool", cm.exception.reason)

    def test_other_http_statuses_are_tool_failed_with_the_status(self):
        for status in (403, 500, 502):
            client, _, server = make([])
            server.fail("Task.list", HttpStatus(status))
            with self.assertRaises(ToolFailed) as cm:
                client.call("Task.list", {})
            self.assertIn("HTTP %d" % status, cm.exception.reason)

    def test_unreadable_reply_is_tool_failed(self):
        client = McpClient(BASE, TOKEN, lambda *a: (200, "text/html", "<html>oops</html>"))
        with self.assertRaises(ToolFailed):
            client.call("Task.list", {})

    def test_the_token_never_appears_in_an_error(self):
        client, _, server = make([])
        server.fail("Task.list", ToolError("boom"))
        with self.assertRaises(ToolFailed) as cm:
            client.call("Task.list", {})
        self.assertNotIn(TOKEN, str(cm.exception) + cm.exception.reason)

    def test_typed_errors_share_a_base_class(self):
        for cls in (LoginExpired, Unreachable, ToolFailed):
            self.assertTrue(issubclass(cls, mcp.McpError))
        self.assertFalse(issubclass(ToolNotAllowed, mcp.McpError))


class Paging(unittest.TestCase):
    ROWS = [{"id": "r%d" % i, "kind": "a" if i % 2 else "b"} for i in range(5)]

    def test_list_all_pages_until_the_last_page(self):
        client, transport, _ = make(self.ROWS, page_size=2)
        rows = client.list_all("Task.list", {})
        self.assertEqual([r["id"] for r in rows], ["r0", "r1", "r2", "r3", "r4"])
        calls = transport.tool_calls("Task.list")
        self.assertEqual([c.arguments[fakes.OFFSET_PARAM] for c in calls], [0, 2, 4])
        self.assertEqual({c.arguments[fakes.LIMIT_PARAM] for c in calls}, {2})

    def test_list_all_passes_the_filters_on_every_page(self):
        client, transport, _ = make(self.ROWS, page_size=1)
        rows = client.list_all("Task.list", {"kind": "a"})
        self.assertEqual([r["id"] for r in rows], ["r1", "r3"])
        for call in transport.tool_calls("Task.list"):
            self.assertEqual(call.arguments["kind"], "a")

    def test_list_all_does_not_change_the_callers_arguments(self):
        client, _, _ = make(self.ROWS, page_size=2)
        args = {"kind": "a"}
        client.list_all("Task.list", args)
        self.assertEqual(args, {"kind": "a"})

    def test_empty_list_is_one_call(self):
        client, transport, _ = make([], page_size=2)
        self.assertEqual(client.list_all("Task.list", {}), [])
        self.assertEqual(len(transport.tool_calls()), 1)

    def test_exactly_one_full_page_stops_when_total_is_reached(self):
        client, transport, _ = make(self.ROWS[:2], page_size=2)
        self.assertEqual(len(client.list_all("Task.list", {})), 2)
        self.assertEqual(len(transport.tool_calls()), 1)

    def test_a_reply_without_has_more_stops_on_a_short_page(self):
        client, transport, server = make()
        server.handler("Task.list", lambda a: {"data": self.ROWS[:1]})
        self.assertEqual(len(client.list_all("Task.list", {})), 1)
        self.assertEqual(len(transport.tool_calls()), 1)

    def test_a_bare_list_reply_is_paged_like_any_other_until_a_short_page(self):
        rows = [{"id": "r%d" % i} for i in range(250)]
        client, transport, server = make(page_size=100)
        server.handler("Task.list", lambda a: rows[a[fakes.OFFSET_PARAM]:a[fakes.OFFSET_PARAM] + a[fakes.LIMIT_PARAM]])
        self.assertEqual(client.list_all("Task.list", {}), rows)
        self.assertEqual(len(transport.tool_calls()), 3)

    def test_a_bare_list_server_that_ignores_the_offset_cannot_loop_forever(self):
        client, _, server = make(page_size=2)
        server.handler("Task.list", lambda a: self.ROWS[:2])
        with self.assertRaises(ToolFailed):
            client.list_all("Task.list", {})

    def test_a_failure_on_page_two_raises(self):
        client, _, server = make(self.ROWS, page_size=2)
        server.fail_when("Task.list", lambda a: a[fakes.OFFSET_PARAM] >= 2, ToolError("page 2 broke"))
        with self.assertRaises(ToolFailed) as cm:
            client.list_all("Task.list", {})
        self.assertEqual(cm.exception.reason, "page 2 broke")

    def test_a_server_that_ignores_the_offset_cannot_loop_forever(self):
        client, transport, server = make(page_size=2)
        server.handler("Task.list", lambda a: {"data": self.ROWS[:2], "total": 5, "has_more": True})
        with self.assertRaises(ToolFailed) as cm:
            client.list_all("Task.list", {})
        self.assertIn("paging", cm.exception.reason)
        self.assertEqual(len(transport.tool_calls()), 2)


class ReadAllowlist(unittest.TestCase):
    """P-B-18: anything outside the allowlist raises before the transport is touched."""

    def test_writes_and_sends_raise_with_zero_transport_calls(self):
        client, transport, _ = make([])
        for name, args in (("EmailMessage.reply", {"id": "m1", "body_text": "hi"}),
                           ("EmailMessage.send_email", {"to": "x@example.test"}),
                           ("Task.update", {"id": "t1", "status": "done"}),
                           ("Task.delete", {"id": "t1"}),
                           ("CalendarEvent.create", {}),
                           ("EmailMessage.save_draft", {"id": "d1"}),
                           ("EmailMessage.create", {"folder": "drafts", "is_draft": True, "body_text": "x"}),
                           ("Nonsense.tool", {})):
            with self.assertRaises(ToolNotAllowed, msg=name):
                client.call(name, args)
        self.assertEqual(transport.calls, [])

    def test_list_all_checks_the_allowlist_too(self):
        client, transport, _ = make([])
        with self.assertRaises(ToolNotAllowed):
            client.list_all("EmailMessage.reply", {})
        self.assertEqual(transport.calls, [])

    def test_create_stays_refused_even_with_the_approval_looking_arguments(self):
        client, transport, _ = make([])
        with self.assertRaises(ToolNotAllowed):
            client.call("EmailMessage.create", {"folder": "drafts", "is_draft": True, "approval": object()})
        self.assertEqual(transport.calls, [])

    def test_the_reads_the_briefing_needs_are_allowed(self):
        for name in ("Mailbox.list", "EmailAccount.list", "EmailMessage.list", "EmailMessage.get",
                     "CalendarEvent.list", "EventAttendee.list", "Task.list"):
            self.assertIn(name, mcp.READ_TOOLS)

    def test_the_allowlist_is_exactly_the_fakes_read_set(self):
        self.assertEqual(set(mcp.READ_TOOLS), fakes.READ_TOOLS)

    def test_the_refusal_is_not_swallowed_by_mcp_error_handlers(self):
        client, _, _ = make([])
        try:
            client.call("Task.update", {})
        except mcp.McpError:
            self.fail("ToolNotAllowed must not be an McpError: readers would turn it into a missing part")
        except ToolNotAllowed:
            pass


DRAFT_ARGS = {"company_id": "co-1", "from_email": "me@box.test", "to": "p@example.test", "subject": "Re: Hi",
              "body_text": "Hello,\n\nThanks.\n\nMe", "folder": "drafts", "is_draft": True}


def make_mail():
    transport = FakeTransport()
    server = transport.server(BASE, "fake", TOKEN)
    fakes.serve_mail(server, [])
    return McpClient(BASE, TOKEN, transport, label="Keystone"), transport, server


class CreateDraft(unittest.TestCase):
    """P-B-18 and P-B-40: the one write, refused before the transport unless every condition holds."""

    def test_a_fresh_token_and_a_drafts_folder_arguments_make_one_create_call(self):
        client, transport, server = make_mail()
        reply = client.create_draft(dict(DRAFT_ARGS), ApprovalToken())
        (call,) = transport.calls
        self.assertEqual((call.method, call.url, call.tool), ("POST", BASE + "/api/mcp", "EmailMessage.create"))
        self.assertEqual(call.arguments, DRAFT_ARGS)
        self.assertEqual(call.headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(reply["id"], "new-1")      # the decoded payload comes back
        self.assertEqual(len(server.created), 1)

    def test_the_arguments_are_not_changed(self):
        client, _, _ = make_mail()
        args = dict(DRAFT_ARGS)
        client.create_draft(args, ApprovalToken())
        self.assertEqual(args, DRAFT_ARGS)

    def test_without_a_real_token_it_raises_and_the_transport_is_untouched(self):
        for bad in (None, "approved", True, object(), {"approved": True}, ApprovalToken):
            client, transport, _ = make_mail()
            with self.assertRaises(ToolNotAllowed, msg=repr(bad)):
                client.create_draft(dict(DRAFT_ARGS), bad)
            self.assertEqual(transport.calls, [], repr(bad))

    def test_a_token_works_once(self):
        client, transport, _ = make_mail()
        token = ApprovalToken()
        client.create_draft(dict(DRAFT_ARGS), token)
        with self.assertRaises(ToolNotAllowed):
            client.create_draft(dict(DRAFT_ARGS), token)
        self.assertEqual(len(transport.calls), 1)

    def test_each_token_is_its_own(self):
        client, transport, _ = make_mail()
        client.create_draft(dict(DRAFT_ARGS), ApprovalToken())
        client.create_draft(dict(DRAFT_ARGS), ApprovalToken())
        self.assertEqual(len(transport.calls), 2)

    def test_a_token_is_spent_even_when_the_call_fails_so_a_failed_save_is_never_retried(self):
        client, transport, server = make_mail()
        server.fail("EmailMessage.create", ToolError("refused"))
        token = ApprovalToken()
        with self.assertRaises(ToolFailed):
            client.create_draft(dict(DRAFT_ARGS), token)
        with self.assertRaises(ToolNotAllowed):
            client.create_draft(dict(DRAFT_ARGS), token)
        self.assertEqual(len(transport.calls), 1)

    def test_only_folder_drafts_is_accepted(self):
        for folder in (None, "inbox", "sent", "outbox", "scheduled", "Drafts", "drafts "):
            client, transport, _ = make_mail()
            args = dict(DRAFT_ARGS)
            if folder is None:
                del args["folder"]
            else:
                args["folder"] = folder
            with self.assertRaises(ToolNotAllowed, msg=repr(folder)):
                client.create_draft(args, ApprovalToken())
            self.assertEqual(transport.calls, [], repr(folder))

    def test_only_is_draft_true_is_accepted(self):
        for value in (None, False, "true", 1, 0, "yes"):
            client, transport, _ = make_mail()
            args = dict(DRAFT_ARGS)
            if value is None:
                del args["is_draft"]
            else:
                args["is_draft"] = value
            with self.assertRaises(ToolNotAllowed, msg=repr(value)):
                client.create_draft(args, ApprovalToken())
            self.assertEqual(transport.calls, [], repr(value))

    def test_any_argument_the_approval_step_does_not_build_is_refused_before_the_transport(self):
        """B1: the create may not carry a send, a status or any other field the step never sets."""
        for extra in ({"status": "sent"}, {"sent_at": "2026-11-07T09:00:00Z"}, {"server_folder": "Sent"},
                      {"is_read": True}, {"is_answered": True}, {"labels": ["x"]}, {"received_at": "2026-11-07"},
                      {"cc": "a@box.test"}, {"body_html": "<p>x</p>"}):
            client, transport, _ = make_mail()
            token = ApprovalToken()
            with self.assertRaises(ToolNotAllowed, msg=repr(extra)):
                client.create_draft(dict(DRAFT_ARGS, **extra), token)
            self.assertEqual(transport.calls, [], repr(extra))
            self.assertFalse(token.used, repr(extra))

    def test_the_create_arguments_are_exactly_what_the_approval_step_builds(self):
        from ea_briefing import approval
        self.assertEqual(mcp.CREATE_ARGS, frozenset({
            "company_id", "mailbox_id", "from_email", "to", "subject", "body_text", "folder", "is_draft",
            "thread_id", "in_reply_to", "references"}))
        client, transport, _ = make_mail()
        client.create_draft({k: DRAFT_ARGS.get(k, "x") for k in mcp.CREATE_ARGS} | {"is_draft": True,
                                                                                    "folder": "drafts"},
                            ApprovalToken())
        self.assertEqual(len(transport.calls), 1)

    def test_post_takes_only_a_read_tool_or_the_create_that_create_draft_just_spent_a_token_on(self):
        """B2."""
        client, transport, _ = make_mail()
        for name in ("EmailMessage.send_email", "EmailMessage.create", "Task.update", "", None):
            with self.assertRaises(ToolNotAllowed, msg=repr(name)):
                client._post(name, {})
        self.assertEqual(transport.calls, [])
        client._post("EmailMessage.list", {})
        self.assertEqual(len(transport.calls), 1)
        client.create_draft(dict(DRAFT_ARGS), ApprovalToken())     # the create is let through only inside this
        self.assertEqual(len(transport.calls), 2)
        with self.assertRaises(ToolNotAllowed):                     # ...and the permission ends with the attempt
            client._post("EmailMessage.create", dict(DRAFT_ARGS))
        self.assertEqual(len(transport.calls), 2)

    def test_a_refusal_for_bad_arguments_does_not_spend_the_token(self):
        client, transport, _ = make_mail()
        token = ApprovalToken()
        with self.assertRaises(ToolNotAllowed):
            client.create_draft(dict(DRAFT_ARGS, folder="sent"), token)
        client.create_draft(dict(DRAFT_ARGS), token)
        self.assertEqual(len(transport.calls), 1)

    def test_call_and_the_read_allowlist_stay_read_only(self):
        """The write path is a separate method: `call` still refuses create, token or not."""
        client, transport, _ = make_mail()
        self.assertNotIn("EmailMessage.create", mcp.READ_TOOLS)
        for name in ("EmailMessage.create", "EmailMessage.reply", "EmailMessage.send_email", "Task.update"):
            with self.assertRaises(ToolNotAllowed, msg=name):
                client.call(name, dict(DRAFT_ARGS, approval=ApprovalToken()))
        self.assertEqual(transport.calls, [])

    def test_create_draft_has_no_way_to_name_another_tool(self):
        import inspect
        self.assertEqual(list(inspect.signature(McpClient.create_draft).parameters), ["self", "arguments", "token"])

    def test_failures_map_to_the_typed_errors(self):
        client, _, server = make_mail()
        server.expire()
        with self.assertRaises(LoginExpired):
            client.create_draft(dict(DRAFT_ARGS), ApprovalToken())
        client, _, server = make_mail()
        server.down()
        with self.assertRaises(Unreachable):
            client.create_draft(dict(DRAFT_ARGS), ApprovalToken())
        client, _, server = make_mail()
        server.fail("EmailMessage.create", HttpStatus(500, "boom"))
        with self.assertRaises(ToolFailed) as cm:
            client.create_draft(dict(DRAFT_ARGS), ApprovalToken())
        self.assertEqual(cm.exception.reason, "HTTP 500")

    def test_a_tokens_state_is_visible_and_starts_fresh(self):
        token = ApprovalToken()
        self.assertFalse(token.used)
        client, _, _ = make_mail()
        client.create_draft(dict(DRAFT_ARGS), token)
        self.assertTrue(token.used)


class PartResultAndMessages(unittest.TestCase):
    def test_part_result_complete_and_missing(self):
        ok = PartResult.complete([1, 2])
        self.assertTrue(ok.ok)
        self.assertEqual(ok.data, [1, 2])
        self.assertIsNone(ok.reason)
        bad = PartResult.missing("Keystone: login expired — log in again")
        self.assertFalse(bad.ok)
        self.assertIsNone(bad.data)
        self.assertEqual(bad.reason, "Keystone: login expired — log in again")

    def test_login_expired_message(self):
        self.assertEqual(failure_message("Keystone", LoginExpired(), "meetings"),
                         "Keystone: login expired — log in again")

    def test_unreachable_message(self):
        self.assertEqual(failure_message("Suryodaya", Unreachable("timed out")),
                         "Suryodaya: can't be reached right now — try again later")

    def test_other_failures_name_the_reason_and_the_part(self):
        self.assertEqual(failure_message("Keystone", ToolFailed("calendar down"), "meetings"),
                         "Keystone meetings: couldn't be read (reason: calendar down) — report it if it repeats")
        self.assertEqual(failure_message("Keystone", ToolFailed("me broke")),
                         "Keystone: couldn't be read (reason: me broke) — report it if it repeats")

    def test_a_non_mcp_exception_is_described_not_raised(self):
        text = failure_message("Keystone", ValueError("odd"), "tasks")
        self.assertIn("Keystone tasks: couldn't be read (reason: ", text)
        self.assertIn("odd", text)


if __name__ == "__main__":
    unittest.main()
