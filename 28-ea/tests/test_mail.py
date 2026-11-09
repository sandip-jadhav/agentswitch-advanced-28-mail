"""mail.py: the mailbox check (P-B-10) and the Mail part's reads and rules (P-B-24 to P-B-29, P-B-47).

Field names are the design's assumptions (plan task 15, the probe, deferred); `message_row` builds them.
"""
import unittest

from ea_briefing import mail
from ea_briefing.auth import User
from ea_briefing.mcp import McpClient
from tests.fakes import FakeTransport, HttpStatus, MAILBOX_ROW, ToolError, message_row, serve_mail

BASE = "https://fake.test"
USER = User("u-100", "e-100", "team28c@example.test", "Team")
NO_MAILBOX = "Suryodaya: no mailbox for this login — ask the course team to set one up"
ME = "team28c@example.test"


def read(mailboxes=(), accounts=(), setup=None):
    transport = FakeTransport()
    server = transport.server(BASE, "fake", "tok")
    server.rows("Mailbox.list", list(mailboxes))
    server.rows("EmailAccount.list", list(accounts))
    if setup:
        setup(server)
    result = mail.read(McpClient(BASE, "tok", transport, label="Suryodaya"), USER)
    result.transport = transport
    return result


def read_mail(rows, setup=None, filters=True, mailboxes=(MAILBOX_ROW,), user=USER):
    """Read a mailbox holding `rows` (inbox and drafts) and return the PartResult."""
    transport = FakeTransport()
    server = transport.server(BASE, "fake", "tok")
    server.rows("Mailbox.list", list(mailboxes))
    server.rows("EmailAccount.list", [])
    serve_mail(server, rows, filters=filters)
    if setup:
        setup(server)
    result = mail.read(McpClient(BASE, "tok", transport, label="Suryodaya"), user)
    result.transport = transport
    return result


def row(id, **kw):
    """An inbox row from a person asking something, received on 5 Nov 2026 at the given time."""
    kw.setdefault("subject", "Question " + id)
    kw.setdefault("sender", "person@example.test")
    kw.setdefault("received_at", "2026-11-05T09:00:00Z")
    return message_row(id, kw.pop("subject"), kw.pop("sender"), kw.pop("received_at"), **kw)


def ids(messages):
    return [m.id for m in messages]


class MailboxCheck(unittest.TestCase):
    def test_both_lists_empty_is_no_mailbox_and_missing(self):
        r = read()
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, NO_MAILBOX)

    def test_no_mailbox_reads_nothing_but_the_two_mailbox_lists(self):
        r = read()
        self.assertEqual(r.transport.tool_names(), {"Mailbox.list", "EmailAccount.list"})

    def test_a_mailbox_row_alone_counts_and_the_part_is_complete(self):
        for kw in ({"mailboxes": [MAILBOX_ROW]}, {"accounts": [MAILBOX_ROW]}):
            def setup(server):
                serve_mail(server, [])
            r = read(setup=setup, **kw)
            self.assertTrue(r.ok, kw)
            self.assertEqual((r.data.needs_answer, r.data.skipped, r.data.unopened, r.data.already_drafted),
                             ([], [], [], []))

    def test_the_label_is_the_clients_system_name(self):
        transport = FakeTransport()
        server = transport.server(BASE, "fake", "tok")
        server.rows("Mailbox.list", [])
        server.rows("EmailAccount.list", [])
        r = mail.read(McpClient(BASE, "tok", transport, label="Keystone"), USER)
        self.assertEqual(r.reason, "Keystone: no mailbox for this login — ask the course team to set one up")


class MailboxCheckFailures(unittest.TestCase):
    def test_a_failing_mailbox_list_is_a_missing_part_with_its_reason(self):
        r = read(setup=lambda s: s.fail("Mailbox.list", ToolError("mail service down")))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "Suryodaya mail: couldn't be read (reason: mail service down)"
                                   " — report it if it repeats")

    def test_a_failing_account_list_is_a_missing_part_not_no_mailbox(self):
        r = read(setup=lambda s: s.fail("EmailAccount.list", ToolError("accounts down")))
        self.assertFalse(r.ok)
        self.assertIn("accounts down", r.reason)
        self.assertNotIn("no mailbox", r.reason)

    def test_an_expired_login_gives_the_login_line(self):
        transport = FakeTransport()
        transport.server(BASE, "fake", "tok").expire()
        r = mail.read(McpClient(BASE, "tok", transport, label="Keystone"), USER)
        self.assertEqual(r.reason, "Keystone: login expired — log in again")


class TheReads(unittest.TestCase):
    """P-B-24: the inbox and drafts-folder reads; a failure of either makes Mail missing."""

    def test_inbox_is_asked_for_unread_non_drafts_and_the_drafts_folder_separately(self):
        r = read_mail([row("a")])
        lists = [c.arguments for c in r.transport.tool_calls("EmailMessage.list")]
        self.assertEqual(len(lists), 2)
        by_folder = {a["folder"]: a for a in lists}
        self.assertEqual({k: v for k, v in by_folder["inbox"].items() if k not in ("limit", "offset")},
                         {"folder": "inbox", "is_read": False, "is_draft": False})
        self.assertEqual({k: v for k, v in by_folder["drafts"].items() if k not in ("limit", "offset")},
                         {"folder": "drafts"})

    def test_every_page_of_the_inbox_is_read(self):
        rows = [row("m%03d" % i, received_at="2026-11-05T09:%02d:00Z" % (i % 60)) for i in range(250)]
        r = read_mail(rows)
        self.assertEqual(len(r.data.needs_answer), 250)

    def test_a_failing_inbox_read_makes_mail_missing_with_its_reason(self):
        r = read_mail([row("a")], setup=lambda s: s.fail_when("EmailMessage.list", lambda a: a["folder"] == "inbox",
                                                              ToolError("inbox down")))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "Suryodaya mail: couldn't be read (reason: inbox down) — report it if it repeats")

    def test_a_failing_drafts_read_makes_mail_missing_because_a_second_draft_could_be_proposed(self):
        r = read_mail([row("a")], setup=lambda s: s.fail_when("EmailMessage.list", lambda a: a["folder"] == "drafts",
                                                              ToolError("drafts down")))
        self.assertFalse(r.ok)
        self.assertIn("drafts down", r.reason)
        self.assertEqual(r.transport.tool_calls("EmailMessage.get"), [])  # nothing was opened or proposed

    def test_a_401_on_a_list_is_an_expired_missing_part(self):
        r = read_mail([row("a")], setup=lambda s: s.fail("EmailMessage.list", HttpStatus(401)))
        self.assertFalse(r.ok)
        self.assertTrue(r.expired)
        self.assertEqual(r.reason, "Suryodaya: login expired — log in again")


class DraftAlreadySaved(unittest.TestCase):
    """P-B-25: my draft whose in_reply_to is the message's message_id."""

    def saved(self, **kw):
        kw.setdefault("created_by", "u-100")
        kw.setdefault("in_reply_to", "<q@mail.test>")
        return message_row("d1", "Re: Question q", ME, "2026-11-05T10:00:00Z", folder="drafts", is_read=True,
                           is_draft=True, body="Hi, here is my answer.", **kw)

    def test_the_message_is_shown_with_the_drafts_text_and_gets_no_proposal(self):
        r = read_mail([row("q", message_id="<q@mail.test>"), self.saved()])
        self.assertEqual(r.data.needs_answer, [])
        (entry,) = r.data.already_drafted
        self.assertEqual((entry.message.id, entry.text), ("q", "Hi, here is my answer."))
        self.assertEqual(r.transport.tool_calls("EmailMessage.get"), [])

    def test_it_counts_even_when_the_message_is_now_answered(self):
        r = read_mail([row("q", message_id="<q@mail.test>", is_answered=True), self.saved()])
        self.assertEqual(ids([e.message for e in r.data.already_drafted]), ["q"])

    def test_a_draft_by_somebody_else_or_for_another_message_does_not_match(self):
        for kw in ({"created_by": "u-999"}, {"in_reply_to": "<other@mail.test>"}, {"in_reply_to": None}):
            r = read_mail([row("q", message_id="<q@mail.test>"), self.saved(**kw)])
            self.assertEqual(ids(r.data.needs_answer), ["q"], kw)
            self.assertEqual(r.data.already_drafted, [], kw)

    def test_the_match_is_made_on_the_client_even_if_the_server_ignores_filters(self):
        r = read_mail([row("q", message_id="<q@mail.test>"), self.saved()], filters=False)
        self.assertEqual(ids([e.message for e in r.data.already_drafted]), ["q"])
        self.assertEqual(r.data.needs_answer, [])

    def test_the_draft_itself_is_never_listed(self):
        r = read_mail([self.saved()], filters=False)
        self.assertEqual((r.data.needs_answer, r.data.skipped, r.data.already_drafted), ([], [], []))


class NotListed(unittest.TestCase):
    """P-B-26: read, draft, non-inbox and answered rows are dropped on the client, whatever the server did."""

    def test_the_four_kinds_are_dropped_even_when_the_server_ignores_every_filter(self):
        rows = [row("ok"), row("read", is_read=True), row("draft", is_draft=True),
                row("sent", folder="sent"), row("trash", folder="trash"), row("answered", is_answered=True)]
        r = read_mail(rows, filters=False)
        self.assertEqual(ids(r.data.needs_answer), ["ok"])
        self.assertEqual((r.data.skipped, r.data.unopened, r.data.already_drafted), ([], [], []))
        gets = [c.arguments["id"] for c in r.transport.tool_calls("EmailMessage.get")]
        self.assertEqual(gets, ["ok"])

    def test_flags_that_arrive_as_text_or_numbers_are_still_read_as_flags(self):
        rows = [row("a", is_read="true"), row("b", is_answered=1), row("c", is_draft="True"), row("d")]
        r = read_mail(rows, filters=False)
        self.assertEqual(ids(r.data.needs_answer), ["d"])

    def test_a_row_without_an_id_is_ignored(self):
        bad = row("x")
        del bad["id"]
        r = read_mail([bad, row("ok")], filters=False)
        self.assertEqual(ids(r.data.needs_answer), ["ok"])


class SkipRules(unittest.TestCase):
    """P-B-27: fixed rules in order; the first match gives the reason."""

    def skipped(self, **kw):
        r = read_mail([row("s", **kw)])
        self.assertEqual(r.data.needs_answer, [], kw)
        (entry,) = r.data.skipped
        self.assertEqual(entry.message.id, "s")
        return entry.reason

    def test_own_mail_by_the_mailbox_address_in_any_case(self):
        self.assertEqual(self.skipped(sender=ME), "sent by you")
        self.assertEqual(self.skipped(sender="Team28C@Example.Test"), "sent by you")

    def test_own_mail_by_the_users_email_when_the_mailbox_has_no_address(self):
        r = read_mail([row("s", sender=ME)], mailboxes=[{"id": "mb-1"}])
        self.assertEqual([e.reason for e in r.data.skipped], ["sent by you"])

    def test_mail_from_a_colleagues_mailbox_is_not_sent_by_you(self):
        """Review fix: Mailbox.list and EmailAccount.list may return other users' rows; their addresses are
        not the login's own, so their mail still needs an answer."""
        mine = {"id": "mb-1", "email": ME, "user_id": "u-100"}
        boss = {"id": "mb-9", "email": "boss@example.test", "user_id": "u-999"}
        account = {"id": "acct-9", "email_address": "peer@example.test", "user_id": "u-998"}

        def setup(server):
            server.rows("EmailAccount.list", [account])
        r = read_mail([row("b", sender="Boss <boss@example.test>"), row("p", sender="peer@example.test"),
                       row("m", sender=ME)], mailboxes=[mine, boss], setup=setup)
        self.assertEqual(ids(r.data.needs_answer), ["b", "p"])
        self.assertEqual([(e.message.id, e.reason) for e in r.data.skipped], [("m", "sent by you")])

    def test_no_reply_senders(self):
        for sender in ("noreply@x.test", "no-reply@x.test", "NoReply@x.test", "donotreply@x.test",
                       "do-not-reply@x.test", "mailer-daemon@x.test",
                       "notifications@x.test", "notification@x.test", "Alerts <no-reply@x.test>"):
            self.assertEqual(self.skipped(sender=sender), "no-reply sender", sender)

    def test_a_person_whose_address_merely_resembles_one_is_not_skipped(self):
        for sender in ("reply@x.test", "norah.eply@x.test", "notify@x.test", "ann@x.test"):
            r = read_mail([row("p", sender=sender)])
            self.assertEqual(ids(r.data.needs_answer), ["p"], sender)

    def test_newsletters_by_the_unsubscribe_header_or_the_news_category(self):
        self.assertEqual(self.skipped(list_unsubscribe="<mailto:u@x.test>"), "newsletter")
        self.assertEqual(self.skipped(split_category="news"), "newsletter")
        r = read_mail([row("p", list_unsubscribe="", split_category="primary")])
        self.assertEqual(ids(r.data.needs_answer), ["p"])

    def test_automated_mail_by_auto_submitted_or_precedence(self):
        for headers in ("Auto-Submitted: auto-generated", "auto-submitted: auto-replied",
                        "X-A: 1\r\nAuto-Submitted: yes\r\nX-B: 2", "Precedence: bulk", "precedence:  List",
                        "Precedence: junk"):
            self.assertEqual(self.skipped(raw_headers=headers), "automated notification", headers)

    def test_auto_submitted_no_and_other_precedence_values_are_ordinary_mail(self):
        for headers in ("Auto-Submitted: no", "auto-submitted: NO", "Precedence: first-class", "", None,
                        "X-Note: Auto-Submitted: yes in the middle of a line"):
            r = read_mail([row("p", raw_headers=headers)])
            self.assertEqual(ids(r.data.needs_answer), ["p"], headers)

    def test_raw_headers_may_be_a_mapping(self):
        self.assertEqual(self.skipped(raw_headers={"Auto-Submitted": "auto-generated"}), "automated notification")
        r = read_mail([row("p", raw_headers={"Auto-Submitted": "no"})])
        self.assertEqual(ids(r.data.needs_answer), ["p"])

    def test_the_first_matching_rule_in_order_gives_the_reason(self):
        every = dict(list_unsubscribe="<x>", raw_headers="Precedence: bulk")
        self.assertEqual(self.skipped(sender=ME, **every), "sent by you")
        self.assertEqual(self.skipped(sender="noreply@x.test", **every), "no-reply sender")
        self.assertEqual(self.skipped(**every), "newsletter")

    def test_skipped_mail_is_not_opened(self):
        r = read_mail([row("a", sender="noreply@x.test"), row("b", list_unsubscribe="<x>")])
        self.assertEqual(r.transport.tool_calls("EmailMessage.get"), [])
        self.assertEqual(len(r.data.skipped), 2)


class OpeningAndOrder(unittest.TestCase):
    """P-B-28, P-B-29: one EmailMessage.get per candidate, oldest first."""

    def test_one_get_per_candidate_and_the_body_comes_from_it(self):
        rows = [row("a", body="Full body of a"), row("b", body="Full body of b")]
        r = read_mail(rows)
        gets = sorted(c.arguments["id"] for c in r.transport.tool_calls("EmailMessage.get"))
        self.assertEqual(gets, ["a", "b"])
        self.assertEqual({m.id: m.body for m in r.data.needs_answer}, {"a": "Full body of a", "b": "Full body of b"})

    def test_the_opened_row_wins_over_the_listed_row(self):
        def setup(server):
            full = dict(row("a", body="x"), body_text="The whole message, much longer than the snippet")
            server.handler("EmailMessage.get", lambda arguments: full)
        r = read_mail([row("a", body="short", snippet="short")], setup=setup)
        (m,) = r.data.needs_answer
        self.assertEqual(m.body, "The whole message, much longer than the snippet")
        self.assertEqual(m.snippet, "short")

    def test_the_message_carries_what_the_writer_and_renderer_need(self):
        r = read_mail([row("a", subject="Hello there", sender="Priya <priya@example.test>",
                           message_id="<a@m.test>", received_at="2026-11-05T09:00:00Z")])
        (m,) = r.data.needs_answer
        self.assertEqual((m.id, m.message_id, m.subject, m.sender, m.received_at),
                         ("a", "<a@m.test>", "Hello there", "Priya <priya@example.test>", "2026-11-05T09:00:00Z"))
        self.assertEqual(m.received.isoformat(), "2026-11-05T09:00:00+00:00")
        self.assertEqual(m.row["id"], "a")  # the full row, for the approval step's threading fields

    def test_needs_answer_is_oldest_received_first(self):
        rows = [row("late", received_at="2026-11-05T20:00:00Z"), row("early", received_at="2026-11-04T08:00:00Z"),
                row("mid", received_at="2026-11-05T09:00:00Z")]
        r = read_mail(rows)
        self.assertEqual(ids(r.data.needs_answer), ["early", "mid", "late"])

    def test_offsets_are_honoured_and_an_offsetless_time_is_utc(self):
        rows = [row("a", received_at="2026-11-05T10:00:00+05:30"),   # 04:30 UTC
                row("b", received_at="2026-11-05T05:00:00"),         # 05:00 UTC
                row("c", received_at="2026-11-05T04:00:00Z")]       # 04:00 UTC
        r = read_mail(rows)
        self.assertEqual(ids(r.data.needs_answer), ["c", "a", "b"])

    def test_an_unreadable_time_sorts_last_without_failing(self):
        rows = [row("bad", received_at="yesterday"), row("none", received_at=None),
                row("ok", received_at="2026-11-05T09:00:00Z")]
        r = read_mail(rows)
        self.assertEqual(ids(r.data.needs_answer)[0], "ok")
        self.assertEqual(sorted(ids(r.data.needs_answer)), ["bad", "none", "ok"])

    def test_skipped_mail_is_listed_oldest_first_too(self):
        rows = [row("n2", list_unsubscribe="x", received_at="2026-11-05T20:00:00Z"),
                row("n1", list_unsubscribe="x", received_at="2026-11-05T01:00:00Z")]
        r = read_mail(rows)
        self.assertEqual([e.message.id for e in r.data.skipped], ["n1", "n2"])

    def test_a_message_that_cannot_be_opened_is_listed_and_the_rest_continue(self):
        r = read_mail([row("a"), row("b")],
                      setup=lambda s: s.fail_when("EmailMessage.get", lambda a: a["id"] == "a", ToolError("boom")))
        self.assertTrue(r.ok)
        self.assertEqual(ids(r.data.needs_answer), ["b"])
        self.assertEqual(ids(r.data.unopened), ["a"])

    def test_a_get_that_cannot_connect_or_answers_http_500_also_lists_the_message_as_unopened(self):
        for exc in (HttpStatus(500), ConnectionRefusedError("refused")):
            r = read_mail([row("a")], setup=lambda s, e=exc: s.fail("EmailMessage.get", e))
            self.assertTrue(r.ok, exc)
            self.assertEqual((ids(r.data.needs_answer), ids(r.data.unopened)), ([], ["a"]), exc)

    def test_a_401_on_a_get_fails_the_part_as_an_expired_login(self):
        r = read_mail([row("a")], setup=lambda s: s.fail("EmailMessage.get", HttpStatus(401)))
        self.assertFalse(r.ok)
        self.assertTrue(r.expired)

    def test_a_get_reply_without_an_id_or_body_falls_back_on_the_listed_row(self):
        r = read_mail([row("a", body="Listed body")], setup=lambda s: s.handler("EmailMessage.get", lambda a: {}))
        (m,) = r.data.needs_answer
        self.assertEqual(m.body, "Listed body")

    def test_a_get_reply_that_is_not_a_row_is_unopened(self):
        r = read_mail([row("a")], setup=lambda s: s.handler("EmailMessage.get", lambda a: "nope"))
        self.assertEqual(ids(r.data.unopened), ["a"])

    def test_the_row_may_come_wrapped_in_data(self):
        r = read_mail([row("a", body="Wrapped body")],
                      setup=lambda s: s.handler("EmailMessage.get", lambda a: {"data": row("a", body="Wrapped body")}))
        self.assertEqual([m.body for m in r.data.needs_answer], ["Wrapped body"])


class MessagesAreOpenedConcurrently(unittest.TestCase):
    """Review fix: EmailMessage.get ran one blocking round-trip per candidate, eating the draft deadline."""

    def test_several_gets_are_in_flight_at_once_and_the_order_is_kept(self):
        import threading
        import time
        lock, state = threading.Lock(), {"now": 0, "peak": 0}
        rows = [row("m%02d" % i, received_at="2026-11-05T%02d:00:00Z" % i) for i in range(12)]

        def setup(server):
            def get(arguments):
                with lock:
                    state["now"] += 1
                    state["peak"] = max(state["peak"], state["now"])
                deadline = time.monotonic() + 1.0
                while time.monotonic() < deadline:   # wait (bounded) for company in flight
                    with lock:
                        if state["peak"] >= 3:
                            break
                    time.sleep(0.005)
                with lock:
                    state["now"] -= 1
                return next(r for r in server.mail_rows if r["id"] == arguments["id"])
            server.handler("EmailMessage.get", get)
        started = time.monotonic()
        r = read_mail(rows, setup=setup)
        self.assertGreaterEqual(state["peak"], 3)
        self.assertLess(time.monotonic() - started, 5.0)
        self.assertEqual(ids(r.data.needs_answer), ["m%02d" % i for i in range(12)])

    def test_a_401_on_one_of_many_gets_still_fails_the_part_as_expired(self):
        rows = [row("m%02d" % i) for i in range(6)]
        r = read_mail(rows, setup=lambda s: s.fail_when("EmailMessage.get", lambda a: a["id"] == "m03",
                                                       HttpStatus(401)))
        self.assertFalse(r.ok)
        self.assertTrue(r.expired)


class EachRowIsBuiltOnce(unittest.TestCase):
    def test_a_listed_message_that_is_not_opened_is_built_once(self):
        from unittest import mock
        rows = [row("a", is_read=True), row("b", is_read=True), row("c", is_read=True)]
        with mock.patch.object(mail, "_message", wraps=mail._message) as build:
            read_mail(rows, filters=False)
        self.assertEqual(build.call_count, 3)


class EmptyAndOnlyRead(unittest.TestCase):
    """P-B-47 and 'reads only'."""

    def test_nothing_unread_is_a_complete_empty_part(self):
        r = read_mail([])
        self.assertTrue(r.ok)
        self.assertEqual((r.data.needs_answer, r.data.skipped, r.data.unopened, r.data.already_drafted),
                         ([], [], [], []))

    def test_everything_skipped_is_complete_with_the_skipped_list(self):
        r = read_mail([row("a", sender="noreply@x.test"), row("b", list_unsubscribe="x")])
        self.assertTrue(r.ok)
        self.assertEqual(r.data.needs_answer, [])
        self.assertEqual(len(r.data.skipped), 2)

    def test_only_read_tools_are_used_and_no_model_is_involved(self):
        r = read_mail([row("a"), row("b", sender="noreply@x.test")])
        self.assertLessEqual(r.transport.tool_names(),
                             {"Mailbox.list", "EmailAccount.list", "EmailMessage.list", "EmailMessage.get"})
        self.assertEqual([c for c in r.transport.calls if "chat/completions" in c.url], [])

    def test_the_drafts_dict_starts_empty_for_the_runner_to_fill(self):
        self.assertEqual(read_mail([row("a")]).data.drafts, {})


class MailPartKeepsTheMailbox(unittest.TestCase):
    """Task 26 (P-B-40): the approval step needs the mailbox's id and address, so the part keeps them."""

    def test_the_part_holds_the_id_and_the_address_of_the_mailbox_row(self):
        r = read_mail([], mailboxes=[{"id": "mb-7", "address": "me@box.test"}])
        self.assertEqual((r.data.mailbox.id, r.data.mailbox.address), ("mb-7", "me@box.test"))

    def test_the_live_rows_address_field_is_email(self):
        """The slice 3 probe: Keystone's Mailbox row has `email` (not the login's email) and `user_id`."""
        live = {"id": "9e4a85d0", "email": "team28c@keystoneprecision.com", "user_id": "u-100",
                "company_id": "c1", "display_name": "Team 28c - Executive Assistant"}
        r = read_mail([], mailboxes=[live])
        self.assertEqual((r.data.mailbox.id, r.data.mailbox.address), ("9e4a85d0", "team28c@keystoneprecision.com"))

    def test_with_several_mailboxes_the_logins_own_comes_first_then_one_without_an_owner(self):
        other = {"id": "mb-other", "email": "other@box.test", "user_id": "u-999"}
        second = {"id": "mb-second", "email": "second@box.test", "user_id": "u-998",
                  "is_default": True}    # not a field the platform has: ignored
        mine = {"id": "mb-mine", "email": "mine@box.test", "user_id": "u-100"}
        self.assertEqual(read_mail([], mailboxes=[other, second, mine]).data.mailbox.id, "mb-mine")
        self.assertEqual(read_mail([], mailboxes=[other, {"id": "mb-x", "email": "x@box.test"}]).data.mailbox.id,
                         "mb-x")

    def test_a_colleagues_mailbox_is_never_the_one_drafts_are_saved_into(self):
        """Review fix: Mailbox.list may return other users' rows; a row owned by another user is never used."""
        other = {"id": "mb-other", "email": "other@box.test", "user_id": "u-999"}
        second = {"id": "mb-second", "email": "second@box.test", "user_id": "u-998"}
        box = read_mail([], mailboxes=[other, second]).data.mailbox
        self.assertNotIn(box.id, ("mb-other", "mb-second"))
        self.assertNotIn(box.address, ("other@box.test", "second@box.test"))

    def test_with_only_colleagues_mailboxes_the_logins_own_email_account_gives_the_address(self):
        transport = FakeTransport()
        server = transport.server(BASE, "fake", "tok")
        server.rows("Mailbox.list", [{"id": "mb-other", "email": "other@box.test", "user_id": "u-999"}])
        server.rows("EmailAccount.list", [{"id": "acct-9", "email_address": "boss@box.test", "user_id": "u-999"},
                                          {"id": "acct-1", "email_address": "acct@box.test", "user_id": "u-100"}])
        serve_mail(server, [])
        r = mail.read(McpClient(BASE, "tok", transport, label="Suryodaya"), USER)
        self.assertEqual((r.data.mailbox.id, r.data.mailbox.address), (None, "acct@box.test"))

    def test_an_email_account_row_gives_an_address_but_no_mailbox_id(self):
        """An EmailAccount id is not a mailbox id, so it is never offered as `mailbox_id`."""
        transport = FakeTransport()
        server = transport.server(BASE, "fake", "tok")
        server.rows("Mailbox.list", [])
        server.rows("EmailAccount.list", [{"id": "acct-1", "email_address": "acct@box.test"}])
        serve_mail(server, [])
        r = mail.read(McpClient(BASE, "tok", transport, label="Suryodaya"), USER)
        self.assertTrue(r.ok)
        self.assertEqual((r.data.mailbox.id, r.data.mailbox.address), (None, "acct@box.test"))

    def test_a_row_without_an_address_keeps_the_id_and_a_missing_address(self):
        r = read_mail([], mailboxes=[{"id": "mb-7"}])
        self.assertEqual((r.data.mailbox.id, r.data.mailbox.address), ("mb-7", None))

    def test_the_mailbox_is_kept_whatever_the_inbox_holds(self):
        r = read_mail([row("m1")])
        self.assertEqual(ids(r.data.needs_answer), ["m1"])
        self.assertEqual(r.data.mailbox.id, "mb-1")
        self.assertEqual(r.data.mailbox.address, "team28c@example.test")


class FirstLine(unittest.TestCase):
    """The '(no summary — first line shown)' text: the first sentence of snippet or body, 25 words at most."""

    def message(self, snippet=None, body="Body text."):
        r = read_mail([row("a", snippet=snippet, body=body)])
        return r.data.needs_answer[0]

    def test_it_prefers_the_snippet_and_takes_one_sentence(self):
        m = self.message(snippet="Where is the report? I cannot find it.", body="Something else entirely.")
        self.assertEqual(m.first_line(), "Where is the report?")

    def test_it_falls_back_on_the_body_and_collapses_whitespace(self):
        m = self.message(snippet="", body="Hello,\n\n  could you   help me. Thanks")
        self.assertEqual(m.first_line(), "Hello, could you help me.")

    def test_it_is_cut_to_25_words(self):
        m = self.message(snippet=" ".join("w%d" % i for i in range(40)))
        words = m.first_line().rstrip("…").split()
        self.assertEqual(len(words), 25)
        self.assertTrue(m.first_line().endswith("…"))

    def test_an_empty_message_has_an_empty_first_line(self):
        self.assertEqual(self.message(snippet="", body="").first_line(), "")


if __name__ == "__main__":
    unittest.main()
