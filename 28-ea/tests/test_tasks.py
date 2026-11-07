"""tasks.py: own overdue tasks from three reads, the others count apart (P-B-15 to P-B-17)."""
import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from ea_briefing import tasks
from ea_briefing.auth import User
from ea_briefing.mcp import McpClient
from tests import fakes
from tests.fakes import FakeClock, FakeTransport, ToolError, task_row

BASE = "https://fake.test"
TOKEN = "tok"
IST, NY = ZoneInfo("Asia/Kolkata"), ZoneInfo("America/New_York")
USER = User("u-100", "e-100", "team28c@example.test", "Team")


def mine(id, title, due, **kw):
    kw.setdefault("created_by", "u-100")
    kw.setdefault("assignee_id", None)
    return task_row(id, title, due, **kw)


def read(rows, zone=IST, now=fakes.FIXED_NOW, filters=True, page_size=100, server=None, user=USER):
    transport = FakeTransport()
    srv = transport.server(BASE, "fake", TOKEN)
    srv.rows("Task.list", rows, filters=filters)
    if server:
        server(srv)
    client = McpClient(BASE, TOKEN, transport, page_size=page_size, label="Suryodaya")
    result = tasks.read(client, user, zone, FakeClock(now))
    result.transport = transport
    return result


def own_ids(result):
    return [t.id for t in result.data.own]


class OwnTasks(unittest.TestCase):
    def test_three_own_reads_each_filtered_to_overdue_then_one_read_for_the_others(self):
        r = read([])
        filters = [c.arguments for c in r.transport.tool_calls("Task.list")]
        core = [{k: v for k, v in f.items() if k not in ("limit", "offset")} for f in filters]
        self.assertEqual(core, [
            {"assignee_id": "u-100", "due_date": "lt:2026-11-06"},
            {"assignee_id": "e-100", "due_date": "lt:2026-11-06"},
            {"created_by": "u-100", "due_date": "lt:2026-11-06"},
            {"due_date": "lt:2026-11-06"},
        ])

    def test_no_employee_id_means_no_employee_read(self):
        user = User("u-100", None, "t@example.test", "T")
        r = read([], user=user)
        owners = [c.arguments.get("assignee_id") for c in r.transport.tool_calls("Task.list")]
        self.assertEqual(owners, ["u-100", None, None])
        self.assertEqual(len(r.transport.tool_calls("Task.list")), 3)

    def test_an_employee_id_equal_to_the_user_id_is_not_read_twice(self):
        user = User("u-100", "u-100", "t@example.test", "T")
        self.assertEqual(len(read([], user=user).transport.tool_calls("Task.list")), 3)

    def test_a_task_is_mine_by_creator_assignee_id_or_assignee_employee_id(self):
        rows = [task_row("c", "created", "2026-11-01", created_by="u-100", assignee_id="u-5"),
                task_row("a", "assigned id", "2026-11-02", created_by="u-5", assignee_id="u-100"),
                task_row("e", "assigned employee", "2026-11-03", created_by="u-5", assignee_id="e-100"),
                task_row("x", "someone else", "2026-11-01", created_by="u-5", assignee_id="u-6")]
        self.assertEqual(own_ids(read(rows)), ["c", "a", "e"])

    def test_a_task_found_by_two_reads_is_listed_once(self):
        rows = [task_row("both", "mine twice", "2026-11-01", created_by="u-100", assignee_id="u-100")]
        r = read(rows)
        self.assertEqual(own_ids(r), ["both"])
        self.assertEqual(r.data.others_count, 0)

    def test_open_means_status_not_done_completed_cancelled_canceled_or_closed(self):
        rows = [mine("%d" % i, status, "2026-11-01", status=status)
                for i, status in enumerate(["done", "completed", "cancelled", "canceled", "closed", "Done"])]
        rows += [mine("o1", "open", "2026-11-01", status="open"),
                 mine("o2", "in progress", "2026-11-01", status="in_progress"),
                 mine("o3", "no status", "2026-11-01", status=None)]
        self.assertEqual(sorted(own_ids(read(rows))), ["o1", "o2", "o3"])

    def test_overdue_means_due_before_today_in_the_systems_zone(self):
        rows = [mine("yesterday", "y", "2026-11-05"), mine("today", "t", "2026-11-06"),
                mine("tomorrow", "n", "2026-11-07"), mine("undated", "u", None), mine("blank", "b", "")]
        self.assertEqual(own_ids(read(rows)), ["yesterday"])

    def test_the_due_date_is_its_first_ten_characters(self):
        rows = [mine("dt", "with time", "2026-11-05T23:30:00Z"), mine("today", "t", "2026-11-06T00:00:00Z")]
        r = read(rows)
        self.assertEqual(own_ids(r), ["dt"])
        self.assertEqual(r.data.own[0].due_date, "2026-11-05")

    def test_today_depends_on_the_zone(self):
        now = datetime(2026, 11, 6, 20, 0, tzinfo=timezone.utc)  # 7 Nov in IST, 6 Nov in New York
        rows = [mine("t", "due on the 6th", "2026-11-06")]
        self.assertEqual(own_ids(read(rows, IST, now)), ["t"])
        self.assertEqual(own_ids(read(rows, NY, now)), [])
        self.assertEqual(read(rows, IST, now).data.today, date(2026, 11, 7))

    def test_most_overdue_first_with_days_overdue_and_text(self):
        rows = [mine("1", "one day", "2026-11-05"), mine("5", "five days", "2026-11-01"),
                mine("10", "ten days", "2026-10-27")]
        r = read(rows)
        self.assertEqual([(t.id, t.title, t.due_date, t.days_overdue) for t in r.data.own],
                         [("10", "ten days", "2026-10-27", 10), ("5", "five days", "2026-11-01", 5),
                          ("1", "one day", "2026-11-05", 1)])
        self.assertEqual([t.overdue_text for t in r.data.own], ["10 days overdue", "5 days overdue", "1 day overdue"])

    def test_equal_due_dates_order_by_title(self):
        rows = [mine("b", "Beta", "2026-11-01"), mine("a", "Alpha", "2026-11-01")]
        self.assertEqual(own_ids(read(rows)), ["a", "b"])

    def test_a_server_that_ignores_the_filters_gives_the_same_lists(self):
        rows = [mine("m", "mine", "2026-11-01"), mine("fut", "future", "2026-12-01"),
                task_row("o", "other", "2026-11-02"), mine("d", "done", "2026-11-01", status="done")]
        a, b = read(rows, filters=True), read(rows, filters=False)
        self.assertEqual(own_ids(b), own_ids(a))
        self.assertEqual(own_ids(b), ["m"])
        self.assertEqual(b.data.others_count, a.data.others_count)
        self.assertEqual(b.data.others_count, 1)

    def test_all_pages_of_the_own_reads_are_merged(self):
        rows = [mine("t%d" % i, "task %d" % i, "2026-10-%02d" % (i + 1)) for i in range(5)]
        self.assertEqual(own_ids(read(rows, page_size=2)), ["t0", "t1", "t2", "t3", "t4"])

    def test_the_default_world_tasks_read_as_the_scenarios_expect(self):
        rows = fakes.load_fixture("tasks.json")
        s = read(rows["suryodaya"])
        self.assertEqual([(t.id, t.days_overdue) for t in s.data.own], [("t-s1", 1)])
        self.assertEqual(s.data.others_count, 1)
        k = read(rows["keystone"], NY)
        self.assertEqual([(t.id, t.days_overdue) for t in k.data.own], [("t-k1", 5)])
        self.assertEqual(k.data.others_count, 0)


class OthersCount(unittest.TestCase):
    def test_none_of_mine_and_63_others(self):
        rows = [task_row("o%d" % i, "other %d" % i, "2026-10-%02d" % (1 + i % 28)) for i in range(63)]
        r = read(rows)
        self.assertTrue(r.ok)
        self.assertEqual(r.data.own, [])
        self.assertEqual(r.data.others_count, 63)
        self.assertEqual(r.data.others_line, "63 other overdue tasks are visible to this login")

    def test_others_apply_the_same_open_and_overdue_checks(self):
        rows = [task_row("a", "overdue", "2026-11-01"), task_row("b", "done", "2026-11-01", status="done"),
                task_row("c", "today", "2026-11-06"), task_row("d", "undated", None),
                mine("m", "mine", "2026-11-01")]
        r = read(rows)
        self.assertEqual(r.data.others_count, 1)
        self.assertEqual(own_ids(r), ["m"])

    def test_the_count_line_wording(self):
        part = tasks.TasksPart(date(2026, 11, 6), [], 1)
        self.assertEqual(part.others_line, "1 other overdue task is visible to this login")
        part.others_count = 0
        self.assertEqual(part.others_line, "0 other overdue tasks are visible to this login")
        part.others_count = None
        self.assertEqual(part.others_line,
                         "Count of other overdue tasks unavailable — your own list above is complete")


class Failures(unittest.TestCase):
    def test_each_own_read_failing_makes_the_part_missing(self):
        for key in ("assignee_id", "created_by"):
            r = read([mine("m", "mine", "2026-11-01")], server=lambda s, k=key: s.fail_when(
                "Task.list", lambda a: k in a, ToolError("tasks service down")))
            self.assertFalse(r.ok, key)
            self.assertEqual(r.reason, "Suryodaya tasks: couldn't be read (reason: tasks service down)"
                                       " — report it if it repeats")

    def test_the_employee_read_failing_makes_the_part_missing_too(self):
        r = read([], server=lambda s: s.fail_when("Task.list", lambda a: a.get("assignee_id") == "e-100",
                                                   ToolError("nope")))
        self.assertFalse(r.ok)

    def test_only_the_others_read_failing_keeps_the_own_list_and_a_complete_part(self):
        """P-B-17: the others read breaks on page 2 of 3."""
        rows = [mine("m", "mine", "2026-11-01")] + [task_row("o%d" % i, "o", "2026-11-02") for i in range(4)]

        def others_page_two(args):
            return not any(k in args for k in ("assignee_id", "created_by")) and args["offset"] >= 2
        r = read(rows, page_size=2, server=lambda s: s.fail_when("Task.list", others_page_two, ToolError("boom")))
        self.assertTrue(r.ok)
        self.assertEqual(own_ids(r), ["m"])
        self.assertIsNone(r.data.others_count)
        self.assertEqual(r.data.others_line,
                         "Count of other overdue tasks unavailable — your own list above is complete")

    def test_login_expired_and_unreachable_have_their_own_lines(self):
        self.assertEqual(read([], server=lambda s: s.expire()).reason, "Suryodaya: login expired — log in again")
        self.assertEqual(read([], server=lambda s: s.down()).reason,
                         "Suryodaya: can't be reached right now — try again later")

    def test_the_others_read_hitting_login_expired_marks_the_part_expired(self):
        # P-B-2: a 401 is never "count unavailable"; the part is missing and says the login expired.
        def handler(args):
            if not any(k in args for k in ("assignee_id", "created_by")):
                raise fakes.HttpStatus(401)
            return {"data": [], "total": 0, "has_more": False}
        r = read([], server=lambda s: s.handler("Task.list", handler))
        self.assertFalse(r.ok)
        self.assertTrue(r.expired)
        self.assertEqual(r.reason, "Suryodaya: login expired — log in again")

    def test_a_401_on_an_own_read_marks_the_part_expired(self):
        r = read([], server=lambda s: s.expire())
        self.assertTrue(r.expired)

    def test_other_failures_are_not_marked_expired(self):
        self.assertFalse(read([], server=lambda s: s.down()).expired)

    def test_a_task_created_by_the_employee_id_is_not_the_users(self):
        """P-B-15 / G4-AC1: created_by matches the user id only; the employee id is for assignees."""
        rows = [task_row("x", "employee-created", "2026-11-01", created_by="e-100", assignee_id="u-5"),
                task_row("y", "user-created", "2026-11-02", created_by="u-100", assignee_id="u-5")]
        r = read(rows, filters=False)  # a server that ignores filters: the ownership test decides
        self.assertEqual(own_ids(r), ["y"])
        self.assertEqual(r.data.others_count, 1)

    def test_only_the_task_tool_is_called(self):
        self.assertEqual(read([]).transport.tool_names(), {"Task.list"})


if __name__ == "__main__":
    unittest.main()
