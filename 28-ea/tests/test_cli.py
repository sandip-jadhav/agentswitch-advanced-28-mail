"""cli.py beyond the scenario tests: the login subcommand, the real clock, secrets and file problems end
to end, and the --json file when nothing was fetched (P-B-8, P-B-19, P-B-20, P-B-23)."""
import contextlib
import io
import json
import os
import stat
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest import mock

from ea_briefing import cli
from tests import fakes
from tests.fakes import (KEYSTONE_BASE, TOKEN_KEYSTONE, TOKEN_SURYODAYA, FakeClock, HttpStatus, event_row,
                         task_row)

DATE = "2026-11-06"


class CliCase(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.root = Path(tmp.name)
        self.home = self.root / "home"
        self.home.mkdir()
        self.briefings = self.root / "briefings"
        self.briefings.mkdir()
        self.clock = FakeClock()

    def env(self, **extra):
        env = {"HOME": str(self.home), "BRIEFING_DIR": str(self.briefings),
               "AGENTSWITCH_SURYODAYA_TOKEN": TOKEN_SURYODAYA, "AGENTSWITCH_KEYSTONE_TOKEN": TOKEN_KEYSTONE}
        env.update(extra)
        return env

    def run_cli(self, argv, transport=None, env=None, stdin_text="", **kw):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                code = cli.main(list(argv), env if env is not None else self.env(), io.StringIO(stdin_text),
                                self.clock, transport=transport, **kw)
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()


class Login(CliCase):
    def login_world(self):
        transport, _, keystone = fakes.default_world()
        return transport, keystone

    def test_login_stores_token_and_signature_privately_and_never_the_password(self):
        transport, keystone = self.login_world()
        seen = {}
        keystone.login_handler = lambda body: (seen.update(body), {"token": "new-token-9"})[1]
        asked = []
        code, out, err = self.run_cli(["login", "keystone"], transport, env={"HOME": str(self.home)},
                                      stdin_text="me@example.test\nSandip J\n",
                                      getpass_fn=lambda prompt: asked.append(prompt) or "pw-secret-123")
        self.assertEqual(code, 0, err)
        self.assertEqual(seen, {"email": "me@example.test", "password": "pw-secret-123"})
        self.assertEqual(len(asked), 1)
        path = self.home / ".config" / "agentswitch" / "keystone.json"
        self.assertEqual(json.loads(path.read_text()), {"token": "new-token-9", "signature": "Sandip J"})
        self.assertEqual(stat.S_IMODE(os.stat(path).st_mode), 0o600)
        for text in (out, err, path.read_text()):
            self.assertNotIn("pw-secret-123", text)
            self.assertNotIn("new-token-9", out + err)
        self.assertEqual(transport.calls_to(fakes.SURYODAYA_BASE), [])

    def test_login_works_without_a_run_environment_and_asks_the_signature_once(self):
        transport, keystone = self.login_world()
        first = self.run_cli(["login", "keystone"], transport, env={"HOME": str(self.home)},
                             stdin_text="me@example.test\nSandip J\n", getpass_fn=lambda p: "pw")
        second = self.run_cli(["login", "keystone"], transport, env={"HOME": str(self.home)},
                              stdin_text="me@example.test\n", getpass_fn=lambda p: "pw")
        self.assertEqual((first[0], second[0]), (0, 0))
        path = self.home / ".config" / "agentswitch" / "keystone.json"
        self.assertEqual(json.loads(path.read_text())["signature"], "Sandip J")

    def test_a_rejected_login_exits_one_with_one_line_and_writes_no_file(self):
        transport, keystone = self.login_world()

        def refuse(body):
            raise HttpStatus(401, "bad")
        keystone.login_handler = refuse
        code, out, err = self.run_cli(["login", "keystone"], transport, env={"HOME": str(self.home)},
                                      stdin_text="me@example.test\nSandip J\n", getpass_fn=lambda p: "pw-secret-123")
        self.assertEqual(code, 1)
        self.assertEqual(len(err.strip().splitlines()), 1)
        self.assertNotIn("pw-secret-123", out + err)
        self.assertFalse((self.home / ".config").exists())

    def test_login_with_the_platform_down_says_it_cannot_be_reached(self):
        transport, keystone = self.login_world()
        keystone.down()
        code, _, err = self.run_cli(["login", "keystone"], transport, env={"HOME": str(self.home)},
                                    stdin_text="me@example.test\nSandip J\n", getpass_fn=lambda p: "pw")
        self.assertEqual(code, 1)
        self.assertIn("Keystone: can't be reached right now — try again later", err)

    def test_login_at_end_of_input_stops_with_one_line_and_posts_nothing(self):
        for stdin_text in ("", "\n", "   \n"):
            transport, _ = self.login_world()
            code, out, err = self.run_cli(["login", "keystone"], transport, env={"HOME": str(self.home)},
                                          stdin_text=stdin_text, getpass_fn=lambda p: "pw")
            self.assertEqual(code, 2, repr(stdin_text))
            self.assertEqual(len(err.strip().splitlines()), 1, err)
            self.assertIn("email", err.replace("Email: ", "").lower())
            self.assertEqual(transport.calls, [], repr(stdin_text))
            self.assertFalse((self.home / ".config" / "agentswitch" / "keystone.json").exists())

    def test_login_without_a_signature_on_first_login_stops_with_one_line_and_posts_nothing(self):
        transport, _ = self.login_world()
        code, _, err = self.run_cli(["login", "keystone"], transport, env={"HOME": str(self.home)},
                                    stdin_text="me@example.test\n", getpass_fn=lambda p: "pw")
        self.assertEqual(code, 2)
        self.assertEqual(transport.calls, [])
        self.assertFalse((self.home / ".config" / "agentswitch" / "keystone.json").exists())
        self.assertIn("name", err.lower())

    def test_login_needs_a_known_system(self):
        for argv in (["login"], ["login", "nowhere"], ["login", "keystone", "extra"]):
            transport, _ = self.login_world()
            code, _, err = self.run_cli(argv, transport, env={"HOME": str(self.home)})
            self.assertEqual(code, 2, argv)
            self.assertEqual(transport.calls, [], argv)


class Secrets(CliCase):
    def test_a_token_in_content_is_stars_on_screen_in_markdown_and_in_json_and_the_file_is_saved(self):
        transport, surya, _ = fakes.default_world()
        surya.rows("CalendarEvent.list", [event_row("e1", "Call about " + TOKEN_SURYODAYA,
                                                    "2026-11-06T04:00:00Z", "2026-11-06T05:00:00Z")])
        surya.rows("Task.list", [task_row("t1", "Fix " + TOKEN_KEYSTONE, "2026-11-01", created_by="u-100")])
        path = self.root / "result.json"
        code, out, err = self.run_cli(["--json", str(path)], transport)
        saved = (self.briefings / (DATE + ".md")).read_text(encoding="utf-8")
        self.assertEqual(code, 0, err)
        for text in (out, err, saved, path.read_text(encoding="utf-8")):
            self.assertNotIn(TOKEN_SURYODAYA, text)
            self.assertNotIn(TOKEN_KEYSTONE, text)
        self.assertIn("Call about ***", out)
        self.assertIn("Call about ***", saved)
        self.assertIn("Fix ***", json.dumps(json.loads(path.read_text(encoding="utf-8"))))
        self.assertIn("Fix ***", path.read_text(encoding="utf-8"))

    def test_harness_token_in_content_is_redacted_too(self):
        base = "https://harness.example.test"
        transport, _, _ = fakes.default_world()
        server = transport.server(base, "harness", "harness-token-777", fakes.load_fixture("me_keystone.json"))
        server.rows("Mailbox.list", [])
        server.rows("EmailAccount.list", [])
        server.rows("CalendarEvent.list", [event_row("e", "Leak harness-token-777", "2026-11-06T15:00:00Z",
                                                     "2026-11-06T15:30:00Z")])
        server.rows("Task.list", [])
        env = {"HOME": str(self.home), "BRIEFING_DIR": str(self.briefings), "AGENTSWITCH_TOKEN": "harness-token-777",
               "AGENTSWITCH_INSTANCE": "keystone", "AGENTSWITCH_BASE_URL": base}
        code, out, err = self.run_cli([], transport, env=env)
        self.assertEqual(code, 0, err)
        self.assertNotIn("harness-token-777", out + err)
        self.assertNotIn("harness-token-777", (self.briefings / (DATE + "-keystone.md")).read_text())


class Drafting(CliCase):
    """cli builds the draft writer and the per-system signatures and hands the model counts to the JSON."""

    def world(self):
        transport, surya, keystone = fakes.default_world(mailbox=True, messages={
            "suryodaya": [fakes.message_row("s1", "Question one", "a@example.test", "2026-11-05T09:00:00Z")],
            "keystone": [fakes.message_row("k1", "Question two", "b@example.test", "2026-11-05T10:00:00Z")]})
        model = transport.add_server(fakes.MODEL_BASE, fakes.FakeModelServer())
        return transport, model

    def model_env(self, **extra):
        return self.env(BRIEFING_LLM_URL=fakes.MODEL_URL, BRIEFING_LLM_MODEL=fakes.MODEL_NAME, **extra)

    def test_the_draft_writer_uses_the_model_settings_and_the_same_transport(self):
        transport, model = self.world()
        code, out, err = self.run_cli([], transport, env=self.model_env())
        self.assertEqual(code, 0, err)
        self.assertEqual(len(model.requests), 2)
        self.assertEqual(model.requests[0]["model"], fakes.MODEL_NAME)

    def test_each_system_is_signed_with_its_own_login_name(self):
        login = self.home / ".config" / "agentswitch"
        login.mkdir(parents=True)
        (login / "keystone.json").write_text(json.dumps({"token": "x", "signature": "Keystone Name"}))
        transport, model = self.world()
        code, out, err = self.run_cli([], transport, env=self.model_env())
        self.assertIn("Keystone Name", out)
        self.assertEqual(out.count("Keystone Name"), 1)
        self.assertIn("Team Twenty-Eight", out)

    def test_the_json_model_block_is_there_even_when_no_model_was_called(self):
        transport, _, _ = fakes.default_world()
        path = self.root / "r.json"
        code, out, err = self.run_cli(["--json", str(path)], transport)
        self.assertEqual(json.loads(path.read_text())["model"], {"calls": 0, "failures": 0, "unreachable": False})

    def test_the_api_key_is_in_the_secrets_for_screen_file_and_json(self):
        transport, model = self.world()
        transport.servers[fakes.MODEL_BASE].responder = lambda request: fakes.model_reply(
            summary="Asks about " + fakes.MODEL_KEY)
        path = self.root / "r.json"
        env = self.env(OPENAI_BASE_URL=fakes.MODEL_URL, OPENAI_MODEL="m", OPENAI_API_KEY=fakes.MODEL_KEY)
        code, out, err = self.run_cli(["--json", str(path)], transport, env=env)
        saved = (self.briefings / (DATE + ".md")).read_text(encoding="utf-8")
        for text in (out, err, saved, path.read_text()):
            self.assertNotIn(fakes.MODEL_KEY, text)
        self.assertIn("Asks about ***", out)

    def test_a_missing_model_prints_the_warning_once_and_calls_no_model(self):
        transport, model = self.world()
        code, out, err = self.run_cli([], transport, env=self.env())
        self.assertEqual(code, 0)
        self.assertEqual(err.count("BRIEFING_LLM_MODEL not set — no drafts this run"), 1)
        self.assertEqual(model.requests, [])
        self.assertEqual(out.count("No draft for this message (model unavailable) — reply by hand"), 2)


class FileProblems(CliCase):
    def test_unwritable_briefing_folder_still_prints_the_briefing_and_exits_zero(self):
        blocker = self.root / "blocker"
        blocker.write_text("a file, not a folder")
        transport, _, _ = fakes.default_world()
        code, out, err = self.run_cli([], transport, env=self.env(BRIEFING_DIR=str(blocker / "briefings")))
        self.assertEqual(code, 0, err)
        self.assertIn("## Suryodaya", out)
        self.assertIn("Briefing shown but not saved to file: ", err)
        self.assertIn("copy it from the screen", err)

    def test_nothing_fetched_still_writes_the_json_file_and_exits_one(self):
        transport, surya, keystone = fakes.default_world()
        surya.down()
        keystone.down()
        path = self.root / "result.json"
        code, out, err = self.run_cli(["--json", str(path)], transport)
        self.assertEqual(code, 1)
        data = json.loads(path.read_text(encoding="utf-8"))
        for system in ("suryodaya", "keystone"):
            for part in ("mail", "meetings", "tasks"):
                self.assertFalse(data["systems"][system][part]["complete"])
        self.assertEqual(list(self.briefings.iterdir()), [])

    def test_an_unwritable_json_path_is_reported_and_does_not_change_the_exit_code(self):
        blocker = self.root / "blocker"
        blocker.write_text("file")
        transport, _, _ = fakes.default_world()
        code, out, err = self.run_cli(["--json", str(blocker / "r.json")], transport)
        self.assertEqual(code, 0)
        self.assertIn("JSON result not saved to ", err)


class ApprovalWiring(CliCase):
    """Task 30: the editor parameter, one client per system with proposals, the second save and the --json file."""

    def world(self, keystone_rows=(), surya_rows=()):
        transport, surya, keystone = fakes.default_world(mailbox=True, messages={
            "suryodaya": list(surya_rows), "keystone": list(keystone_rows)})
        transport.add_server(fakes.MODEL_BASE, fakes.FakeModelServer())
        return transport, surya, keystone

    def env_model(self):
        return self.env(BRIEFING_LLM_URL=fakes.MODEL_URL, BRIEFING_LLM_MODEL=fakes.MODEL_NAME)

    @staticmethod
    def rows(prefix, n):
        return [fakes.message_row("%s%d" % (prefix, i), "Question %d" % i, "p%d@example.test" % i,
                                  "2026-11-0%dT09:00:00Z" % i) for i in range(1, n + 1)]

    def go(self, transport, stdin, argv=(), editor=None):
        extra = {"editor": editor} if editor is not None else {}
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv), self.env_model(), stdin, self.clock, transport=transport, **extra)
        return code, out.getvalue(), err.getvalue()

    def test_each_system_is_saved_through_its_own_client_and_token(self):
        transport, surya, keystone = self.world(self.rows("k", 1), self.rows("s", 1))
        code, out, err = self.go(transport, io.StringIO("s\ns\n"))
        self.assertEqual(code, 0, err)
        self.assertEqual((len(surya.created), len(keystone.created)), (1, 1))
        for call in transport.tool_calls("EmailMessage.create"):
            token = TOKEN_SURYODAYA if call.base == fakes.SURYODAYA_BASE else TOKEN_KEYSTONE
            self.assertEqual(call.headers["Authorization"], "Bearer " + token)
        self.assertIn("2 saved (0 edited), 0 skipped, 0 not saved", err)

    def test_suryodaya_is_offered_before_keystone(self):
        transport, surya, keystone = self.world(self.rows("k", 1), self.rows("s", 1))
        code, out, err = self.go(transport, io.StringIO("\n\n"))
        self.assertLess(err.index("Suryodaya"), err.rindex("Keystone"))
        self.assertLess(err.index("Reply 1 of 2 — Suryodaya"), err.index("Reply 2 of 2 — Keystone"))

    def test_a_briefing_file_that_could_not_be_saved_is_not_rewritten_but_approval_still_runs(self):
        blocker = self.root / "blocker"
        blocker.write_text("a file, not a folder")
        transport, surya, keystone = self.world(self.rows("k", 1))
        env = self.env_model()
        env["BRIEFING_DIR"] = str(blocker / "briefings")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main([], env, io.StringIO("s\n"), self.clock, transport=transport)
        self.assertEqual(code, 0, err.getvalue())
        self.assertEqual(err.getvalue().count("Briefing shown but not saved to file: "), 1)
        self.assertEqual(len(keystone.created), 1)

    def test_one_system_run_rewrites_only_its_own_file(self):
        transport, surya, keystone = self.world(self.rows("k", 1))
        full = self.briefings / (DATE + ".md")
        full.write_text("earlier full briefing", encoding="utf-8")
        code, out, err = self.go(transport, io.StringIO("s\n"), argv=["--system", "keystone"])
        self.assertEqual(full.read_text(encoding="utf-8"), "earlier full briefing")
        one = (self.briefings / (DATE + "-keystone.md")).read_text(encoding="utf-8")
        self.assertIn("## Approval", one)
        self.assertIn("1 saved (0 edited), 0 skipped, 0 not saved", one)
        self.assertEqual(transport.calls_to(fakes.SURYODAYA_BASE), [])

    def test_the_json_file_is_written_once_after_the_step_with_the_model_block_too(self):
        transport, surya, keystone = self.world(self.rows("k", 2))
        path = self.root / "r.json"
        self.go(transport, io.StringIO("s\n"), argv=["--json", str(path)])
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((data["approval"]["saved"], data["approval"]["skipped"]), (1, 1))
        self.assertEqual(data["model"]["calls"], 2)

    def test_the_default_editor_is_the_environment_chain_and_a_test_editor_replaces_it(self):
        transport, surya, keystone = self.world(self.rows("k", 1))
        editor = fakes.FakeEditor("Edited by the injected editor")
        self.go(transport, fakes.TtyInput("e\n"), editor=editor)
        self.assertEqual(keystone.created[0]["body_text"], "Edited by the injected editor")
        transport, surya, keystone = self.world(self.rows("k", 1))
        env = self.env_model()
        env["EDITOR"] = "definitely-not-installed-editor"
        with mock.patch("ea_briefing.approval.shutil.which", return_value=None):
            out, err = io.StringIO(), io.StringIO()
            with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
                cli.main([], env, fakes.TtyInput("e\nTyped reply\n.\n"), self.clock, transport=transport)
        self.assertEqual(keystone.created[0]["body_text"], "Typed reply")

    def test_the_first_save_failing_means_the_failure_line_does_not_promise_the_file(self):
        """B8: the file is never rewritten when its first save failed, so the line must not say it keeps the text."""
        blocker = self.root / "blocker"
        blocker.write_text("a file, not a folder")
        transport, surya, keystone = self.world(self.rows("k", 1))
        keystone.fail("EmailMessage.create", fakes.ToolError("refused"))
        env = self.env_model()
        env["BRIEFING_DIR"] = str(blocker / "briefings")
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli.main([], env, io.StringIO("s\n"), self.clock, transport=transport)
        self.assertIn("Draft not saved: refused — your text is shown below; save it by hand", err.getvalue())
        self.assertNotIn("kept in this run's briefing file", err.getvalue())
        transport, surya, keystone = self.world(self.rows("k", 1))
        keystone.fail("EmailMessage.create", fakes.ToolError("refused"))
        code, out, err = self.go(transport, io.StringIO("s\n"))
        self.assertIn("kept in this run's briefing file", err)

    def test_an_unexpected_error_in_the_step_still_gives_the_summary_the_file_rewrite_and_the_json(self):
        """B3."""
        transport, surya, keystone = self.world(self.rows("k", 2))
        path = self.root / "r.json"
        code, out, err = self.go(transport, fakes.TtyInput("e\ns\n"), argv=["--json", str(path)],
                                 editor=fakes.FakeEditor(ValueError()))
        self.assertEqual(code, 0, err)
        self.assertIn("unexpected error: ValueError", err)
        self.assertIn("1 saved (0 edited), 0 skipped, 1 not saved", err)
        written = (self.briefings / (DATE + ".md")).read_text(encoding="utf-8")
        self.assertIn("## Approval", written)
        self.assertIn("unexpected error: ValueError", written)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((data["approval"]["saved"], data["approval"]["not_saved"]), (1, 1))
        self.assertEqual(data["approval"]["failed"][0]["kind"], "refused")

    def test_ctrl_c_at_the_prompt_still_gives_the_summary_the_file_rewrite_and_the_json(self):
        transport, surya, keystone = self.world(self.rows("k", 2))
        path = self.root / "r.json"

        def interrupt(n):
            if n == 2:
                raise KeyboardInterrupt()

        code, out, err = self.go(transport, fakes.TtyInput("s\n", before_read=interrupt),
                                 argv=["--json", str(path)])
        self.assertEqual(code, 0, err)
        self.assertIn("Stopped — remaining drafts not saved", err)
        self.assertIn("1 saved (0 edited), 0 skipped, 1 not saved", err)
        written = (self.briefings / (DATE + ".md")).read_text(encoding="utf-8")
        self.assertIn("1 saved (0 edited), 0 skipped, 1 not saved", written)
        data = json.loads(path.read_text(encoding="utf-8"))
        self.assertEqual((data["approval"]["saved"], data["approval"]["not_saved"]), (1, 1))

    def test_the_time_the_user_takes_to_approve_is_not_in_the_status_line_or_elapsed_seconds(self):
        """FR-004, G1-AC1: the elapsed time is the fetch, not the user's reading and typing."""
        results = []
        for taken in (0, 100):
            transport, surya, keystone = self.world(self.rows("k", 1))
            self.clock = FakeClock()
            path = self.root / ("r%d.json" % taken)
            stdin = fakes.TtyInput("s\n", before_read=lambda n, taken=taken: self.clock.advance(taken))
            code, out, err = self.go(transport, stdin, argv=["--json", str(path)])
            self.assertGreaterEqual(self.clock.monotonic(), taken)
            status = [l for l in out.splitlines() if l.startswith("Status:")]
            filed = [l for l in (self.briefings / (DATE + ".md")).read_text(encoding="utf-8").splitlines()
                     if l.startswith("Status:")]
            self.assertEqual(status, filed)
            results.append((status, json.loads(path.read_text(encoding="utf-8"))["elapsed_seconds"]))
        self.assertEqual(results[0], results[1])
        self.assertLess(results[1][1], 100)

    def test_the_exit_code_is_not_changed_by_unsaved_drafts(self):
        transport, surya, keystone = self.world(self.rows("k", 1))
        keystone.fail("EmailMessage.create", fakes.ToolError("refused"))
        code, out, err = self.go(transport, io.StringIO("s\n"))
        self.assertEqual(code, 0)
        self.assertIn("Draft not saved: refused", err)

    def test_offers_and_text_are_redacted_on_stderr(self):
        transport, surya, keystone = self.world(self.rows("k", 1))
        transport.servers[fakes.MODEL_BASE].responder = lambda request: fakes.model_reply(
            body="Your code is " + TOKEN_KEYSTONE)
        code, out, err = self.go(transport, io.StringIO("\n"))
        self.assertNotIn(TOKEN_KEYSTONE, err + out)
        self.assertIn("Your code is ***", err)

    def test_help_names_the_approved_draft_write_and_no_longer_says_it_only_reads(self):
        code, out, err = self.run_cli(["--help"])
        text = " ".join(out.split())
        self.assertNotIn("It only reads", text)
        self.assertIn("draft", text.lower())
        self.assertIn("approve", text.lower())
        self.assertIn("nothing is ever sent", text.lower())


class MainDefaults(CliCase):
    def test_help_exits_zero_and_names_the_options(self):
        code, out, err = self.run_cli(["--help"])
        self.assertEqual(code, 0)
        for word in ("--system", "--tz", "--json", "login"):
            self.assertIn(word, out)

    def test_help_names_the_model_and_signature_variables(self):
        code, out, err = self.run_cli(["--help"])
        text = " ".join(out.split())
        for name in ("OPENAI_BASE_URL", "OPENAI_MODEL", "OPENAI_API_KEY", "BRIEFING_LLM_URL",
                     "BRIEFING_LLM_MODEL", "BRIEFING_SIGNATURE"):
            self.assertIn(name, text)

    def test_system_clock_gives_aware_now_a_float_monotonic_and_a_local_date(self):
        clock = cli.SystemClock()
        self.assertIsNotNone(clock.now().tzinfo)
        self.assertIsInstance(clock.monotonic(), float)
        self.assertEqual(clock.local_date(), datetime.now().date())


if __name__ == "__main__":
    unittest.main()
