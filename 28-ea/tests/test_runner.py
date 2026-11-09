"""runner.py: whoami per system, one thread per part, identity failure fanned out, run timer, progress
lines (P-B-2 to P-B-4, P-B-9)."""
import threading
import unittest
from unittest import mock

from ea_briefing import auth, meetings, runner
from ea_briefing.config import SystemSettings
from ea_briefing.draft_writer import Draft, DraftFailed
from ea_briefing.mcp import ToolNotAllowed
from tests import fakes
from tests.fakes import (KEYSTONE_BASE, SURYODAYA_BASE, TOKEN_KEYSTONE, TOKEN_SURYODAYA, FakeClock,
                         FakeDraftWriter, HttpStatus, ToolError, message_row)
from zoneinfo import ZoneInfo

SURYODAYA = SystemSettings("suryodaya", "Suryodaya", SURYODAYA_BASE, ZoneInfo("Asia/Kolkata"), ZoneInfo("Asia/Kolkata"))
KEYSTONE = SystemSettings("keystone", "Keystone", KEYSTONE_BASE, ZoneInfo("America/New_York"), ZoneInfo("America/New_York"))
TOKENS = {"suryodaya": TOKEN_SURYODAYA, "keystone": TOKEN_KEYSTONE}
EXPIRED = "Keystone: login expired — log in again"
UNREACHABLE = "Keystone: can't be reached right now — try again later"


class Run(unittest.TestCase):
    def go(self, transport, systems=(SURYODAYA, KEYSTONE), tokens=None, clock=None, progress=None,
           draft_writer=None, signatures=None):
        self.progress_lines = [] if progress is None else progress
        return runner.run(list(systems), TOKENS if tokens is None else tokens, clock or FakeClock(), transport,
                          self.progress_lines.append, draft_writer, signatures)


class Results(Run):
    def test_one_result_per_system_in_the_given_order_each_with_three_parts(self):
        transport, _, _ = fakes.default_world()
        out = self.go(transport, systems=(KEYSTONE, SURYODAYA))
        self.assertEqual([r.name for r in out.results], ["keystone", "suryodaya"])
        self.assertEqual([r.label for r in out.results], ["Keystone", "Suryodaya"])
        for r in out.results:
            self.assertFalse(r.mail.ok)  # no mailbox in the default world
            self.assertTrue(r.meetings.ok)
            self.assertTrue(r.tasks.ok)

    def test_each_system_is_read_at_its_own_url_with_its_own_token(self):
        transport, _, _ = fakes.default_world()
        self.go(transport)
        for base, token in ((SURYODAYA_BASE, TOKEN_SURYODAYA), (KEYSTONE_BASE, TOKEN_KEYSTONE)):
            calls = transport.calls_to(base)
            self.assertTrue(calls)
            self.assertEqual({c.headers["Authorization"] for c in calls}, {"Bearer " + token})

    def test_whoami_is_read_once_per_system_and_before_its_tools(self):
        transport, _, _ = fakes.default_world()
        self.go(transport)
        for base in (SURYODAYA_BASE, KEYSTONE_BASE):
            calls = transport.calls_to(base)
            me = [i for i, c in enumerate(calls) if c.path == "/api/auth/me"]
            self.assertEqual(me, [0])

    def test_the_identity_feeds_the_readers(self):
        """Tasks are the user's by the id from /api/auth/me."""
        transport, surya, _ = fakes.default_world()
        surya.rows("Task.list", [fakes.task_row("t1", "Mine", "2026-11-01", created_by="u-100")])
        out = self.go(transport)
        self.assertEqual([t.id for t in out.results[0].tasks.data.own], ["t1"])

    def test_each_result_carries_its_systems_display_zone_for_the_mail_times(self):
        transport, _, _ = fakes.default_world()
        out = self.go(transport)
        self.assertEqual([str(r.zone) for r in out.results], ["Asia/Kolkata", "America/New_York"])

    def test_one_system_only(self):
        transport, _, _ = fakes.default_world()
        out = self.go(transport, systems=(KEYSTONE,))
        self.assertEqual([r.name for r in out.results], ["keystone"])
        self.assertEqual(transport.calls_to(SURYODAYA_BASE), [])


class Threads(Run):
    def test_every_part_of_every_system_is_read_in_its_own_thread(self):
        """Six parties wait for each other: a sequential run would time out at the barrier."""
        barrier = threading.Barrier(6, timeout=5)
        seen = set()

        def waiting(server, tool, payload):
            def handler(arguments):
                key = (id(server), tool)
                if key not in seen:
                    seen.add(key)
                    barrier.wait()
                return payload
            server.handler(tool, handler)

        transport, surya, keystone = fakes.default_world()
        page = {"data": [], "total": 0, "has_more": False}
        for server in (surya, keystone):
            for tool in ("Mailbox.list", "CalendarEvent.list", "Task.list"):
                waiting(server, tool, page)
        out = self.go(transport)
        self.assertEqual(barrier.n_waiting, 0)
        for r in out.results:
            self.assertTrue(r.meetings.ok and r.tasks.ok, (r.meetings.reason, r.tasks.reason))

    def test_results_keep_system_order_whichever_finishes_first(self):
        transport, surya, _ = fakes.default_world()
        gate = threading.Event()
        keystone_done = threading.Event()
        real = surya._handlers["CalendarEvent.list"]

        def slow(arguments):
            gate.wait(timeout=5)
            return real(arguments)
        surya.handler("CalendarEvent.list", slow)
        transport.servers[KEYSTONE_BASE].handler(
            "Task.list", lambda a: (keystone_done.set(), gate.set(), {"data": [], "total": 0, "has_more": False})[2])
        out = self.go(transport)
        self.assertTrue(keystone_done.is_set())
        self.assertEqual([r.name for r in out.results], ["suryodaya", "keystone"])


class IdentityFailure(Run):
    def assert_all_parts(self, result, message):
        for part in ("mail", "meetings", "tasks"):
            p = getattr(result, part)
            self.assertFalse(p.ok, part)
            self.assertEqual(p.reason, message, part)

    def test_a_401_from_me_fails_all_three_parts_and_the_other_system_is_complete(self):
        transport, _, keystone = fakes.default_world()
        keystone.me = HttpStatus(401)
        out = self.go(transport)
        self.assert_all_parts(out.results[1], EXPIRED)
        self.assertTrue(out.results[0].meetings.ok and out.results[0].tasks.ok)
        self.assertEqual(transport.tool_calls(base=KEYSTONE_BASE), [])

    def test_a_missing_token_fails_all_three_parts_without_any_call(self):
        transport, _, _ = fakes.default_world()
        out = self.go(transport, tokens={"suryodaya": TOKEN_SURYODAYA, "keystone": None})
        self.assert_all_parts(out.results[1], EXPIRED)
        self.assertEqual(transport.calls_to(KEYSTONE_BASE), [])

    def test_a_system_missing_from_the_token_table_counts_as_no_token(self):
        transport, _, _ = fakes.default_world()
        out = self.go(transport, tokens={"suryodaya": TOKEN_SURYODAYA})
        self.assert_all_parts(out.results[1], EXPIRED)

    def test_me_that_cannot_connect_says_unreachable(self):
        transport, _, keystone = fakes.default_world()
        keystone.me = ConnectionRefusedError("refused")
        out = self.go(transport)
        self.assert_all_parts(out.results[1], UNREACHABLE)
        self.assertEqual(transport.tool_calls(base=KEYSTONE_BASE), [])

    def test_me_that_times_out_says_unreachable(self):
        transport, _, keystone = fakes.default_world()
        keystone.me = TimeoutError("timed out")
        self.assert_all_parts(self.go(transport).results[1], UNREACHABLE)

    def test_me_failing_another_way_gives_the_reason_line(self):
        transport, _, keystone = fakes.default_world()
        keystone.me = HttpStatus(500)
        self.assert_all_parts(self.go(transport).results[1],
                              "Keystone: couldn't be read (reason: HTTP 500) — report it if it repeats")

    def test_me_failing_with_a_protocol_error_says_unreachable_and_the_other_system_prints(self):
        import http.client
        transport, _, keystone = fakes.default_world()
        keystone.me = http.client.BadStatusLine("garbage")
        out = self.go(transport)
        self.assert_all_parts(out.results[1], UNREACHABLE)
        self.assertTrue(out.results[0].meetings.ok and out.results[0].tasks.ok)

    def test_an_unexpected_error_around_me_gives_all_three_parts_and_spares_the_other_system(self):
        transport, _, _ = fakes.default_world()
        real = auth.whoami

        def flaky(base_url, token, transport=None, **kw):
            if base_url == KEYSTONE_BASE:
                raise ValueError("odd")
            return real(base_url, token, transport, **kw)
        with mock.patch.object(auth, "whoami", side_effect=flaky):
            out = self.go(transport)
        self.assert_all_parts(out.results[1],
                              "Keystone: couldn't be read (reason: unexpected error: ValueError) — report it if it repeats")
        self.assertTrue(out.results[0].meetings.ok and out.results[0].tasks.ok)

    def test_a_forbidden_tool_error_around_me_is_still_passed_on(self):
        transport, _, _ = fakes.default_world()
        with mock.patch.object(auth, "whoami", side_effect=ToolNotAllowed("Task.update")):
            with self.assertRaises(ToolNotAllowed):
                self.go(transport)

    def test_both_systems_down_gives_every_part_missing(self):
        transport, surya, keystone = fakes.default_world()
        surya.down()
        keystone.down()
        out = self.go(transport)
        self.assertEqual(sum(getattr(r, p).ok for r in out.results for p in ("mail", "meetings", "tasks")), 0)


class PartFailure(Run):
    def test_only_calendar_failing_leaves_mail_and_tasks_of_that_system(self):
        transport, _, keystone = fakes.default_world(mailbox=True)
        keystone.fail("CalendarEvent.list", ToolError("calendar down"))
        ks = self.go(transport).results[1]
        self.assertFalse(ks.meetings.ok)
        self.assertEqual(ks.meetings.reason,
                         "Keystone meetings: couldn't be read (reason: calendar down) — report it if it repeats")
        self.assertTrue(ks.tasks.ok)
        self.assertTrue(ks.mail.ok)  # a mailbox with no unread mail: the part is complete (slice 2)

    def test_a_401_from_one_tool_fails_all_three_parts_of_that_system(self):
        """P-B-2 / G1-AC2: a 401 on any tool is the login line on every part of that system."""
        for tool in ("Mailbox.list", "CalendarEvent.list", "Task.list"):
            transport, surya, keystone = fakes.default_world(mailbox=True)
            keystone.fail(tool, HttpStatus(401))
            out = self.go(transport)
            for part in ("mail", "meetings", "tasks"):
                p = getattr(out.results[1], part)
                self.assertFalse(p.ok, (tool, part))
                self.assertEqual(p.reason, EXPIRED, (tool, part))
            self.assertTrue(out.results[0].meetings.ok and out.results[0].tasks.ok, tool)

    def test_a_401_from_the_others_read_fails_all_three_parts_too(self):
        transport, _, keystone = fakes.default_world()
        keystone.fail_when("Task.list", lambda a: not any(k in a for k in ("assignee_id", "created_by")),
                           HttpStatus(401))
        ks = self.go(transport).results[1]
        for part in ("mail", "meetings", "tasks"):
            self.assertEqual(getattr(ks, part).reason, EXPIRED, part)

    def test_a_reader_that_blows_up_becomes_a_missing_part_with_a_reason(self):
        transport, _, _ = fakes.default_world()
        with mock.patch.object(meetings, "read", side_effect=KeyError("boom")):
            out = self.go(transport)
        for r in out.results:
            self.assertFalse(r.meetings.ok)
            self.assertIn("unexpected error: KeyError", r.meetings.reason)
            self.assertIn("report it if it repeats", r.meetings.reason)
            self.assertTrue(r.tasks.ok)

    def test_a_forbidden_tool_is_never_turned_into_a_missing_part(self):
        transport, _, _ = fakes.default_world()
        with mock.patch.object(meetings, "read", side_effect=ToolNotAllowed("Task.update")):
            with self.assertRaises(ToolNotAllowed):
                self.go(transport)


class Timer(Run):
    def test_elapsed_is_the_clock_difference_over_the_whole_run(self):
        transport, surya, _ = fakes.default_world()
        clock = FakeClock()
        clock.advance(100)  # the run starts at 100 s on the monotonic clock

        def slow(arguments):
            clock.advance(7)
            return {"data": [], "total": 0, "has_more": False}
        surya.handler("Task.list", slow)
        out = self.go(transport, clock=clock)
        self.assertGreaterEqual(out.elapsed, 7.0)
        self.assertEqual(out.elapsed, clock.monotonic() - 100)

    def test_a_fixed_clock_gives_zero(self):
        transport, _, _ = fakes.default_world()
        self.assertEqual(self.go(transport).elapsed, 0.0)


class Progress(Run):
    def test_one_reading_line_per_system_in_order_before_any_platform_call(self):
        transport, _, _ = fakes.default_world()
        lines = []
        calls_when_announced = []
        out = runner.run([SURYODAYA, KEYSTONE], TOKENS, FakeClock(), transport,
                         lambda line: (lines.append(line), calls_when_announced.append(len(transport.calls))))
        self.assertEqual(lines, ["Reading Suryodaya…", "Reading Keystone…"])
        self.assertEqual(calls_when_announced, [0, 0])
        self.assertTrue(out.results)

    def test_without_a_progress_callable_nothing_is_printed(self):
        import contextlib
        import io
        transport, _, _ = fakes.default_world()
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            runner.run([SURYODAYA], TOKENS, FakeClock(), transport)
        self.assertEqual((out.getvalue(), err.getvalue()), ("", ""))


def mail(system, *specs):
    """{system: [rows]} for default_world: specs are (id, received_at)."""
    return {system: [message_row(i, "Subject " + i, "someone@example.test", at) for i, at in specs]}


class OnMail(Run):
    """`_read_system` hands a complete Mail part's messages to `on_mail` (the drafting queue's way in)."""

    def read(self, transport, on_mail):
        return runner._read_system(SURYODAYA, TOKEN_SURYODAYA, FakeClock(), transport, [], on_mail)

    def test_on_mail_gets_the_system_the_identity_and_the_messages_needing_an_answer(self):
        transport, _, _ = fakes.default_world(mailbox=True, messages=mail(
            "suryodaya", ("b", "2026-11-05T06:00:00Z"), ("a", "2026-11-05T05:00:00Z")))
        calls = []
        result = self.read(transport, lambda system, user, messages: calls.append((system, user, messages)))
        self.assertEqual(len(calls), 1)
        system, user, messages = calls[0]
        self.assertIs(system, SURYODAYA)
        self.assertEqual(user.id, "u-100")
        self.assertEqual([m.id for m in messages], ["a", "b"])
        self.assertIs(messages, result.mail.data.needs_answer)

    def test_on_mail_is_not_called_when_the_mail_part_is_missing(self):
        transport, _, _ = fakes.default_world()  # no mailbox
        calls = []
        self.read(transport, lambda *args: calls.append(args))
        self.assertEqual(calls, [])

    def test_on_mail_is_not_called_when_the_identity_fails(self):
        transport, surya, _ = fakes.default_world(mailbox=True)
        surya.me = HttpStatus(401)
        calls = []
        self.read(transport, lambda *args: calls.append(args))
        self.assertEqual(calls, [])

    def test_a_failing_on_mail_never_loses_the_other_parts(self):
        transport, _, _ = fakes.default_world(mailbox=True)

        def boom(*args):
            raise RuntimeError("queue broke")
        result = self.read(transport, boom)
        self.assertTrue(result.meetings.ok and result.tasks.ok)


    def test_messages_whose_on_mail_failed_read_model_unavailable_with_the_keyword_mark(self):
        transport, _, _ = fakes.default_world(mailbox=True, messages={"suryodaya": [
            message_row("k", "Please confirm Friday's meeting", "x@example.test", "2026-11-05T05:00:00Z"),
            message_row("p", "Where is the report", "y@example.test", "2026-11-05T06:00:00Z")]})

        def boom(*args):
            raise RuntimeError("queue broke")
        drafts = self.read(transport, boom).mail.data.drafts
        self.assertEqual({i: (d.status, d.needs_decision) for i, d in drafts.items()},
                         {"k": ("model_unavailable", True), "p": ("model_unavailable", False)})

    def test_an_expired_login_calls_drop_mail_with_the_system(self):
        transport, surya, _ = fakes.default_world(mailbox=True, messages=mail("suryodaya", ("a", "2026-11-05T05:00:00Z")))
        surya.fail("Task.list", HttpStatus(401))
        dropped, added = [], []
        result = runner._read_system(SURYODAYA, TOKEN_SURYODAYA, FakeClock(), transport, [],
                                     lambda system, user, messages: added.append(system), dropped.append)
        self.assertEqual((dropped, added), ([SURYODAYA], [SURYODAYA]))
        self.assertTrue(result.mail.expired)

    def test_drop_mail_is_not_called_when_the_system_read_fine(self):
        transport, _, _ = fakes.default_world(mailbox=True, messages=mail("suryodaya", ("a", "2026-11-05T05:00:00Z")))
        dropped = []
        runner._read_system(SURYODAYA, TOKEN_SURYODAYA, FakeClock(), transport, [], lambda *a: None, dropped.append)
        self.assertEqual(dropped, [])


class Drafting(Run):
    """P-B-33 to P-B-35: one queue for both systems, 4 workers, the 45 s deadline, the guard, the counts."""

    def world(self, **per_system):
        transport, _, _ = fakes.default_world(mailbox=True, messages=per_system)
        return transport

    def two_systems(self):
        return self.world(
            suryodaya=mail("suryodaya", ("s3", "2026-11-05T09:00:00Z"), ("s1", "2026-11-05T05:00:00Z"),
                           ("s5", "2026-11-05T11:00:00Z"))["suryodaya"],
            keystone=mail("keystone", ("k2", "2026-11-05T07:00:00Z"), ("k4", "2026-11-05T10:00:00Z"),
                          ("k6", "2026-11-05T12:00:00Z"))["keystone"])

    def wait_for(self, writer, n):
        for _ in range(500):
            if len(writer.calls) >= n:
                return
            threading.Event().wait(0.01)
        self.fail("only %d writer calls started, wanted %d" % (len(writer.calls), n))

    def held_run(self, clock=None):
        """Start a run whose writer holds every call open; returns (writer, hold, thread, done)."""
        hold = threading.Event()
        writer = FakeDraftWriter(hold=hold)
        done = []
        thread = threading.Thread(target=lambda: done.append(
            self.go(self.two_systems(), clock=clock, draft_writer=writer)))
        thread.start()
        self.addCleanup(hold.set)
        return writer, hold, thread, done

    def test_every_message_gets_a_draft_attached_to_its_own_mail_part(self):
        out = self.go(self.two_systems(), draft_writer=FakeDraftWriter())
        for r, ids in zip(out.results, (["s1", "s3", "s5"], ["k2", "k4", "k6"])):
            self.assertEqual([m.id for m in r.mail.data.needs_answer], ids)
            self.assertEqual(sorted(r.mail.data.drafts), ids)
            for draft in r.mail.data.drafts.values():
                self.assertIsInstance(draft, Draft)

    def test_the_queue_takes_the_oldest_received_first_across_both_systems(self):
        """Both systems add before the single worker starts, so the order is the queue's, not a race's."""
        from ea_briefing import mail as mailmod
        from ea_briefing.auth import User
        writer = FakeDraftWriter()
        queue = runner._DraftQueue(writer, {}, FakeClock(), 45.0, None)
        user = User("u-1", None, "a@example.test", "A")

        def messages(*specs):
            return [mailmod._message(message_row(i, "S" + i, "x@example.test", at)) for i, at in specs]
        queue.add(SURYODAYA, user, messages(("s3", "2026-11-05T09:00:00Z"), ("s1", "2026-11-05T05:00:00Z")))
        queue.add(KEYSTONE, user, messages(("k2", "2026-11-05T07:00:00Z"), ("k4", "2026-11-05T10:00:00Z")))
        queue.close()
        queue.work()
        self.assertEqual([call[0] for call in writer.calls], ["s1", "k2", "s3", "k4"])

    def test_a_message_with_no_readable_time_is_drafted_last(self):
        from ea_briefing import mail as mailmod
        from ea_briefing.auth import User
        writer = FakeDraftWriter()
        queue = runner._DraftQueue(writer, {}, FakeClock(), 45.0, None)
        queue.add(SURYODAYA, User("u-1", None, "a@example.test", "A"), [
            mailmod._message(message_row("odd", "S", "x@example.test", "garbage")),
            mailmod._message(message_row("old", "S", "x@example.test", "2026-11-05T05:00:00Z"))])
        queue.close()
        queue.work()
        self.assertEqual([call[0] for call in writer.calls], ["old", "odd"])

    def test_the_first_four_calls_come_from_the_six_queued_messages_and_all_six_are_drafted(self):
        writer, hold, thread, done = self.held_run()
        self.wait_for(writer, 4)
        hold.set()
        thread.join(10)
        self.assertEqual({call[0] for call in writer.calls}, {"s1", "k2", "s3", "k4", "s5", "k6"})
        self.assertTrue(done)

    def test_at_most_four_calls_run_at_a_time(self):
        writer, hold, thread, done = self.held_run()
        self.wait_for(writer, 4)
        threading.Event().wait(0.1)  # a fifth worker would have started by now
        self.assertEqual(len(writer.calls), 4)
        hold.set()
        thread.join(10)
        self.assertEqual(writer.max_concurrent, 4)

    def test_at_45_seconds_the_messages_not_yet_taken_get_time_limit(self):
        clock = FakeClock()
        writer, hold, thread, done = self.held_run(clock)
        self.wait_for(writer, 4)
        taken = {call[0] for call in writer.calls}
        clock.advance(45)
        hold.set()
        thread.join(10)
        out = done[0]
        drafts = {**out.results[0].mail.data.drafts, **out.results[1].mail.data.drafts}
        for i in {"s1", "k2", "s3", "k4", "s5", "k6"} - taken:
            self.assertIsInstance(drafts[i], DraftFailed)
            self.assertEqual((drafts[i].reason, drafts[i].status), ("time limit", "time_limit"))
        self.assertEqual(len(taken), 4)
        self.assertEqual(len(writer.calls), 4)

    def test_every_write_is_given_the_deadline_start_plus_45_seconds(self):
        """The writer owns the rule for calls in flight (G2-AC7); the runner hands it the deadline."""
        clock = FakeClock()
        clock.advance(1000)
        writer = FakeDraftWriter()
        self.go(self.two_systems(), clock=clock, draft_writer=writer)
        self.assertEqual(len(writer.deadlines), 6)
        self.assertEqual(set(writer.deadlines), {1045.0})

    def test_a_draft_that_arrives_after_45_seconds_is_a_time_limit_with_the_real_writer(self):
        """G2-AC7: a result after the deadline is not ready by it, and the redraft is not started."""
        from ea_briefing.config import ModelSettings
        from ea_briefing.draft_writer import DraftWriter
        clock = FakeClock()
        transport = self.world(suryodaya=[
            message_row("a", "Please confirm Friday's meeting", "x@example.test", "2026-11-05T05:00:00Z"),
            message_row("b", "Where is the report", "y@example.test", "2026-11-05T06:00:00Z")])

        def responder(request):
            clock.advance(46)
            return fakes.model_reply(needs_decision=False)
        model = transport.add_server(fakes.MODEL_BASE, fakes.FakeModelServer(responder))
        writer = DraftWriter(ModelSettings(fakes.MODEL_URL, fakes.MODEL_NAME, None), transport, clock)
        out = self.go(transport, systems=(SURYODAYA,), clock=clock, draft_writer=writer)
        drafts = out.results[0].mail.data.drafts
        self.assertEqual({d.status for d in drafts.values()}, {"time_limit"})
        self.assertEqual((drafts["a"].needs_decision, drafts["b"].needs_decision), (True, False))
        self.assertLessEqual(len(model.requests), 2)  # one per message: neither redraft started

    def test_just_before_45_seconds_a_message_is_still_taken(self):
        clock = FakeClock()
        clock.advance(1000)  # the deadline is start + 45 s, not 45 s on the clock
        writer = FakeDraftWriter(clock=clock, seconds=44)
        out = self.go(self.world(suryodaya=mail("suryodaya", ("a", "2026-11-05T05:00:00Z"),
                                                ("b", "2026-11-05T06:00:00Z"))["suryodaya"]),
                      systems=(SURYODAYA,), clock=clock, draft_writer=writer)
        self.assertEqual(len(writer.calls), 2)  # both were taken at once, before the first call ended
        self.assertTrue(all(isinstance(d, Draft) for d in out.results[0].mail.data.drafts.values()))

    def test_drafting_starts_when_the_mail_part_is_read_not_after_every_part(self):
        """A slow Tasks read must not hold the drafting back."""
        transport = self.world(suryodaya=mail("suryodaya", ("a", "2026-11-05T05:00:00Z"))["suryodaya"])
        started = threading.Event()
        writer = FakeDraftWriter(outcome=lambda m, u, s: (started.set(), Draft("Hi,\n\nx"))[1])
        seen = []
        transport.servers[SURYODAYA_BASE].handler(
            "Task.list", lambda a: (seen.append(started.wait(5)), {"data": [], "total": 0})[1])
        self.go(transport, systems=(SURYODAYA,), draft_writer=writer)
        self.assertTrue(seen and all(seen))

    def test_each_system_passes_its_own_signature(self):
        writer = FakeDraftWriter()
        self.go(self.two_systems(), draft_writer=writer, signatures={"suryodaya": "Sig S", "keystone": None})
        by_id = dict(writer.calls)
        self.assertEqual({by_id[i] for i in ("s1", "s3", "s5")}, {"Sig S"})
        self.assertEqual({by_id[i] for i in ("k2", "k4", "k6")}, {None})

    def test_a_writer_that_raises_gives_model_unavailable_and_the_run_goes_on(self):
        writer = FakeDraftWriter(outcome={"s3": RuntimeError("boom")})
        out = self.go(self.two_systems(), draft_writer=writer)
        drafts = out.results[0].mail.data.drafts
        self.assertIsInstance(drafts["s3"], DraftFailed)
        self.assertEqual((drafts["s3"].reason, drafts["s3"].status), ("model unavailable", "model_unavailable"))
        self.assertIsInstance(drafts["s1"], Draft)
        self.assertIsInstance(out.results[1].mail.data.drafts["k2"], Draft)
        self.assertTrue(out.results[0].meetings.ok and out.results[0].tasks.ok)

    def keyword_world(self):
        """Suryodaya: s-kw1 .. s-kw3 need a decision (keyword), s-pl1 .. s-pl3 do not."""
        rows = []
        for n in (1, 2, 3):
            rows.append(message_row("s-kw%d" % n, "Please confirm the meeting %d" % n, "x@example.test",
                                    "2026-11-05T0%d:00:00Z" % n))
            rows.append(message_row("s-pl%d" % n, "Where is the report %d" % n, "y@example.test",
                                    "2026-11-05T0%d:30:00Z" % n))
        return self.world(suryodaya=rows)

    def assert_marks(self, out):
        drafts = out.results[0].mail.data.drafts
        self.assertEqual(len(drafts), 6)
        for id_, outcome in drafts.items():
            self.assertIsInstance(outcome, DraftFailed, id_)
            self.assertEqual(outcome.needs_decision, id_.startswith("s-kw"), id_)

    def test_a_message_that_hits_the_time_limit_keeps_the_keyword_mark(self):
        clock = FakeClock()
        hold = threading.Event()
        writer = FakeDraftWriter(hold=hold, outcome=lambda m, u, s: DraftFailed("time limit"))
        self.addCleanup(hold.set)
        done = []
        thread = threading.Thread(target=lambda: done.append(
            self.go(self.keyword_world(), systems=(SURYODAYA,), clock=clock, draft_writer=writer)))
        thread.start()
        self.wait_for(writer, 4)
        clock.advance(45)
        hold.set()
        thread.join(10)
        untaken = {m.id for m in done[0].results[0].mail.data.needs_answer} - {c[0] for c in writer.calls}
        self.assertEqual(len(untaken), 2)
        drafts = done[0].results[0].mail.data.drafts
        for id_ in untaken:
            self.assertEqual((drafts[id_].reason, drafts[id_].needs_decision), ("time limit", id_.startswith("s-kw")))

    def test_without_a_draft_writer_the_keyword_mark_still_shows(self):
        out = self.go(self.keyword_world(), systems=(SURYODAYA,))
        self.assert_marks(out)
        self.assertEqual({d.reason for d in out.results[0].mail.data.drafts.values()}, {"model unavailable"})

    def test_a_writer_that_raises_keeps_the_keyword_mark(self):
        out = self.go(self.keyword_world(), systems=(SURYODAYA,), draft_writer=FakeDraftWriter(outcome=RuntimeError("x")))
        self.assert_marks(out)
        self.assertEqual({d.reason for d in out.results[0].mail.data.drafts.values()}, {"model unavailable"})

    def test_a_failing_drafting_queue_leaves_every_message_model_unavailable_with_the_mark(self):
        with mock.patch.object(runner._DraftQueue, "add", side_effect=RuntimeError("queue broke")):
            out = self.go(self.keyword_world(), systems=(SURYODAYA,), draft_writer=FakeDraftWriter())
        self.assert_marks(out)
        self.assertEqual({d.status for d in out.results[0].mail.data.drafts.values()}, {"model_unavailable"})

    def test_an_expired_system_loses_its_queued_messages_and_they_are_not_counted(self):
        """B2: Keystone's 401 on Tasks replaces its Mail part; messages still queued are not drafted."""
        transport = self.world(keystone=[
            message_row("k%d" % n, "Subject %d" % n, "x@example.test", "2026-11-05T0%d:00:00Z" % n)
            for n in range(1, 7)])
        transport.servers[KEYSTONE_BASE].fail("Task.list", HttpStatus(401))
        hold = threading.Event()
        writer = FakeDraftWriter(hold=hold)
        self.addCleanup(hold.set)
        real_drop = runner._DraftQueue.drop

        def drop_then_release(queue, system):
            self.wait_for(writer, 4)   # the four workers hold the four oldest; two are still queued
            real_drop(queue, system)
            hold.set()
        with mock.patch.object(runner._DraftQueue, "drop", drop_then_release):
            out = self.go(transport, systems=(KEYSTONE,), draft_writer=writer)
        self.assertEqual(out.results[0].mail.reason, EXPIRED)
        self.assertEqual(len(writer.calls), 4)
        counts = [l for l in self.progress_lines if l.startswith("Drafting")]
        self.assertTrue(counts and all(l.endswith("of 4)…") for l in counts), counts)

    def test_the_queue_drops_only_the_items_of_the_system_asked_for(self):
        from ea_briefing import mail as mailmod
        from ea_briefing.auth import User
        writer = FakeDraftWriter()
        queue = runner._DraftQueue(writer, {}, FakeClock(), 45.0, None)
        user = User("u-1", None, "a@example.test", "A")

        def messages(*ids):
            return [mailmod._message(message_row(i, "S" + i, "x@example.test", "2026-11-05T05:00:00Z")) for i in ids]
        queue.add(SURYODAYA, user, messages("s1", "s2"))
        queue.add(KEYSTONE, user, messages("k1"))
        queue.drop(KEYSTONE)
        queue.close()
        queue.work()
        self.assertEqual(sorted(call[0] for call in writer.calls), ["s1", "s2"])

    def test_a_progress_callable_that_raises_never_stops_a_worker(self):
        def broken(line):
            raise OSError("stderr closed")
        out = runner.run([SURYODAYA], TOKENS, FakeClock(), self.keyword_world(), broken, FakeDraftWriter())
        drafts = out.results[0].mail.data.drafts
        self.assertEqual(len(drafts), 6)
        self.assertTrue(all(isinstance(d, Draft) for d in drafts.values()))

    def test_progress_is_called_without_the_queue_lock_held(self):
        from ea_briefing import mail as mailmod
        from ea_briefing.auth import User
        free = []

        def probe(line):
            if not line.startswith("Drafting"):
                return
            got = []

            def try_lock():
                got.append(queue._cond.acquire(timeout=2))
                if got[0]:
                    queue._cond.release()
            other = threading.Thread(target=try_lock)
            other.start()
            other.join()
            free.append(got[0])
        queue = runner._DraftQueue(FakeDraftWriter(), {}, FakeClock(), 45.0, probe)
        queue.add(SURYODAYA, User("u-1", None, "a@example.test", "A"),
                  [mailmod._message(message_row("a", "S", "x@example.test", "2026-11-05T05:00:00Z"))])
        queue.close()
        queue.work()
        self.assertEqual(free, [True])

    def test_the_run_result_carries_the_writers_counts(self):
        stats = {"calls": 6, "failures": 1, "unreachable": True}
        out = self.go(self.two_systems(), draft_writer=FakeDraftWriter(stats=stats))
        self.assertEqual(out.model, stats)

    def test_without_a_draft_writer_every_message_is_model_unavailable_and_the_model_is_none(self):
        out = self.go(self.two_systems())
        self.assertIsNone(out.model)
        for r in out.results:
            self.assertEqual(len(r.mail.data.drafts), 3)
            for draft in r.mail.data.drafts.values():
                self.assertEqual(draft.reason, "model unavailable")

    def test_a_writer_outcome_is_kept_as_given(self):
        failed = DraftFailed("invalid thing")
        out = self.go(self.two_systems(), draft_writer=FakeDraftWriter(outcome={"k4": failed}))
        self.assertIs(out.results[1].mail.data.drafts["k4"], failed)

    def test_a_system_with_no_mailbox_queues_nothing(self):
        transport, _, _ = fakes.default_world()
        writer = FakeDraftWriter()
        self.go(transport, draft_writer=writer)
        self.assertEqual(writer.calls, [])

    def test_a_401_from_another_tool_replaces_the_mail_part_and_spares_the_other_system(self):
        transport = self.two_systems()
        transport.servers[KEYSTONE_BASE].fail("Task.list", HttpStatus(401))
        out = self.go(transport, draft_writer=FakeDraftWriter())
        self.assertFalse(out.results[1].mail.ok)
        self.assertEqual(out.results[1].mail.reason, EXPIRED)
        self.assertEqual(len(out.results[0].mail.data.drafts), 3)

    def test_drafting_progress_goes_to_the_progress_callable_as_n_of_m(self):
        lines = []
        self.go(self.two_systems(), progress=lines, draft_writer=FakeDraftWriter())
        drafting = [l for l in lines if l.startswith("Drafting replies")]
        self.assertEqual(len(drafting), 6)
        for l in drafting:
            self.assertRegex(l, r"^Drafting replies \(\d of \d\)…$")
        self.assertIn("Drafting replies (6 of 6)…", drafting)
        self.assertEqual([l for l in lines if l.startswith("Reading")], ["Reading Suryodaya…", "Reading Keystone…"])

    def test_no_drafting_line_when_nothing_is_drafted(self):
        lines = []
        self.go(self.world(), progress=lines, draft_writer=FakeDraftWriter())
        self.assertEqual([l for l in lines if l.startswith("Drafting")], [])


if __name__ == "__main__":
    unittest.main()
