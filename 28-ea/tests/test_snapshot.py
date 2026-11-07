"""tools/snapshot.py: read-only before/after snapshots and their diff (G5-AC5 helper; the live run is by hand)."""
import contextlib
import importlib.util
import io
import json
import tempfile
import unittest
from pathlib import Path

from tests import fakes
from tests.fakes import TOKEN_KEYSTONE, TOKEN_SURYODAYA, event_row, task_row

_spec = importlib.util.spec_from_file_location("snapshot_tool", Path(__file__).resolve().parent.parent / "tools" / "snapshot.py")
snapshot = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(snapshot)


def world(mailbox=False):
    """The default fake world plus an (empty) EmailMessage.list on both systems."""
    transport, surya, keystone = fakes.default_world(mailbox=mailbox)
    for server in (surya, keystone):
        server.rows("EmailMessage.list", [])
    return transport, surya, keystone


class Snapshot(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "snaps"
        self.env = {"HOME": tmp.name, "AGENTSWITCH_SURYODAYA_TOKEN": TOKEN_SURYODAYA,
                    "AGENTSWITCH_KEYSTONE_TOKEN": TOKEN_KEYSTONE}

    def run_tool(self, action, transport, *extra):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = snapshot.main([action, "--dir", str(self.dir)] + list(extra), self.env, transport)
        return code, out.getvalue(), err.getvalue()

    def load(self, name):
        return json.loads((self.dir / (name + ".json")).read_text())

    def test_before_and_after_of_an_unchanged_world_diff_to_nothing(self):
        transport, surya, _ = world(mailbox=True)
        surya.rows("EmailMessage.list", [{"id": "m1", "updated_at": "2026-11-05T10:00:00Z"}])
        self.assertEqual(self.run_tool("before", transport)[0], 0)
        self.assertEqual(self.run_tool("after", transport)[0], 0)
        code, out, _ = self.run_tool("diff", transport)
        self.assertEqual((code, out.strip()), (0, "No differences"))

    def test_it_only_calls_read_tools_and_stores_no_token_or_content(self):
        transport, surya, _ = world()
        surya.rows("EmailMessage.list", [])
        surya.rows("CalendarEvent.list", [event_row("e1", "Secret title", "2026-11-06T04:00:00Z", "2026-11-06T05:00:00Z")])
        self.run_tool("before", transport)
        self.assertLessEqual(transport.tool_names(), fakes.READ_TOOLS)
        text = (self.dir / "before.json").read_text()
        for needle in (TOKEN_SURYODAYA, TOKEN_KEYSTONE, "Secret title"):
            self.assertNotIn(needle, text)
        self.assertIn("e1", text)

    def test_added_removed_and_changed_items_are_reported_with_the_exit_code(self):
        transport, surya, _ = world()
        tasks = [dict(task_row("t1", "a", "2026-11-01"), updated_at="1"), dict(task_row("t2", "b", "2026-11-01"), updated_at="1")]
        surya.rows("Task.list", tasks)
        self.run_tool("before", transport)
        surya.rows("Task.list", [dict(tasks[0], updated_at="2"), dict(task_row("t3", "c", "2026-11-01"), updated_at="1")])
        self.run_tool("after", transport)
        code, out, _ = self.run_tool("diff", transport)
        self.assertEqual(code, 1)
        self.assertIn("suryodaya tasks: added t3", out)
        self.assertIn("suryodaya tasks: removed t2", out)
        self.assertIn("suryodaya tasks: changed t1 (1 -> 2)", out)

    def test_a_new_draft_is_exactly_one_difference(self):
        transport, surya, _ = world(mailbox=True)
        surya.rows("EmailMessage.list", [{"id": "m1", "updated_at": "1"}])
        self.run_tool("before", transport)
        surya.rows("EmailMessage.list", [{"id": "m1", "updated_at": "1"}, {"id": "d1", "updated_at": "2"}])
        self.run_tool("after", transport)
        _, out, _ = self.run_tool("diff", transport)
        self.assertEqual(out.strip().splitlines(), ["suryodaya mail: added d1"])

    def test_pages_are_read_in_full(self):
        transport, surya, _ = world()
        surya.rows("Task.list", [dict(task_row("t%d" % i, "x", None), updated_at="1") for i in range(250)])
        self.run_tool("before", transport, "--system", "suryodaya")
        self.assertEqual(len(self.load("before")["suryodaya"]["tasks"]["items"]), 250)

    def test_a_system_that_cannot_be_read_is_reported_not_compared(self):
        transport, surya, keystone = world()
        self.run_tool("before", transport)
        keystone.expire()
        self.run_tool("after", transport)
        code, out, _ = self.run_tool("diff", transport)
        self.assertEqual(code, 1)
        self.assertIn("keystone tasks: could not be compared", out)

    def test_system_option_limits_the_snapshot(self):
        transport, _, _ = world()
        self.run_tool("before", transport, "--system", "keystone")
        self.assertEqual(list(self.load("before")), ["keystone"])
        self.assertEqual(transport.calls_to(fakes.SURYODAYA_BASE), [])

    def test_diff_without_both_files_exits_two(self):
        self.assertEqual(self.run_tool("diff", None)[0], 2)


if __name__ == "__main__":
    unittest.main()
