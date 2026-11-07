"""Scenario tests: the wired command, `cli.main`, end to end with fakes (plan R2, test strategy).

One `test_story<N>_scenario<M>_*` per PRD scenario. Slice 1 covers Story 1 Scenarios 2-6, Story 3,
Story 4 and Story 2 Scenario 2. Slice 2 adds Story 1 Scenario 1 (every part complete, P-B-46) and
Story 2 Scenarios 1, 3, 4 and 5 with a fake model gateway at a third base URL (`fakes.MODEL_BASE`).

THE INJECTION SEAM (what `cli.main` must offer; written here, against the plan, not an implementation)

    cli.main(argv, env, stdin, clock, transport=None) -> int      # the exit code

* `argv`: list of arguments after `briefing.py` (`["--system", "keystone"]`).
* `env`: a dict used instead of `os.environ`. Tests set `HOME` (login files live under
  `$HOME/.config/agentswitch/`), `BRIEFING_DIR` (where briefings are written) and the token
  variables. `cli.main` must never read `os.environ` or the real home for these.
* `stdin`: a text stream (here `io.StringIO`, so never a terminal).
* `clock`: an object with `now()`, `monotonic()`, `local_date()` (see tests/fakes.py `FakeClock`).
* `transport`: the HTTP seam, `transport(method, url, headers, body, timeout) -> (status,
  content_type, text)`, passed to `McpClient` and `auth`. `None` means the real urllib transport.
* The briefing goes to `sys.stdout`, progress lines ("Reading Suryodaya…") to `sys.stderr`; the tests
  capture both with `contextlib.redirect_stdout/redirect_stderr`. `cli.main` returns the exit code
  (a `SystemExit(n)` from argument parsing is accepted too).

OUTPUT CONTRACT these tests check (the render/store/cli workers must produce it)

* One `## Suryodaya` and one `## Keystone` heading line per system shown, each followed by
  `### Mail`, `### Meetings`, `### Tasks` heading lines with that part's text below.
* Exactly one status line, starting `Status:`, after the sections. For each system it contains the
  system's name followed by `Mail|Meetings|Tasks complete|missing` in that order, and the elapsed
  time as a number followed by ` s` (for example `0.0 s`).
* A meeting is one line holding its title, `HH:MM–HH:MM <ZONE>` (an en dash), the organiser or `—`,
  and the words `clash` and/or `declined` when marked. An all-day event reads `all day`.
* An overdue task is one line holding its title, its due date (`YYYY-MM-DD`) and `N days overdue`.
* Files are `<BRIEFING_DIR>/<local date>.md` (full run) or `<local date>-<system>.md` (one system).
* `--json <path>` writes JSON shaped like (only the slice-1 keys the tests read are listed):
      {"systems": {"<system>": {
          "mail":     {"complete": false, "reason": "..."},
          "meetings": {"complete": true, "events": [{"id": "...", "clash": bool, "declined": bool}]},
          "tasks":    {"complete": true, "own_overdue": [{"id": "...", "days_overdue": n}],
                       "others_count": n}}}}
  A missing part is `{"complete": false, "reason": "<the line the Markdown shows>"}`.

SLICE 2 CONTRACT (mail; written here against the plan, for `render` and `cli` to meet)

* The model is reached through `BRIEFING_LLM_URL` + `BRIEFING_LLM_MODEL` (or `OPENAI_*`); `fakes.FakeModelServer`
  answers `<url>/chat/completions`. Every test that needs drafts passes `mail_env()`.
* Each message needing an answer is an entry in the Mail part holding its sender address, subject, the
  received time as `HH:MM <ZONE>` in the system's zone, then either the summary or `(no summary — first line
  shown)` plus the first line, then either the reply (greeting, body, signature) or
  `No draft for this message (<reason>) — reply by hand`, and the words `needs your decision` when marked.
  Entries are oldest first. Skipped mail sits under `Skipped — no reply expected`, one line each with
  subject and reason. A message that couldn't be read says `1 message couldn't be opened (subject: …)`; a
  message with a saved draft says `draft already saved` with the draft's text.
* With no mail needing an answer a complete Mail part says `No mail needs an answer`.
* `--json` adds, to a complete mail part:
      {"complete": true,
       "needs_answer": [{"id": "...", "draft_status": "proposed|model_unavailable|time_limit|invalid|
                                                       needs_decision_redraft_failed"}],
       "skipped": [{"id": "...", "reason": "..."}], "unopened": ["id"], "draft_saved": ["id"]}
  and, at the top level, `"model": {"calls": n, "failures": n, "unreachable": bool}`.
* Progress lines `Drafting replies (n of m)…` go to stderr only.

SLICE 3 CONTRACT (approval; written here against the plan, for `approval`, `render` and `cli` to meet)

    cli.main(argv, env, stdin, clock, transport=None, getpass_fn=None, editor=None) -> int

* `editor(text) -> edited text` is injected by tests (`fakes.FakeEditor`); `None` means the real chain
  (`$EDITOR`, then `nano`, then typed lines). The `e` choice needs `stdin.isatty()`; `fakes.TtyInput` is a
  scripted stdin that says it is a terminal, a plain `io.StringIO` says it is not.
* After the briefing is printed and saved, each proposed reply (a message with a valid draft) is offered on
  STDERR, oldest first per system, Suryodaya before Keystone: the offer holds `[s]ave / [e]dit then save /
  [Enter] skip / [q] stop`, a save prints `Saving draft…` then `Saved` (or the failure line), and the step
  ends with `N saved (M edited), N skipped, N not saved`. With no proposed replies it prints
  `No drafts to approve`. Stdout holds only the briefing.
* A save is `EmailMessage.create` with `folder="drafts"`, `is_draft=true`, `company_id`, `mailbox_id`,
  `from_email` (the mailbox address), `to` (the original's sender), `subject` ("Re: " + subject),
  `body_text` (the approved text), `thread_id`, `in_reply_to` (the original's message id) and `references`
  (the original's references with its message id added). It is preceded by an `EmailMessage.get` of the
  original (answered: "Already answered — draft not saved") and followed by an `EmailMessage.get` of the new
  draft (a failed read: "Draft may not be saved — check your Drafts folder").
* After the step the Markdown file is saved again with an `## Approval` section after the status line (the
  summary line, and for every failed save its approved text quoted); `--json` is written after the step and
  adds `"approval": {"saved", "edited", "skipped", "not_saved", "failed": [{"system", "id", "subject",
  "reason", "text"}]}`.
* Stdin at end of file skips every reply (nothing is saved, nothing is "not saved"); `q` leaves the reply it
  was chosen on and every later one "not saved".
"""
import contextlib
import hashlib
import io
import json
import os
import re
import tempfile
import unittest
from pathlib import Path

from tests import fakes
from tests.fakes import (KEYSTONE_BASE, MODEL_BASE, MODEL_KEY, MODEL_NAME, MODEL_URL, SURYODAYA_BASE,
                         TOKEN_KEYSTONE, TOKEN_SURYODAYA, FakeClock, FakeEditor, FakeModelServer, HttpStatus,
                         ToolError, TtyInput,
                         attendee, event_row, message_row, model_reply, prompt_text, serve_mail, task_row)

NO_MAILBOX = "Suryodaya: no mailbox for this login — ask the course team to set one up"
EXPIRED = "Keystone: login expired — log in again"
ME_EMAIL = "team28c@example.test"
DATE = "2026-11-06"


class Result:
    def __init__(self, code, out, err):
        self.code, self.out, self.err = code, out, err


def section(text, system):
    """Lines of a system's section: after `## <System>` up to the next `## ` or `Status:` line."""
    lines, grab, found = [], False, []
    for line in text.splitlines():
        if line.startswith("## "):
            grab = line[3:].strip().lower().startswith(system.lower())
            continue
        if line.startswith("Status:"):
            grab = False
        if grab:
            found.append(line)
    return "\n".join(found)


def part(section_text, name):
    """Text of a part: after `### <name>` up to the next `### ` line."""
    lines, grab, found = section_text.splitlines(), False, []
    for line in lines:
        if line.startswith("### "):
            grab = line[4:].strip().lower().startswith(name.lower())
            continue
        if grab:
            found.append(line)
    return "\n".join(found)


def line_with(text, needle):
    for line in text.splitlines():
        if needle in line:
            return line
    return None


def status_line(text):
    lines = [l for l in text.splitlines() if l.startswith("Status:")]
    assert len(lines) == 1, "expected exactly one status line, found %d in:\n%s" % (len(lines), text)
    return lines[0]


def status_of(line, system, other):
    """The part of the status line about `system` (up to `other`'s name, if it comes after)."""
    start = line.index(system)
    end = line.find(other, start + 1)
    return line[start:end if end > start else None]


class ScenarioCase(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.home = Path(self._tmp.name) / "home"
        self.home.mkdir()
        self.briefings = Path(self._tmp.name) / "briefings"
        self.briefings.mkdir()
        self.clock = FakeClock()

    # ---- running
    def env(self, **extra):
        env = {"HOME": str(self.home), "BRIEFING_DIR": str(self.briefings),
               "AGENTSWITCH_SURYODAYA_TOKEN": TOKEN_SURYODAYA,
               "AGENTSWITCH_KEYSTONE_TOKEN": TOKEN_KEYSTONE}
        env.update(extra)
        return env

    def mail_env(self, **extra):
        """The local-mode environment plus a model endpoint at the fake gateway."""
        return self.env(BRIEFING_LLM_URL=MODEL_URL, BRIEFING_LLM_MODEL=MODEL_NAME, **extra)

    def mail_world(self, suryodaya=None, keystone=None, responder=None):
        """Both systems with a mailbox holding the given inbox rows, plus a fake model gateway.
        Returns (transport, suryodaya_server, keystone_server, model_server)."""
        transport, surya, keystone = fakes.default_world(mailbox=True, messages={
            "suryodaya": suryodaya or [], "keystone": keystone or []})
        model = transport.add_server(MODEL_BASE, FakeModelServer(responder))
        return transport, surya, keystone, model

    def run_briefing(self, transport, argv=(), env=None, stdin_text="", stdin=None, editor=None):
        import ea_briefing
        if not (Path(ea_briefing.__file__).parent / "cli.py").exists():
            self.fail("ea_briefing.cli is not built yet (plan task 12): there is no command to run")
        from ea_briefing import cli
        out, err = io.StringIO(), io.StringIO()
        code = None
        extra = {"editor": editor} if editor is not None else {}
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv), env if env is not None else self.env(),
                                stdin if stdin is not None else io.StringIO(stdin_text), self.clock,
                                transport=transport, **extra)
            except SystemExit as e:
                code = e.code
        return Result(code, out.getvalue(), err.getvalue())

    def briefing_file(self, name=None):
        return self.briefings / (name or DATE + ".md")

    def digest(self, name=None):
        return hashlib.sha256(self.briefing_file(name).read_bytes()).hexdigest()


class Story1RunTheBriefing(ScenarioCase):
    def test_story1_scenario1_sections_status_line_and_file(self):
        """P-B-1: both systems have a mailbox and mail; every part is read, every Mail part is complete."""
        rows = fakes.load_fixture("messages.json")
        transport, _, _, _ = self.mail_world(rows["suryodaya"], rows["keystone"])
        r = self.run_briefing(transport, env=self.mail_env())
        self.assertEqual(r.code, 0, r.err)
        for system in ("Suryodaya", "Keystone"):
            sec = section(r.out, system)
            for name in ("Mail", "Meetings", "Tasks"):
                self.assertNotEqual(part(sec, name).strip(), "", "%s %s part is empty" % (system, name))
            self.assertNotIn("not built yet", part(sec, "Mail"))
        self.assertIn("Where is the seat report?", part(section(r.out, "Suryodaya"), "Mail"))
        self.assertIn("Demo notes", part(section(r.out, "Keystone"), "Mail"))
        for name in ("Mail", "Meetings", "Tasks"):
            headings = [l for l in r.out.splitlines() if l.startswith("### " + name)]
            self.assertEqual(len(headings), 2, "one '### %s' heading per system" % name)
        line = status_line(r.out)
        for system, other in (("Suryodaya", "Keystone"), ("Keystone", "Suryodaya")):
            self.assertRegex(status_of(line, system, other), r"Mail complete.*Meetings complete.*Tasks complete")
        self.assertRegex(line, r"\d+(\.\d+)? s\b")
        saved = self.briefing_file().read_text(encoding="utf-8")
        self.assertIn(status_line(r.out), saved)
        self.assertIn("## Suryodaya", saved)
        self.assertIn("## Keystone", saved)

    def test_story1_scenario1_every_part_complete_with_nothing_to_report(self):
        """P-B-46: mailbox on both systems, every list read in full and empty: all six parts complete."""
        transport, surya, keystone, model = self.mail_world()
        for server in (surya, keystone):
            server.rows("CalendarEvent.list", [])
            server.rows("Task.list", [])
        r = self.run_briefing(transport, env=self.mail_env())
        self.assertEqual(r.code, 0, r.err)
        line = status_line(r.out)
        for system, other in (("Suryodaya", "Keystone"), ("Keystone", "Suryodaya")):
            self.assertRegex(status_of(line, system, other), r"Mail complete.*Meetings complete.*Tasks complete")
        self.assertNotIn("missing", line)
        for system in ("Suryodaya", "Keystone"):
            sec = section(r.out, system)
            self.assertIn("No mail needs an answer", part(sec, "Mail"))
            self.assertIn("No meetings today", part(sec, "Meetings"))
            self.assertIn("No overdue tasks of yours", part(sec, "Tasks"))
        self.assertEqual(model.requests, [])

    def test_story1_scenario2_keystone_login_expired(self):
        """P-B-2: Keystone's 401 fails all its parts; Suryodaya is complete; exit 0."""
        transport, _, keystone = fakes.default_world()
        keystone.expire()
        r = self.run_briefing(transport)
        self.assertEqual(r.code, 0, r.err)
        ks = section(r.out, "Keystone")
        for name in ("Mail", "Meetings", "Tasks"):
            self.assertIn(EXPIRED, part(ks, name))
        sy = section(r.out, "Suryodaya")
        self.assertIn("Daily standup", part(sy, "Meetings"))
        self.assertNotIn("login expired", sy)
        line = status_line(r.out)
        self.assertRegex(status_of(line, "Keystone", "Suryodaya"), r"Mail missing.*Meetings missing.*Tasks missing")
        self.assertRegex(status_of(line, "Suryodaya", "Keystone"), r"Meetings complete.*Tasks complete")
        self.assertTrue(self.briefing_file().exists())

    def test_story1_scenario2_a_401_on_one_keystone_tool_fails_all_three_keystone_parts(self):
        """P-B-2 / G1-AC2: Task.list answering 401 on Keystone is the login line on all its parts."""
        transport, _, keystone = fakes.default_world()
        keystone.fail("Task.list", HttpStatus(401))
        r = self.run_briefing(transport)
        self.assertEqual(r.code, 0, r.err)
        ks = section(r.out, "Keystone")
        for name in ("Mail", "Meetings", "Tasks"):
            self.assertIn(EXPIRED, part(ks, name))
        self.assertNotIn("Class sync", ks)
        sy = section(r.out, "Suryodaya")
        self.assertIn("Daily standup", part(sy, "Meetings"))
        self.assertNotIn("login expired", sy)
        line = status_line(r.out)
        self.assertRegex(status_of(line, "Keystone", "Suryodaya"), r"Mail missing.*Meetings missing.*Tasks missing")
        self.assertRegex(status_of(line, "Suryodaya", "Keystone"), r"Meetings complete.*Tasks complete")

    def test_a_protocol_error_on_one_system_is_unreachable_and_the_other_system_prints(self):
        """A transport raising http.client.BadStatusLine must not crash the run (exit 0, other system complete)."""
        import http.client
        transport, _, keystone = fakes.default_world()
        keystone.down(http.client.BadStatusLine("garbage"))
        r = self.run_briefing(transport)
        self.assertEqual(r.code, 0, r.err)
        ks = section(r.out, "Keystone")
        for name in ("Mail", "Meetings", "Tasks"):
            self.assertIn("Keystone: can't be reached right now — try again later", part(ks, name))
        self.assertIn("Daily standup", part(section(r.out, "Suryodaya"), "Meetings"))
        self.assertRegex(status_of(status_line(r.out), "Suryodaya", "Keystone"), r"Meetings complete.*Tasks complete")

    def test_story1_scenario3_limit_to_one_system(self):
        """P-B-5: --system keystone reads only Keystone and leaves <date>.md byte-identical."""
        self.briefing_file().write_bytes(b"# earlier full briefing\n")
        before = self.digest()
        transport, _, _ = fakes.default_world()
        r = self.run_briefing(transport, ["--system", "keystone"])
        self.assertEqual(r.code, 0, r.err)
        self.assertIn("## Keystone", r.out)
        self.assertNotIn("Suryodaya", r.out)
        self.assertNotIn("Suryodaya", status_line(r.out))
        self.assertIn("Keystone", status_line(r.out))
        self.assertEqual(transport.calls_to(SURYODAYA_BASE), [])
        self.assertTrue(self.briefing_file(DATE + "-keystone.md").exists())
        self.assertEqual(self.digest(), before)

    def test_story1_scenario4_rerun_replaces_only_the_same_kind_of_file(self):
        """P-B-6: the full file of the day is replaced; other dates and one-system files are untouched."""
        self.briefing_file().write_bytes(b"old full\n")
        self.briefing_file("2026-11-05.md").write_bytes(b"yesterday\n")
        self.briefing_file(DATE + "-keystone.md").write_bytes(b"old keystone only\n")
        others = {n: self.digest(n) for n in ("2026-11-05.md", DATE + "-keystone.md")}
        transport, _, _ = fakes.default_world()
        r = self.run_briefing(transport)
        self.assertEqual(r.code, 0, r.err)
        text = self.briefing_file().read_text(encoding="utf-8")
        self.assertNotEqual(text, "old full\n")
        self.assertIn("## Suryodaya", text)
        self.assertIn("## Keystone", text)
        for name, digest in others.items():
            self.assertEqual(self.digest(name), digest, name)

    def test_story1_scenario4_file_date_is_the_machine_local_date(self):
        """SD-D-12: the date in the file name comes from clock.local_date(), not the system's zone."""
        from datetime import timedelta, timezone
        # FIXED_NOW is 06:00 UTC on 6 Nov: still 5 Nov (23:00) on a machine at UTC-7, while both
        # systems' own day is 6 Nov. (The earlier +14 h offset gave 20:00 on 6 Nov, so it proved nothing.)
        clock = FakeClock(local_tz=timezone(timedelta(hours=-7)))
        self.clock = clock
        transport, _, _ = fakes.default_world()
        r = self.run_briefing(transport)
        self.assertEqual(r.code, 0, r.err)
        self.assertTrue(self.briefing_file("2026-11-05.md").exists())
        self.assertFalse(self.briefing_file().exists())

    def test_story1_scenario5_nothing_fetched_keeps_the_earlier_file(self):
        """P-B-7, P-B-23: neither system reachable; reasons shown, no write, exit code 1."""
        self.briefing_file().write_bytes(b"earlier full briefing\n")
        before = self.digest()
        transport, surya, keystone = fakes.default_world()
        surya.down()
        keystone.down()
        r = self.run_briefing(transport)
        self.assertEqual(r.code, 1, r.err)
        self.assertIn("Nothing fetched — today's earlier briefing file kept", r.out + r.err)
        self.assertIn("Suryodaya: can't be reached right now — try again later", r.out)
        self.assertIn("Keystone: can't be reached right now — try again later", r.out)
        self.assertEqual(self.digest(), before)
        self.assertEqual(sorted(p.name for p in self.briefings.iterdir()), [DATE + ".md"])

    def test_story1_scenario5_no_mailbox_counts_as_missing_for_nothing_fetched(self):
        """P-B-7: every requested part missing, 'no mailbox' included, writes no file and exits 1."""
        self.briefing_file().write_bytes(b"earlier full briefing\n")
        before = self.digest()
        transport, surya, _ = fakes.default_world()  # no mailbox
        surya.fail("CalendarEvent.list", ToolError("calendar offline"))
        surya.fail("Task.list", ToolError("tasks offline"))
        r = self.run_briefing(transport, ["--system", "suryodaya"])
        self.assertEqual(r.code, 1, r.err)
        self.assertIn(NO_MAILBOX, r.out)
        self.assertIn("Nothing fetched — today's earlier briefing file kept", r.out + r.err)
        self.assertEqual(self.digest(), before)

    def test_story1_scenario6_a_run_changes_nothing(self):
        """P-B-18 at the wired level: only read tools are called, and only reads of /api/auth/me."""
        transport, _, _ = fakes.default_world(mailbox=True)
        r = self.run_briefing(transport)
        self.assertEqual(r.code, 0, r.err)
        self.assertTrue(transport.tool_calls(), "the run made no platform calls at all")
        self.assertLessEqual(transport.tool_names(), fakes.READ_TOOLS)
        for call in transport.calls:
            if call.tool is None:
                self.assertEqual((call.method, call.path), ("GET", "/api/auth/me"))
            else:
                self.assertEqual(call.method, "POST")

    def test_progress_lines_go_to_stderr_only(self):
        """P-B-9: 'Reading …' on stderr; stdout and the file hold only the briefing."""
        transport, _, _ = fakes.default_world()
        r = self.run_briefing(transport)
        self.assertIn("Reading Suryodaya…", r.err)
        self.assertIn("Reading Keystone…", r.err)
        saved = self.briefing_file().read_text(encoding="utf-8")
        for text in (r.out, saved):
            self.assertNotIn("Reading Suryodaya", text)
            self.assertNotIn("Reading Keystone", text)


class Story2MailNoMailbox(ScenarioCase):
    def test_story2_scenario2_no_mailbox_keeps_meetings_and_tasks(self):
        """P-B-10: 'no mailbox' line, Meetings and Tasks still shown, no model call."""
        transport, _, _ = fakes.default_world()  # no mailbox on either system
        r = self.run_briefing(transport)
        self.assertEqual(r.code, 0, r.err)
        sy = section(r.out, "Suryodaya")
        self.assertIn(NO_MAILBOX, part(sy, "Mail"))
        self.assertIn("Daily standup", part(sy, "Meetings"))
        self.assertIn("Write seat report", part(sy, "Tasks"))
        self.assertRegex(status_of(status_line(r.out), "Suryodaya", "Keystone"), r"Mail missing")
        for call in transport.calls:
            self.assertNotIn("chat/completions", call.url)
        self.assertEqual(transport.tool_names() & {"EmailMessage.list", "EmailMessage.get"}, set())


def mail_part(r, system):
    return part(section(r.out, system), "Mail")


def rows_by_id(*ids):
    """Rows of tests/fixtures/messages.json by id, both systems."""
    data = fakes.load_fixture("messages.json")
    every = {row["id"]: row for system in ("suryodaya", "keystone") for row in data[system]}
    return [every[i] for i in ids]


NON_COMMITTAL = "do not agree to any date"


class Story2MailNeedingAnAnswer(ScenarioCase):
    def test_story2_scenario1_happy_path_summary_reply_and_signature(self):
        """P-B-30: sender, subject, time in IST, summary, greeting + body + the login name as signature."""
        transport, _, _, model = self.mail_world(rows_by_id("s-question"))
        r = self.run_briefing(transport, env=self.mail_env())
        self.assertEqual(r.code, 0, r.err)
        mail = mail_part(r, "Suryodaya")
        for needle in ("priya@example.test", "Where is the seat report?", "14:30 IST",
                       "Asks a question and wants an answer.", "Hello,",
                       "Thanks for your message. I will get back to you soon.", "Team Twenty-Eight"):
            self.assertIn(needle, mail)
        self.assertNotIn("No draft for this message", mail)
        self.assertNotIn("needs your decision", mail)
        self.assertRegex(status_of(status_line(r.out), "Suryodaya", "Keystone"), r"Mail complete")
        self.assertEqual(len(model.requests), 1)
        self.assertEqual(model.calls[0].url, MODEL_URL + "/chat/completions")
        self.assertEqual(model.requests[0]["model"], MODEL_NAME)
        self.assertNotIn("Authorization", model.calls[0].headers)
        self.assertIn("Where is the seat report?", prompt_text(model.requests[0]))
        self.assertIn("Mail", self.briefing_file().read_text(encoding="utf-8"))

    def test_story2_signature_is_brief_signature_then_login_name_then_whoami_name(self):
        """P-B-30: BRIEFING_SIGNATURE, else the name given at login (per system), else /api/auth/me."""
        login_dir = self.home / ".config" / "agentswitch"
        login_dir.mkdir(parents=True)
        (login_dir / "suryodaya.json").write_text(json.dumps({"token": "unused", "signature": "Login Name"}))
        transport, _, _, _ = self.mail_world(rows_by_id("s-question"), rows_by_id("k-question"))
        r = self.run_briefing(transport, env=self.mail_env())
        self.assertIn("Login Name", mail_part(r, "Suryodaya"))
        self.assertNotIn("Team Twenty-Eight", mail_part(r, "Suryodaya"))
        self.assertIn("Team Twenty-Eight", mail_part(r, "Keystone"))
        self.assertNotIn("Login Name", mail_part(r, "Keystone"))
        transport, _, _, _ = self.mail_world(rows_by_id("s-question"), rows_by_id("k-question"))
        r = self.run_briefing(transport, env=self.mail_env(BRIEFING_SIGNATURE="Sandip J"))
        for system in ("Suryodaya", "Keystone"):
            self.assertIn("Sandip J", mail_part(r, system))
            self.assertNotIn("Login Name", mail_part(r, system))
            self.assertNotIn("Team Twenty-Eight", mail_part(r, system))

    def test_story2_entries_are_oldest_first_across_a_system(self):
        """P-B-30: oldest received first."""
        transport, _, _, _ = self.mail_world(rows_by_id("s-question", "s-commit"))
        r = self.run_briefing(transport, env=self.mail_env())
        mail = mail_part(r, "Suryodaya")
        self.assertLess(mail.index("Please confirm Friday's meeting"), mail.index("Where is the seat report?"))

    def test_story2_scenario3_mail_expecting_no_reply_is_skipped_with_reasons(self):
        """P-B-27, P-B-47: no-reply, automated, newsletter and own mail: one line each, no model call."""
        transport, _, _, model = self.mail_world(rows_by_id("s-noreply", "s-auto", "s-news", "s-own"))
        r = self.run_briefing(transport, env=self.mail_env())
        self.assertEqual(r.code, 0, r.err)
        mail = mail_part(r, "Suryodaya")
        self.assertIn("Skipped — no reply expected", mail)
        for subject, reason in (("Your receipt", "no-reply"), ("Build 42 failed", "automated"),
                                ("This week in AI", "newsletter"), ("Note to self", "sent by you")):
            line = line_with(mail, subject)
            self.assertIsNotNone(line, subject)
            self.assertIn(reason, line.lower())
        self.assertIn("No mail needs an answer", mail)
        self.assertNotIn("No draft for this message", mail)
        self.assertEqual(model.requests, [])
        self.assertRegex(status_of(status_line(r.out), "Suryodaya", "Keystone"), r"Mail complete")

    def test_story2_no_unread_mail_says_so_and_has_no_skipped_list(self):
        """P-B-47: nothing unread: 'No mail needs an answer', no 'Skipped' heading."""
        transport, _, _, model = self.mail_world()
        r = self.run_briefing(transport, env=self.mail_env())
        for system in ("Suryodaya", "Keystone"):
            self.assertIn("No mail needs an answer", mail_part(r, system))
            self.assertNotIn("Skipped", mail_part(r, system))
        self.assertEqual(model.requests, [])

    def test_story2_scenario4_commitment_is_redrafted_and_marked(self):
        """P-B-32: the model says no decision, the keyword rule says yes: marked, second request, new reply used."""
        def responder(request):
            if NON_COMMITTAL in prompt_text(request).lower():
                return model_reply(needs_decision=True, body="Thanks. I will confirm the date, amount and "
                                                             "attendance shortly.")
            return model_reply(needs_decision=False, body="Yes, I will attend on Friday and pay the invoice.")
        transport, _, _, model = self.mail_world(rows_by_id("s-commit"), responder=responder)
        r = self.run_briefing(transport, env=self.mail_env())
        mail = mail_part(r, "Suryodaya")
        self.assertIn("needs your decision", mail)
        self.assertIn("I will confirm the date, amount and attendance shortly.", mail)
        self.assertNotIn("I will attend", mail)
        self.assertEqual(len(model.requests), 2)
        self.assertNotIn(NON_COMMITTAL, prompt_text(model.requests[0]).lower())
        self.assertIn(NON_COMMITTAL, prompt_text(model.requests[1]).lower())

    def test_story2_scenario5_answered_read_and_draft_rows_are_not_listed(self):
        """P-B-26: an answered, a read and a draft row never appear; the real question does."""
        transport, _, _, model = self.mail_world(rows_by_id("s-question", "s-answered", "s-read", "s-draftrow"))
        r = self.run_briefing(transport, env=self.mail_env())
        mail = mail_part(r, "Suryodaya")
        self.assertIn("Where is the seat report?", mail)
        for subject in ("Already replied to", "Already read", "Unsent thoughts"):
            self.assertNotIn(subject, r.out)
            self.assertNotIn(subject, self.briefing_file().read_text(encoding="utf-8"))
        self.assertEqual(len(model.requests), 1)

    def test_story2_a_message_with_a_saved_draft_shows_it_and_gets_no_proposal(self):
        """P-B-25: my draft with in_reply_to = message_id: 'draft already saved' + its text, no model call."""
        saved = message_row("s-saved-draft", "Re: Where is the seat report?", "team28c@example.test",
                            "2026-11-05T10:00:00Z", folder="drafts", is_read=True, is_draft=True,
                            in_reply_to="<s-question@mail.test>", created_by="u-100",
                            body="Hi Priya, the report is in the shared folder.")
        transport, _, _, model = self.mail_world(rows_by_id("s-question") + [saved])
        r = self.run_briefing(transport, env=self.mail_env())
        mail = mail_part(r, "Suryodaya")
        self.assertIn("draft already saved", mail)
        self.assertIn("Hi Priya, the report is in the shared folder.", mail)
        self.assertNotIn("No draft for this message", mail)
        self.assertEqual(model.requests, [])
        self.assertRegex(status_of(status_line(r.out), "Suryodaya", "Keystone"), r"Mail complete")

    def test_story2_a_message_that_cannot_be_opened_is_listed_and_the_rest_is_drafted(self):
        """P-B-28: 'couldn't be opened' line, no draft for it, other messages drafted, Mail still complete."""
        transport, surya, _, model = self.mail_world(rows_by_id("s-question", "s-commit"))
        surya.fail_when("EmailMessage.get", lambda a: a.get("id") == "s-question", ToolError("boom"))
        r = self.run_briefing(transport, env=self.mail_env())
        mail = mail_part(r, "Suryodaya")
        self.assertIn("1 message couldn't be opened (subject: Where is the seat report?) — skipped; "
                      "open it in the mailbox, and report it if it repeats", mail)
        self.assertIn("Please confirm Friday's meeting", mail)
        self.assertRegex(status_of(status_line(r.out), "Suryodaya", "Keystone"), r"Mail complete")
        for request in model.requests:
            self.assertNotIn("Where is the seat report?", prompt_text(request))

    def test_story2_drafting_progress_goes_to_stderr_only(self):
        """P-B-35: 'Drafting replies (n of m)…' on stderr, never in stdout or the file."""
        transport, _, _, _ = self.mail_world(rows_by_id("s-question", "s-commit"), rows_by_id("k-question"))
        r = self.run_briefing(transport, env=self.mail_env())
        self.assertRegex(r.err, r"Drafting replies \(\d+ of \d+\)…")
        self.assertNotIn("Drafting replies", r.out)
        self.assertNotIn("Drafting replies", self.briefing_file().read_text(encoding="utf-8"))

    def test_story2_model_outage_keeps_every_message_with_its_first_line(self):
        """P-B-34: connection refused: '(model unavailable)', first line shown, exit 0, the run goes on."""
        transport, _, _, model = self.mail_world(rows_by_id("s-question", "s-commit"), rows_by_id("k-question"))
        model.down()
        path = Path(self._tmp.name) / "result.json"
        r = self.run_briefing(transport, ["--json", str(path)], env=self.mail_env())
        self.assertEqual(r.code, 0, r.err)
        sy = mail_part(r, "Suryodaya")
        self.assertEqual(sy.count("No draft for this message (model unavailable) — reply by hand"), 2)
        self.assertEqual(sy.count("(no summary — first line shown)"), 2)
        self.assertIn("Hi, could you tell me where the seat report lives?", sy)
        self.assertIn("No draft for this message (model unavailable) — reply by hand", mail_part(r, "Keystone"))
        # G2-AC3: the keyword mark is the code's, not the model's: s-commit asks about a meeting and an invoice
        self.assertEqual(sy.count("needs your decision"), 1)
        (commit_line,) = [l for l in sy.splitlines() if "Please confirm Friday's meeting" in l]
        self.assertIn("needs your decision", commit_line)
        self.assertNotIn("needs your decision", mail_part(r, "Keystone"))
        self.assertIn("Meetings", r.out)
        self.assertRegex(status_line(r.out), r"Mail complete")
        data = json.loads(path.read_text(encoding="utf-8"))
        statuses = [m["draft_status"] for s in data["systems"].values() for m in s["mail"]["needs_answer"]]
        self.assertEqual(statuses, ["model_unavailable"] * 3)
        self.assertTrue(data["model"]["unreachable"])
        self.assertTrue(1 <= data["model"]["calls"] <= 3, data["model"])
        self.assertGreaterEqual(data["model"]["failures"], 1)

    def test_story2_json_holds_the_mail_fields_and_the_model_block(self):
        """P-B-37: ids per list, each message's draft status, and calls / failures / unreachable."""
        long_body = " ".join(["word"] * 130)

        def responder(request):
            text = prompt_text(request)
            if "Long reply please" in text:
                return model_reply(body=long_body)
            if "Please confirm Friday's meeting" in text:
                if NON_COMMITTAL in text.lower():
                    return model_reply(greeting="", needs_decision=True)
                return model_reply(needs_decision=False, body="Yes, I will attend.")
            return model_reply()

        extra = [message_row("s-long", "Long reply please", "long@example.test", "2026-11-05T06:00:00Z"),
                 message_row("s-broken", "Cannot open this", "broken@example.test", "2026-11-05T06:30:00Z"),
                 message_row("s-saved", "Has a draft", "sam@example.test", "2026-11-05T06:40:00Z",
                             message_id="<s-saved@mail.test>"),
                 message_row("s-saved-draft", "Re: Has a draft", "team28c@example.test", "2026-11-05T06:50:00Z",
                             folder="drafts", is_read=True, is_draft=True, in_reply_to="<s-saved@mail.test>",
                             created_by="u-100", body="Draft body")]
        transport, surya, _, model = self.mail_world(
            rows_by_id("s-question", "s-commit", "s-noreply", "s-answered") + extra, responder=responder)
        surya.fail_when("EmailMessage.get", lambda a: a.get("id") == "s-broken", ToolError("boom"))
        path = Path(self._tmp.name) / "result.json"
        r = self.run_briefing(transport, ["--json", str(path)], env=self.mail_env())
        self.assertEqual(r.code, 0, r.err)
        data = json.loads(path.read_text(encoding="utf-8"))
        mail = data["systems"]["suryodaya"]["mail"]
        self.assertEqual(mail["complete"], True)
        self.assertEqual([(m["id"], m["draft_status"]) for m in mail["needs_answer"]],
                         [("s-commit", "needs_decision_redraft_failed"), ("s-long", "invalid"),
                          ("s-question", "proposed")])
        self.assertEqual([m["id"] for m in mail["skipped"]], ["s-noreply"])
        self.assertIn("no-reply", mail["skipped"][0]["reason"].lower())
        self.assertEqual(mail["unopened"], ["s-broken"])
        self.assertEqual(mail["draft_saved"], ["s-saved"])
        keystone_mail = data["systems"]["keystone"]["mail"]
        self.assertEqual((keystone_mail["complete"], keystone_mail["needs_answer"]), (True, []))
        self.assertEqual(data["model"], {"calls": 4, "failures": 0, "unreachable": False})
        self.assertEqual(len(model.requests), 4)

    def test_story2_a_key_planted_in_mail_is_redacted_everywhere_and_the_file_is_saved(self):
        """P-B-48: the model API key or a platform token in a subject, body or model summary shows as ***."""
        row = message_row("s-planted", "Token %s please" % TOKEN_SURYODAYA, "eve@example.test",
                          "2026-11-05T09:00:00Z", body="Use key %s to log in" % MODEL_KEY)
        transport, _, _, model = self.mail_world(
            [row], responder=lambda request: model_reply(summary="Asks about key %s" % MODEL_KEY,
                                                          body="Noted %s." % MODEL_KEY))
        path = Path(self._tmp.name) / "result.json"
        env = self.env(OPENAI_BASE_URL=MODEL_URL, OPENAI_MODEL=MODEL_NAME, OPENAI_API_KEY=MODEL_KEY)
        r = self.run_briefing(transport, ["--json", str(path)], env=env)
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(model.calls[0].headers.get("Authorization"), "Bearer " + MODEL_KEY)
        saved = self.briefing_file().read_text(encoding="utf-8")
        shown = json.dumps(json.loads(path.read_text(encoding="utf-8")))
        for text in (r.out, saved, shown, r.err):
            self.assertNotIn(MODEL_KEY, text)
            self.assertNotIn(TOKEN_SURYODAYA, text)
        for text in (r.out, saved):
            self.assertIn("***", text)
            self.assertIn("eve@example.test", text)


    def test_story2_an_ignored_openai_key_planted_in_mail_is_still_redacted(self):
        """G5-AC6: OPENAI_API_KEY is ignored without OPENAI_BASE_URL, yet it is still a secret to hide."""
        row = message_row("s-planted", "Key %s please" % MODEL_KEY, "eve@example.test", "2026-11-05T09:00:00Z")
        transport, _, _, model = self.mail_world([row])
        path = Path(self._tmp.name) / "result.json"
        r = self.run_briefing(transport, ["--json", str(path)], env=self.mail_env(OPENAI_API_KEY=MODEL_KEY))
        self.assertEqual(r.code, 0, r.err)
        self.assertNotIn("Authorization", model.calls[0].headers)   # ignored, as documented
        saved = self.briefing_file().read_text(encoding="utf-8")
        for text in (r.out, saved, path.read_text(encoding="utf-8"), r.err):
            self.assertNotIn(MODEL_KEY, text)
        for text in (r.out, saved):
            self.assertIn("Key *** please", text)


class Story2ModelSettings(ScenarioCase):
    """P-B-36 end to end: which endpoint, model and key a run uses."""

    HARNESS_BASE = "https://harness.example.test"
    DEFAULT_GLC = "http://127.0.0.1:8111"

    def world(self):
        transport = fakes.FakeTransport()
        server = transport.server(self.HARNESS_BASE, "harness", "harness-token-777",
                                  fakes.load_fixture("me_keystone.json"))
        server.rows("Mailbox.list", [fakes.MAILBOX_ROW])
        server.rows("EmailAccount.list", [fakes.MAILBOX_ROW])
        serve_mail(server, rows_by_id("k-question"))
        server.rows("CalendarEvent.list", [])
        server.rows("Task.list", [])
        model = transport.add_server(MODEL_BASE, FakeModelServer())
        return transport, model

    def harness_env(self, **extra):
        env = {"HOME": str(self.home), "BRIEFING_DIR": str(self.briefings),
               "AGENTSWITCH_TOKEN": "harness-token-777", "AGENTSWITCH_INSTANCE": "keystone",
               "AGENTSWITCH_BASE_URL": self.HARNESS_BASE}
        env.update(extra)
        return env

    def test_openai_variables_in_harness_mode_set_endpoint_model_and_key(self):
        transport, model = self.world()
        transport.add_server("https://other.example.test", FakeModelServer())  # must stay untouched
        env = self.harness_env(OPENAI_BASE_URL=MODEL_URL + "/", OPENAI_MODEL="harness-model",
                               OPENAI_API_KEY=MODEL_KEY, BRIEFING_LLM_URL="https://other.example.test/v1",
                               BRIEFING_LLM_MODEL="ignored-model")
        r = self.run_briefing(transport, env=env)
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(len(model.calls), 1)
        self.assertEqual(model.calls[0].url, MODEL_URL + "/chat/completions")
        self.assertEqual(model.requests[0]["model"], "harness-model")
        self.assertEqual(model.calls[0].headers.get("Authorization"), "Bearer " + MODEL_KEY)
        self.assertEqual(transport.calls_to("https://other.example.test"), [])
        self.assertIn("Demo notes", mail_part_of(r.out))
        self.assertNotIn(MODEL_KEY, r.out + r.err)

    def test_openai_base_url_without_a_model_exits_two_before_any_call(self):
        transport, model = self.world()
        r = self.run_briefing(transport, env=self.harness_env(OPENAI_BASE_URL=MODEL_URL))
        self.assertEqual(r.code, 2)
        self.assertEqual(len(r.err.strip().splitlines()), 1, r.err)
        self.assertIn("OPENAI_MODEL", r.err)
        self.assertEqual(transport.calls, [])
        self.assertEqual(list(self.briefings.iterdir()), [])

    def test_without_openai_base_url_the_briefing_settings_apply_and_other_openai_variables_are_ignored(self):
        transport, model = self.world()
        env = self.harness_env(BRIEFING_LLM_URL=MODEL_URL, BRIEFING_LLM_MODEL="glc-model",
                               OPENAI_MODEL="not-used", OPENAI_API_KEY=MODEL_KEY)
        r = self.run_briefing(transport, env=env)
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(model.requests[0]["model"], "glc-model")
        self.assertNotIn("Authorization", model.calls[0].headers)

    def test_the_default_gateway_is_glc_on_localhost(self):
        transport, _ = self.world()
        glc = transport.add_server(self.DEFAULT_GLC, FakeModelServer())
        r = self.run_briefing(transport, env=self.harness_env(BRIEFING_LLM_MODEL="glc-model"))
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(glc.calls[0].url, self.DEFAULT_GLC + "/v1/chat/completions")

    def test_an_unset_briefing_model_runs_without_drafts_and_says_so(self):
        transport, model = self.world()
        r = self.run_briefing(transport, env=self.harness_env())
        self.assertEqual(r.code, 0, r.err)
        self.assertIn("BRIEFING_LLM_MODEL not set — no drafts this run", r.err)
        self.assertEqual(model.requests, [])
        self.assertEqual([c for c in transport.calls if "chat/completions" in c.url], [])
        mail = mail_part_of(r.out)
        self.assertIn("No draft for this message (model unavailable) — reply by hand", mail)
        self.assertIn("Demo notes", mail)
        self.assertIn("Could you send the demo notes", mail)


def mail_part_of(text):
    """The Mail part of the (only) system section in `text`."""
    lines, grab, found = text.splitlines(), False, []
    for line in lines:
        if line.startswith("#"):
            grab = line == "### Mail"
            continue
        if line.startswith("Status:"):
            grab = False
        if grab:
            found.append(line)
    return "\n".join(found)


class Story3Meetings(ScenarioCase):
    def test_story3_scenario1_all_day_first_then_by_start_in_ist(self):
        """P-B-12, P-B-13: order, IST labels, organiser, and an all-day event clashes with nothing."""
        transport, surya, _ = fakes.default_world()
        surya.rows("CalendarEvent.list", [
            event_row("e3", "Retro", "2026-11-06T09:30:00Z", "2026-11-06T10:30:00Z", organizer="pm@example.test"),
            event_row("e1", "Company holiday", "2026-11-06", "2026-11-06", all_day=True, organizer=""),
            event_row("e2", "Planning", "2026-11-06T03:30:00Z", "2026-11-06T04:30:00Z", organizer="boss@example.test"),
        ])
        r = self.run_briefing(transport)
        meetings = part(section(r.out, "Suryodaya"), "Meetings")
        self.assertLess(meetings.index("Company holiday"), meetings.index("Planning"))
        self.assertLess(meetings.index("Planning"), meetings.index("Retro"))
        planning = line_with(meetings, "Planning")
        self.assertIn("09:00–10:00 IST", planning)
        self.assertIn("boss@example.test", planning)
        self.assertIn("15:00–16:00 IST", line_with(meetings, "Retro"))
        self.assertIn("all day", line_with(meetings, "Company holiday"))
        self.assertIn("—", line_with(meetings, "Company holiday"))
        self.assertNotIn("clash", meetings)

    def test_story3_scenario2_calendar_cannot_be_read(self):
        """P-B-4: only Keystone's CalendarEvent.list fails; the other parts are shown."""
        transport, _, keystone = fakes.default_world(mailbox=True)
        keystone.fail("CalendarEvent.list", ToolError("calendar service down"))
        r = self.run_briefing(transport)
        self.assertEqual(r.code, 0, r.err)
        ks = section(r.out, "Keystone")
        self.assertIn("Keystone meetings: couldn't be read (reason: calendar service down)"
                      " — report it if it repeats", part(ks, "Meetings"))
        self.assertIn("Prepare demo", part(ks, "Tasks"))
        self.assertNotEqual(part(ks, "Mail").strip(), "")
        self.assertRegex(status_of(status_line(r.out), "Keystone", "Suryodaya"), r"Meetings missing.*Tasks complete")

    def test_story3_scenario3_each_system_has_its_own_day_and_zone(self):
        """P-B-11: a 22:00 Eastern meeting today (already tomorrow in IST) is Keystone's today."""
        transport, _, keystone = fakes.default_world()
        keystone.rows("CalendarEvent.list", [
            event_row("k1", "Late call", "2026-11-07T03:00:00Z", "2026-11-07T03:30:00Z"),      # 22:00 EST on 6 Nov
            event_row("k2", "Tomorrow call", "2026-11-07T14:00:00Z", "2026-11-07T14:30:00Z"),  # 09:00 EST on 7 Nov
        ])
        r = self.run_briefing(transport)
        ks_meetings = part(section(r.out, "Keystone"), "Meetings")
        self.assertIn("22:00–22:30 EST", line_with(ks_meetings, "Late call"))
        self.assertNotIn("Tomorrow call", ks_meetings)
        self.assertIn("09:00–09:30 IST", part(section(r.out, "Suryodaya"), "Meetings"))

    def test_story3_scenario4_clash_declined_cancelled(self):
        """P-B-13: overlapping meetings both 'clash' (the declined one too); cancelled hidden."""
        transport, surya, _ = fakes.default_world()
        surya.rows("CalendarEvent.list", [
            event_row("a", "Budget talk", "2026-11-06T04:00:00Z", "2026-11-06T05:00:00Z"),
            event_row("b", "Vendor call", "2026-11-06T04:30:00Z", "2026-11-06T05:30:00Z",
                      attendees=[attendee(ME_EMAIL, "declined")]),
            event_row("c", "Quiet hour", "2026-11-06T08:00:00Z", "2026-11-06T09:00:00Z"),
            event_row("d", "Called off", "2026-11-06T04:10:00Z", "2026-11-06T04:40:00Z", status="cancelled"),
        ])
        r = self.run_briefing(transport)
        meetings = part(section(r.out, "Suryodaya"), "Meetings")
        budget, vendor = line_with(meetings, "Budget talk"), line_with(meetings, "Vendor call")
        self.assertIn("clash", budget)
        self.assertNotIn("declined", budget)
        self.assertIn("clash", vendor)
        self.assertIn("declined", vendor)
        self.assertNotIn("clash", line_with(meetings, "Quiet hour"))
        self.assertNotIn("Called off", meetings)

    def test_story3_scenario5_time_zone_override_for_one_system(self):
        """P-B-11: --tz suryodaya=America/New_York changes that system's day and labels only."""
        transport, surya, _ = fakes.default_world()
        surya.rows("CalendarEvent.list", [
            event_row("m", "Morning IST", "2026-11-06T03:30:00Z", "2026-11-06T04:00:00Z"),    # 22:30 EST on 5 Nov
            event_row("p", "Afternoon IST", "2026-11-06T09:30:00Z", "2026-11-06T10:00:00Z"),  # 04:30 EST on 6 Nov
            # offset-less, no timezone: the platform's own wall-clock (IST), never the display zone
            event_row("nm", "Naive morning", "2026-11-06T10:00:00", "2026-11-06T10:30:00"),   # 23:30 EST on 5 Nov
            event_row("ne", "Naive evening", "2026-11-06T17:00:00", "2026-11-06T17:30:00"),   # 06:30 EST on 6 Nov
        ])
        r = self.run_briefing(transport, ["--tz", "suryodaya=America/New_York"])
        self.assertEqual(r.code, 0, r.err)
        meetings = part(section(r.out, "Suryodaya"), "Meetings")
        self.assertNotIn("Morning IST", meetings)
        self.assertIn("04:30–05:00 EST", line_with(meetings, "Afternoon IST"))
        self.assertNotIn(" IST", meetings.replace("Afternoon IST", ""))
        self.assertNotIn("Naive morning", meetings)
        self.assertIn("06:30–07:00 EST", line_with(meetings, "Naive evening"))

    def test_story3_unknown_zone_falls_back_and_says_so(self):
        """P-B-11: an unknown zone uses the default and shows the PRD's message."""
        transport, _, _ = fakes.default_world()
        r = self.run_briefing(transport, ["--tz", "suryodaya=Mars/Olympus"])
        self.assertEqual(r.code, 0, r.err)
        self.assertIn("Unknown time zone 'Mars/Olympus' — using IST for Suryodaya", r.out + r.err)
        self.assertIn("09:00–09:30 IST", part(section(r.out, "Suryodaya"), "Meetings"))

    def test_story3_server_that_ignores_date_filters_gives_the_same_list(self):
        """P-B-14: the client-side overlap check decides, not the server filter."""
        events = fakes.load_fixture("events.json")["suryodaya"] + [
            event_row("far", "Next week", "2026-11-13T04:00:00Z", "2026-11-13T05:00:00Z"),
            event_row("old", "Last week", "2026-10-30T04:00:00Z", "2026-10-30T05:00:00Z"),
        ]
        transport, surya, _ = fakes.default_world()
        surya.rows("CalendarEvent.list", events, filters=False)
        r = self.run_briefing(transport)
        meetings = part(section(r.out, "Suryodaya"), "Meetings")
        self.assertIn("Daily standup", meetings)
        self.assertIn("Design review", meetings)
        self.assertNotIn("Next week", meetings)
        self.assertNotIn("Last week", meetings)


class Story4OwnOverdueTasks(ScenarioCase):
    ME = {"created_by": "u-100", "assignee_id": "e-100"}

    def test_story4_scenario1_own_tasks_most_overdue_first(self):
        """P-B-15: due 1, 5 and 10 days ago, listed most overdue first with days overdue."""
        transport, surya, _ = fakes.default_world()
        surya.rows("Task.list", [
            task_row("t1", "One day late", "2026-11-05", **self.ME),
            task_row("t5", "Five days late", "2026-11-01", **self.ME),
            task_row("t10", "Ten days late", "2026-10-27", created_by="u-900", assignee_id="u-100"),
        ])
        r = self.run_briefing(transport)
        tasks = part(section(r.out, "Suryodaya"), "Tasks")
        self.assertLess(tasks.index("Ten days late"), tasks.index("Five days late"))
        self.assertLess(tasks.index("Five days late"), tasks.index("One day late"))
        ten = line_with(tasks, "Ten days late")
        self.assertIn("2026-10-27", ten)
        self.assertIn("10 days overdue", ten)
        self.assertIn("5 days overdue", line_with(tasks, "Five days late"))
        self.assertIn("1 day overdue", line_with(tasks, "One day late"))

    def test_story4_scenario2_tasks_cannot_be_read(self):
        """P-B-4 for tasks: only Task.list fails on Suryodaya; the other parts are shown."""
        transport, surya, _ = fakes.default_world(mailbox=True)
        surya.fail("Task.list", ToolError("tasks service down"))
        r = self.run_briefing(transport)
        sy = section(r.out, "Suryodaya")
        self.assertIn("Suryodaya tasks: couldn't be read (reason: tasks service down)"
                      " — report it if it repeats", part(sy, "Tasks"))
        self.assertIn("Daily standup", part(sy, "Meetings"))
        self.assertNotEqual(part(sy, "Mail").strip(), "")

    def test_story4_scenario3_other_peoples_overdue_tasks_are_counted_not_listed(self):
        """P-B-16: 63 other overdue tasks and none of the user's."""
        transport, _, keystone = fakes.default_world()
        keystone.rows("Task.list", [task_row("o%d" % i, "Other chore %d" % i, "2026-10-%02d" % (1 + i % 28))
                                    for i in range(63)])
        r = self.run_briefing(transport)
        tasks = part(section(r.out, "Keystone"), "Tasks")
        self.assertIn("No overdue tasks of yours", tasks)
        self.assertIn("63 other overdue tasks are visible to this login", tasks)
        self.assertNotIn("Other chore", tasks)
        self.assertRegex(status_of(status_line(r.out), "Keystone", "Suryodaya"), r"Tasks complete")

    def test_story4_scenario4_due_today_done_and_undated_are_not_overdue(self):
        """P-B-15: boundary cases are not listed."""
        transport, surya, _ = fakes.default_world()
        surya.rows("Task.list", [
            task_row("a", "Due today", "2026-11-06", **self.ME),
            task_row("b", "Finished last week", "2026-10-30", status="done", **self.ME),
            task_row("c", "No date", None, **self.ME),
        ])
        r = self.run_briefing(transport)
        tasks = part(section(r.out, "Suryodaya"), "Tasks")
        self.assertIn("No overdue tasks of yours", tasks)
        for title in ("Due today", "Finished last week", "No date"):
            self.assertNotIn(title, tasks)

    def test_story4_others_count_unavailable_keeps_tasks_complete(self):
        """P-B-17: only the others read fails; own list shown, the PRD-design line, Tasks complete."""
        transport, surya, _ = fakes.default_world()
        surya.rows("Task.list", [task_row("t1", "My late task", "2026-11-05", **self.ME)])
        surya.fail_when("Task.list", lambda a: not any(k in a for k in ("created_by", "assignee_id")),
                        ToolError("others read broke"))
        r = self.run_briefing(transport)
        tasks = part(section(r.out, "Suryodaya"), "Tasks")
        self.assertIn("My late task", tasks)
        self.assertIn("Count of other overdue tasks unavailable — your own list above is complete", tasks)
        self.assertRegex(status_of(status_line(r.out), "Suryodaya", "Keystone"), r"Tasks complete")


class JsonAndExitCodes(ScenarioCase):
    def test_json_result_holds_slice_one_parts(self):
        """P-B-22: complete/missing per part, event marks, own overdue tasks, others count."""
        transport, surya, keystone = fakes.default_world()
        surya.rows("CalendarEvent.list", [
            event_row("a", "Budget talk", "2026-11-06T04:00:00Z", "2026-11-06T05:00:00Z"),
            event_row("b", "Vendor call", "2026-11-06T04:30:00Z", "2026-11-06T05:30:00Z",
                      attendees=[attendee(ME_EMAIL, "declined")]),
        ])
        surya.rows("Task.list", [task_row("t1", "Mine", "2026-11-01", created_by="u-100"),
                                 task_row("t2", "Theirs", "2026-11-02")])
        keystone.expire()
        path = Path(self._tmp.name) / "result.json"
        r = self.run_briefing(transport, ["--json", str(path)])
        self.assertEqual(r.code, 0, r.err)
        data = json.loads(path.read_text(encoding="utf-8"))
        sy, ks = data["systems"]["suryodaya"], data["systems"]["keystone"]
        self.assertEqual(sy["meetings"]["complete"], True)
        marks = {e["id"]: (e["clash"], e["declined"]) for e in sy["meetings"]["events"]}
        self.assertEqual(marks, {"a": (True, False), "b": (True, True)})
        self.assertEqual(sy["tasks"]["complete"], True)
        self.assertEqual([(t["id"], t["days_overdue"]) for t in sy["tasks"]["own_overdue"]], [("t1", 5)])
        self.assertEqual(sy["tasks"]["others_count"], 1)
        self.assertEqual(sy["mail"]["complete"], False)
        self.assertIn("no mailbox", sy["mail"]["reason"])
        for name in ("mail", "meetings", "tasks"):
            self.assertEqual(ks[name], {"complete": False, "reason": EXPIRED})

    def test_exit_code_zero_when_the_run_has_gaps(self):
        """P-B-23: gaps in the briefing still exit 0."""
        transport, _, keystone = fakes.default_world()
        keystone.fail("Task.list", ToolError("boom"))
        self.assertEqual(self.run_briefing(transport).code, 0)

    def test_exit_code_two_for_usage_errors(self):
        """P-B-23: unknown system, malformed --tz, unknown option."""
        for argv in (["--system", "nowhere"], ["--tz", "keystone"], ["--tz", "nowhere=UTC"], ["--bogus"]):
            transport, _, _ = fakes.default_world()
            r = self.run_briefing(transport, argv)
            self.assertEqual(r.code, 2, (argv, r.err))
            self.assertEqual(transport.calls, [], argv)
            self.assertEqual(list(self.briefings.iterdir()), [], argv)


class HarnessMode(ScenarioCase):
    BASE = "https://harness.example.test"

    def harness_env(self, **extra):
        env = {"HOME": str(self.home), "BRIEFING_DIR": str(self.briefings),
               "AGENTSWITCH_TOKEN": "harness-token-777", "AGENTSWITCH_INSTANCE": "keystone",
               "AGENTSWITCH_BASE_URL": self.BASE}
        env.update(extra)
        return env

    def test_valid_harness_run_reads_only_that_instance_at_that_url(self):
        """P-B-21: one instance, its URL, the harness token, no login file, a one-system file."""
        login_dir = self.home / ".config" / "agentswitch"
        login_dir.mkdir(parents=True)
        (login_dir / "keystone.json").write_text(json.dumps({"token": "FILE-TOKEN", "signature": "x"}))
        transport, _, _ = fakes.default_world()
        server = transport.server(self.BASE, "harness", "harness-token-777", fakes.load_fixture("me_keystone.json"))
        server.rows("Mailbox.list", [])
        server.rows("EmailAccount.list", [])
        server.rows("CalendarEvent.list", fakes.load_fixture("events.json")["keystone"])
        server.rows("Task.list", fakes.load_fixture("tasks.json")["keystone"])
        r = self.run_briefing(transport, env=self.harness_env())
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(transport.calls_to(SURYODAYA_BASE) + transport.calls_to(KEYSTONE_BASE), [])
        self.assertTrue(transport.calls)
        for call in transport.calls:
            self.assertTrue(call.url.startswith(self.BASE + "/"), call.url)
            self.assertEqual(call.headers.get("Authorization"), "Bearer harness-token-777")
        self.assertNotIn("Suryodaya", r.out)
        self.assertIn("Class sync", r.out)
        self.assertTrue(self.briefing_file(DATE + "-keystone.md").exists())

    def test_invalid_harness_settings_exit_two_with_one_plain_line(self):
        """P-B-21: token or instance without the other two, bad instance, conflicting --system."""
        cases = [
            (self.harness_env(AGENTSWITCH_INSTANCE=""), []),
            (self.harness_env(AGENTSWITCH_TOKEN=""), []),
            (self.harness_env(AGENTSWITCH_BASE_URL=""), []),
            (self.harness_env(AGENTSWITCH_INSTANCE="atlantis"), []),
            (self.harness_env(AGENTSWITCH_BASE_URL="harness.example.test"), []),
            (self.harness_env(), ["--system", "suryodaya"]),
        ]
        for env, argv in cases:
            transport, _, _ = fakes.default_world()
            r = self.run_briefing(transport, argv, env=env)
            self.assertEqual(r.code, 2, (env, argv, r.err))
            self.assertEqual(len(r.err.strip().splitlines()), 1, r.err)
            self.assertEqual(transport.calls, [])
            self.assertEqual(list(self.briefings.iterdir()), [])

    def test_base_url_alone_is_ignored_with_a_warning(self):
        """P-B-21: AGENTSWITCH_BASE_URL alone does not start harness mode."""
        transport, _, _ = fakes.default_world()
        r = self.run_briefing(transport, env=self.env(AGENTSWITCH_BASE_URL=self.BASE))
        self.assertEqual(r.code, 0, r.err)
        self.assertIn("AGENTSWITCH_BASE_URL", r.err)
        self.assertEqual(transport.calls_to(self.BASE), [])
        self.assertTrue(transport.calls_to(SURYODAYA_BASE))


SAVE_PROMPT = ("[s]ave", "[e]dit then save", "[Enter] skip", "[q] stop")


def questions(n, who="keystone"):
    """`n` unread questions, `Question 1` the oldest, each from its own sender."""
    return [message_row("%s-q%d" % (who[0], i), "Question %d" % i, "p%d@example.test" % i,
                        "2026-11-0%dT09:00:00Z" % i, body="Hello, can you tell me about item %d?" % i)
            for i in range(1, n + 1)]


class Story5ApproveDrafts(ScenarioCase):
    """P-B-38 to P-B-45 through `cli.main`: offers on stderr, saves through `EmailMessage.create`."""

    def world(self, n=2, **kw):
        transport, surya, keystone, model = self.mail_world(keystone=questions(n), **kw)
        return transport, keystone, model

    def go(self, transport, stdin, argv=(), editor=None, env=None):
        return self.run_briefing(transport, argv, env=env or self.mail_env(), stdin=stdin, editor=editor)

    @staticmethod
    def creates(transport):
        return transport.tool_calls("EmailMessage.create")

    def test_story5_scenario1_save_then_edit_then_save_makes_two_unsent_drafts(self):
        """P-B-39, P-B-40, P-B-42: two drafts in Keystone's drafts folder, the second with the edited text."""
        transport, keystone, _ = self.world(2)
        editor = FakeEditor("Hello Priya,\n\nI edited this reply.\n\nTeam Twenty-Eight")
        r = self.go(transport, TtyInput("s\ne\n"), editor=editor)
        self.assertEqual(r.code, 0, r.err)
        made = self.creates(transport)
        self.assertEqual(len(made), 2)
        self.assertTrue(all(c.base == KEYSTONE_BASE for c in made))
        first = made[0].arguments
        self.assertEqual({k: first[k] for k in ("folder", "is_draft", "company_id", "mailbox_id", "from_email",
                                                 "to", "subject", "thread_id", "in_reply_to", "references")},
                         {"folder": "drafts", "is_draft": True, "company_id": "co-1", "mailbox_id": "mb-1",
                          "from_email": "team28c@example.test", "to": "p1@example.test",
                          "subject": "Re: Question 1", "thread_id": "th-k-q1",
                          "in_reply_to": "<k-q1@mail.test>", "references": "<k-q1@mail.test>"})
        self.assertTrue(first["body_text"].startswith("Hello,"))
        self.assertIn("Thanks for your message. I will get back to you soon.", first["body_text"])
        self.assertIn("Team Twenty-Eight", first["body_text"])
        second = made[1].arguments
        self.assertEqual(second["subject"], "Re: Question 2")
        self.assertEqual(second["body_text"], "Hello Priya,\n\nI edited this reply.\n\nTeam Twenty-Eight")
        self.assertEqual(len(editor.texts), 1)
        self.assertIn("Thanks for your message", editor.texts[0])   # the editor is given the proposed reply
        self.assertEqual(len(keystone.created), 2)
        # nothing is sent: only reads and the one write
        self.assertLessEqual(transport.tool_names(), fakes.READ_TOOLS | {"EmailMessage.create"})
        for call in transport.tool_calls():
            if call.tool == "EmailMessage.create":
                self.assertIs(call.arguments["is_draft"], True)
                self.assertEqual(call.arguments["folder"], "drafts")
        self.assertEqual(transport.tool_calls("EmailMessage.create", base=SURYODAYA_BASE), [])
        for needle in SAVE_PROMPT + ("Saving draft…", "Saved", "2 saved (1 edited), 0 skipped, 0 not saved"):
            self.assertIn(needle, r.err)
        self.assertEqual(len(re.findall(r"Saving draft…", r.err)), 2)

    def test_story5_each_save_rechecks_the_original_and_reads_the_new_draft_back(self):
        """P-B-40, P-B-41: get of the original before the create, get of the new id after it."""
        transport, keystone, _ = self.world(1)
        self.go(transport, TtyInput("s\n"))
        order = [(c.tool, (c.arguments or {}).get("id")) for c in transport.tool_calls(base=KEYSTONE_BASE)]
        at = order.index(("EmailMessage.create", None))
        self.assertIn(("EmailMessage.get", "k-q1"), order[:at][-2:])   # the re-check, just before
        self.assertEqual(order[at + 1], ("EmailMessage.get", "new-1"))  # the read-back, right after
        self.assertEqual(len(keystone.created), 1)

    def test_story5_scenario2_a_refused_save_shows_the_approved_text_and_goes_on(self):
        """P-B-42: 'Draft not saved: …' with the edited text, kept in the file, the next reply still offered."""
        transport, keystone, _ = self.world(2)
        keystone.fail_when("EmailMessage.create", lambda a: a.get("subject") == "Re: Question 1",
                           ToolError("mailbox is read-only"))
        path = Path(self._tmp.name) / "result.json"
        r = self.go(transport, TtyInput("e\ns\n"), argv=["--json", str(path)],
                    editor=FakeEditor("My edited words\nover two lines"))
        self.assertEqual(r.code, 0, r.err)
        self.assertIn("Draft not saved: mailbox is read-only — your text is shown below and kept in this run's "
                      "briefing file; save it by hand", r.err)
        self.assertIn("My edited words", r.err)
        self.assertEqual(len(re.findall(r"\[s\]ave", r.err)), 2)         # the second was still offered
        self.assertEqual(len(keystone.created), 1)
        self.assertEqual(keystone.created[0]["subject"], "Re: Question 2")
        self.assertIn("1 saved (0 edited), 0 skipped, 1 not saved", r.err)
        saved = self.briefing_file().read_text(encoding="utf-8")
        self.assertIn("## Approval", saved)
        self.assertIn("1 saved (0 edited), 0 skipped, 1 not saved", saved)
        self.assertIn("My edited words", saved)
        self.assertIn("mailbox is read-only", saved)
        failed = json.loads(path.read_text(encoding="utf-8"))["approval"]["failed"]
        self.assertEqual([(f["system"], f["id"], f["subject"]) for f in failed],
                         [("keystone", "k-q1", "Question 1")])
        self.assertIn("mailbox is read-only", failed[0]["reason"])
        self.assertEqual(failed[0]["text"], "My edited words\nover two lines")

    def test_story5_scenario3_enter_and_other_input_skip(self):
        """P-B-39: Enter skips, and so does any other input; nothing is created."""
        transport, keystone, _ = self.world(2)
        r = self.go(transport, TtyInput("\nxyz\n"))
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(self.creates(transport), [])
        self.assertIn("0 saved (0 edited), 2 skipped, 0 not saved", r.err)

    def test_story5_scenario4_stop_leaves_the_rest_not_saved(self):
        """P-B-42: five replies, one saved, stop on the second: 1 saved, 4 not saved, the rest not offered."""
        transport, keystone, _ = self.world(5)
        r = self.go(transport, TtyInput("s\nq\n"))
        self.assertEqual(len(keystone.created), 1)
        self.assertIn("1 saved (0 edited), 0 skipped, 4 not saved", r.err)
        self.assertEqual(len(re.findall(r"\[s\]ave", r.err)), 2)

    def test_story5_scenario5_a_rerun_shows_draft_already_saved_and_proposes_nothing(self):
        """P-B-25 live in the loop: the saved draft is the login's, replies to the message, and is not changed."""
        transport, keystone, model = self.world(1)
        self.go(transport, TtyInput("s\n"))
        self.assertEqual(len(keystone.created), 1)
        asked = len(model.requests)
        r = self.go(transport, TtyInput("s\n"))
        mail = mail_part(r, "Keystone")
        self.assertIn("draft already saved", mail)
        self.assertIn("Thanks for your message. I will get back to you soon.", mail)
        self.assertNotIn("Proposed reply", mail)
        self.assertEqual(len(model.requests), asked)             # no new reply was drafted
        self.assertEqual(len(keystone.created), 1)               # and nothing was saved again
        self.assertIn("No drafts to approve", r.err)

    def test_story5_scenario6_a_message_answered_meanwhile_is_not_saved(self):
        """P-B-41: the re-read finds it answered: 'Already answered — draft not saved', no create."""
        transport, keystone, _ = self.world(1)

        def answered_by_hand(n):
            for row in keystone.mail_rows:
                row["is_answered"] = True

        r = self.go(transport, TtyInput("s\n", before_read=answered_by_hand))
        self.assertIn("Already answered — draft not saved", r.err)
        self.assertEqual(self.creates(transport), [])
        self.assertIn("0 saved (0 edited), 0 skipped, 1 not saved", r.err)

    def test_story5_an_unconfirmed_save_says_to_check_the_drafts_folder_and_is_not_retried(self):
        """P-B-41: the read-back fails: 'Draft may not be saved — check your Drafts folder', one create only."""
        transport, keystone, _ = self.world(1)
        keystone.fail_when("EmailMessage.get", lambda a: str(a.get("id")).startswith("new-"), ToolError("not found"))
        r = self.go(transport, TtyInput("s\n"))
        self.assertIn("Draft may not be saved — check your Drafts folder", r.err)
        self.assertEqual(len(self.creates(transport)), 1)
        self.assertIn("0 saved (0 edited), 0 skipped, 1 not saved", r.err)

    def test_story5_no_proposed_replies_says_so_and_ends(self):
        """P-B-38: nothing to approve; no create, no summary section in the file."""
        transport, surya, keystone, _ = self.mail_world()
        r = self.go(transport, TtyInput("s\n"))
        self.assertEqual(r.code, 0, r.err)
        self.assertIn("No drafts to approve", r.err)
        self.assertNotIn("No drafts to approve", r.out)
        self.assertEqual(self.creates(transport), [])
        self.assertNotIn("## Approval", self.briefing_file().read_text(encoding="utf-8"))

    def test_story5_replies_the_model_could_not_draft_are_not_offered(self):
        """P-B-38: a message shown as 'No draft for this message' has nothing to approve."""
        transport, keystone, _ = self.world(1)
        r = self.go(transport, TtyInput("s\n"), env=self.env())   # no model configured: every draft fails
        self.assertIn("No draft for this message (model unavailable)", mail_part(r, "Keystone"))
        self.assertIn("No drafts to approve", r.err)
        self.assertEqual(self.creates(transport), [])

    def test_story5_stdin_at_end_of_file_skips_everything(self):
        """P-B-44: no input at all: zero creates, '0 saved (0 edited), N skipped, 0 not saved'."""
        transport, keystone, _ = self.world(3)
        r = self.go(transport, io.StringIO(""))
        self.assertEqual(r.code, 0, r.err)
        self.assertEqual(self.creates(transport), [])
        self.assertIn("0 saved (0 edited), 3 skipped, 0 not saved", r.err)

    def test_story5_piped_save_makes_one_create_and_skips_the_rest(self):
        """P-B-44: a piped `s` is a real answer; the input then ends, so the rest is skipped."""
        transport, keystone, _ = self.world(2)
        r = self.go(transport, io.StringIO("s\n"))
        self.assertEqual(len(self.creates(transport)), 1)
        self.assertIn("1 saved (0 edited), 1 skipped, 0 not saved", r.err)

    def test_story5_edit_without_a_terminal_skips_and_opens_no_editor(self):
        """P-B-44: `e` on a stdin that is not a terminal is a skip."""
        transport, keystone, _ = self.world(1)
        editor = FakeEditor("never used")
        r = self.go(transport, io.StringIO("e\n"), editor=editor)
        self.assertEqual(editor.texts, [])
        self.assertEqual(self.creates(transport), [])
        self.assertIn("0 saved (0 edited), 1 skipped, 0 not saved", r.err)

    def test_story5_offers_and_summary_stay_off_stdout(self):
        """P-B-9: stdout holds only the briefing; the file is that briefing plus the Approval section."""
        transport, keystone, _ = self.world(2)
        r = self.go(transport, TtyInput("s\ns\n"))
        for needle in SAVE_PROMPT + ("Saving draft…", "2 saved", "Approval"):
            self.assertNotIn(needle, r.out)
        self.assertTrue(r.out.startswith("# Daily briefing"))
        saved = self.briefing_file().read_text(encoding="utf-8")
        self.assertTrue(saved.startswith(r.out.rstrip("\n")))
        status = status_line(saved)
        self.assertLess(saved.index(status), saved.index("## Approval"))
        self.assertIn("2 saved (0 edited), 0 skipped, 0 not saved", saved)

    def test_story5_json_after_approval_holds_the_summary(self):
        """P-B-45: the --json file adds the approval summary."""
        transport, keystone, _ = self.world(3)
        path = Path(self._tmp.name) / "result.json"
        self.go(transport, TtyInput("s\ne\n"), argv=["--json", str(path)],
                editor=FakeEditor("A different reply\nTeam Twenty-Eight"))
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["approval"], {"saved": 2, "edited": 1, "skipped": 1, "not_saved": 0, "failed": []})
        self.assertIn("systems", data)

    def test_story5_json_without_proposals_still_holds_a_zero_summary(self):
        """P-B-45: the step ran and offered nothing."""
        transport, surya, keystone, _ = self.mail_world()
        path = Path(self._tmp.name) / "result.json"
        self.go(transport, io.StringIO(""), argv=["--json", str(path)])
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual(data["approval"], {"saved": 0, "edited": 0, "skipped": 0, "not_saved": 0, "failed": []})

    def test_story5_nothing_fetched_has_no_approval_step(self):
        """P-B-7 unchanged: nothing fetched means no approval step, no file, exit 1."""
        transport, surya, keystone, _ = self.mail_world(keystone=questions(1))
        surya.down()
        keystone.down()
        path = Path(self._tmp.name) / "result.json"
        r = self.go(transport, TtyInput("s\n"), argv=["--json", str(path)])
        self.assertEqual(r.code, 1)
        self.assertNotIn("No drafts to approve", r.err)
        self.assertFalse(self.briefing_file().exists())
        self.assertNotIn("approval", json.loads(path.read_text(encoding="utf-8")))

    def test_story5_a_token_in_the_approved_text_is_redacted_on_screen_and_in_the_file(self):
        """P-B-19 applied to the approval step: the edit holds a loaded token; screen and file show ***."""
        transport, keystone, _ = self.world(1)
        keystone.fail("EmailMessage.create", ToolError("refused"))
        r = self.go(transport, TtyInput("e\n"), editor=FakeEditor("my token is " + TOKEN_KEYSTONE))
        self.assertNotIn(TOKEN_KEYSTONE, r.err + r.out)
        saved = self.briefing_file().read_text(encoding="utf-8")
        self.assertNotIn(TOKEN_KEYSTONE, saved)
        self.assertIn("my token is ***", saved)


if __name__ == "__main__":
    unittest.main()
