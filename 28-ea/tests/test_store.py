"""store.py: redact, file names, the nothing-fetched rule, atomic writes, save-failure message
(P-B-5 to P-B-8, P-B-19)."""
import hashlib
import json
import os
import stat
import tempfile
import unittest
from datetime import date
from pathlib import Path

from ea_briefing import store
from ea_briefing.mcp import PartResult
from ea_briefing.render import SystemResult

DAY = date(2026, 11, 6)
GOOD = PartResult.complete(object())
GONE = PartResult.missing("x")


def results(*parts):
    return [SystemResult("suryodaya", "Suryodaya", *parts)]


FETCHED = results(GONE, GOOD, GOOD)
NOTHING = results(GONE, GONE, GONE)


class Redact(unittest.TestCase):
    def test_every_secret_occurrence_becomes_stars(self):
        text = "a TOKEN-1 b TOKEN-1 c KEY-2"
        self.assertEqual(store.redact(text, ["TOKEN-1", "KEY-2"]), "a *** b *** c ***")

    def test_none_empty_and_missing_secrets_are_ignored(self):
        self.assertEqual(store.redact("plain text", [None, "", "zzz"]), "plain text")
        self.assertEqual(store.redact("plain text", []), "plain text")

    def test_a_secret_that_contains_another_is_replaced_whole(self):
        self.assertEqual(store.redact("see abc-def here", ["abc", "abc-def"]), "see *** here")

    def test_redact_value_walks_dicts_and_lists_and_keys(self):
        value = {"title": "has SEC in it", "list": ["SEC", 3, None, {"deep": "x SEC"}], "SEC": True}
        out = store.redact_value(value, ["SEC"])
        self.assertEqual(out, {"title": "has *** in it", "list": ["***", 3, None, {"deep": "x ***"}], "***": True})


class FileNames(unittest.TestCase):
    def test_full_run_is_the_date_alone(self):
        self.assertEqual(store.file_name(DAY, ["suryodaya", "keystone"]), "2026-11-06.md")
        self.assertEqual(store.file_name(DAY, ["keystone", "suryodaya"]), "2026-11-06.md")

    def test_one_system_run_carries_the_system(self):
        self.assertEqual(store.file_name(DAY, ["keystone"]), "2026-11-06-keystone.md")
        self.assertEqual(store.file_name(DAY, ["suryodaya"]), "2026-11-06-suryodaya.md")


class Dir(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name) / "briefings"
        self.dir.mkdir()

    def digest(self, name):
        return hashlib.sha256((self.dir / name).read_bytes()).hexdigest()


class Save(Dir):
    def save(self, text="# hello\n", res=FETCHED, systems=("suryodaya", "keystone"), secrets=()):
        return store.save(text, res, self.dir, DAY, list(systems), secrets)

    def test_writes_the_dated_file_and_says_saved(self):
        outcome = self.save()
        self.assertEqual(outcome.status, "saved")
        self.assertEqual(outcome.path, self.dir / "2026-11-06.md")
        self.assertEqual((self.dir / "2026-11-06.md").read_text(encoding="utf-8"), "# hello\n")

    def test_replaces_the_same_kind_of_file_and_nothing_else(self):
        (self.dir / "2026-11-06.md").write_text("old")
        (self.dir / "2026-11-05.md").write_text("yesterday")
        (self.dir / "2026-11-06-keystone.md").write_text("one system")
        keep = {n: self.digest(n) for n in ("2026-11-05.md", "2026-11-06-keystone.md")}
        self.save("new\n")
        self.assertEqual((self.dir / "2026-11-06.md").read_text(), "new\n")
        for name, digest in keep.items():
            self.assertEqual(self.digest(name), digest)

    def test_a_one_system_run_leaves_the_full_file_byte_identical(self):
        (self.dir / "2026-11-06.md").write_bytes(b"full\n")
        before = self.digest("2026-11-06.md")
        outcome = self.save(systems=["keystone"])
        self.assertEqual(outcome.path.name, "2026-11-06-keystone.md")
        self.assertEqual(self.digest("2026-11-06.md"), before)

    def test_redacts_secrets_in_the_file(self):
        self.save("title SECRET-TOK here\n", secrets=["SECRET-TOK"])
        self.assertEqual((self.dir / "2026-11-06.md").read_text(), "title *** here\n")

    def test_the_file_is_saved_even_when_a_secret_was_found(self):
        self.assertEqual(self.save("x SECRET-TOK", secrets=["SECRET-TOK"]).status, "saved")

    def test_creates_a_missing_briefing_folder(self):
        target = self.dir / "new" / "deeper"
        outcome = store.save("x", FETCHED, target, DAY, ["suryodaya", "keystone"], [])
        self.assertEqual(outcome.status, "saved")
        self.assertTrue((target / "2026-11-06.md").exists())

    def test_no_temporary_file_is_left_behind(self):
        self.save()
        self.assertEqual([p.name for p in self.dir.iterdir()], ["2026-11-06.md"])

    @unittest.skipIf(os.name == "nt", "POSIX file modes")
    def test_the_file_is_private_to_the_user(self):
        self.save()
        mode = stat.S_IMODE(os.stat(self.dir / "2026-11-06.md").st_mode)
        self.assertEqual(mode & 0o077, 0)


class NothingFetched(Dir):
    def test_writes_no_file_and_keeps_the_earlier_one_byte_identical(self):
        (self.dir / "2026-11-06.md").write_bytes(b"earlier\n")
        before = self.digest("2026-11-06.md")
        outcome = store.save("# all missing\n", NOTHING, self.dir, DAY, ["suryodaya", "keystone"], [])
        self.assertEqual(outcome.status, "nothing_fetched")
        self.assertEqual(outcome.message, "Nothing fetched — today's earlier briefing file kept")
        self.assertEqual(self.digest("2026-11-06.md"), before)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["2026-11-06.md"])

    def test_creates_no_folder_either(self):
        target = self.dir / "never"
        store.save("x", NOTHING, target, DAY, ["suryodaya"], [])
        self.assertFalse(target.exists())

    def test_one_part_ok_is_enough_to_save(self):
        outcome = store.save("x", results(GONE, GONE, GOOD), self.dir, DAY, ["suryodaya"], [])
        self.assertEqual(outcome.status, "saved")


class SaveFailure(Dir):
    def test_folder_blocked_by_a_file_gives_the_prd_message(self):
        blocker = self.dir / "blocker"
        blocker.write_text("i am a file")
        outcome = store.save("x", FETCHED, blocker / "inside", DAY, ["suryodaya", "keystone"], [])
        self.assertEqual(outcome.status, "failed")
        self.assertTrue(outcome.message.startswith("Briefing shown but not saved to file: "), outcome.message)
        self.assertTrue(outcome.message.endswith(
            " — copy it from the screen, or check that the briefing folder exists and you can write to it, "
            "then rerun"), outcome.message)

    @unittest.skipIf(not hasattr(os, "geteuid") or os.geteuid() == 0, "root ignores directory modes")
    def test_read_only_folder_fails_and_the_earlier_file_is_intact(self):
        (self.dir / "2026-11-06.md").write_bytes(b"earlier\n")
        before = self.digest("2026-11-06.md")
        os.chmod(self.dir, 0o500)
        self.addCleanup(os.chmod, self.dir, 0o700)
        outcome = store.save("new", FETCHED, self.dir, DAY, ["suryodaya", "keystone"], [])
        self.assertEqual(outcome.status, "failed")
        self.assertIn("Briefing shown but not saved to file", outcome.message)
        self.assertEqual(self.digest("2026-11-06.md"), before)

    def test_a_failed_replace_keeps_the_earlier_file_and_removes_the_temporary_one(self):
        (self.dir / "2026-11-06.md").write_bytes(b"earlier\n")
        before = self.digest("2026-11-06.md")
        real = os.replace

        def boom(src, dst):
            raise OSError("disk full")
        store.os.replace = boom
        try:
            outcome = store.save("new", FETCHED, self.dir, DAY, ["suryodaya", "keystone"], [])
        finally:
            store.os.replace = real
        self.assertEqual(outcome.status, "failed")
        self.assertIn("disk full", outcome.message)
        self.assertEqual(self.digest("2026-11-06.md"), before)
        self.assertEqual([p.name for p in self.dir.iterdir()], ["2026-11-06.md"])


class SaveJson(Dir):
    def test_writes_redacted_json_atomically(self):
        path = self.dir / "result.json"
        outcome = store.save_json({"title": "tok SECRET here", "n": 1}, path, ["SECRET"])
        self.assertEqual(outcome.status, "saved")
        self.assertEqual(json.loads(path.read_text(encoding="utf-8")), {"title": "tok *** here", "n": 1})
        self.assertEqual([p.name for p in self.dir.iterdir()], ["result.json"])

    def test_failure_names_the_file_and_the_reason(self):
        blocker = self.dir / "blocker"
        blocker.write_text("file")
        outcome = store.save_json({}, blocker / "result.json", [])
        self.assertEqual(outcome.status, "failed")
        self.assertTrue(outcome.message.startswith("JSON result not saved to "), outcome.message)

    def test_replaces_an_existing_json_file(self):
        path = self.dir / "result.json"
        path.write_text("{}")
        store.save_json({"a": 1}, path, [])
        self.assertEqual(json.loads(path.read_text()), {"a": 1})


if __name__ == "__main__":
    unittest.main()
