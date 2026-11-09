"""auth.py: login file, token load order, whoami and its error messages (P-B-20, P-B-2, P-B-3)."""
import json
import os
import stat
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace

from ea_briefing import auth
from ea_briefing.auth import LoginRejected, User
from ea_briefing.mcp import LoginExpired, ToolFailed, Unreachable, failure_message
from tests import fakes
from tests.fakes import FakeTransport, HttpStatus

BASE = "https://fake.test"
TOKEN = "tok-secret-123"
KEYSTONE = SimpleNamespace(name="keystone", label="Keystone", base_url=BASE)


def transport_with(me=None, token=TOKEN):
    transport = FakeTransport()
    server = transport.server(BASE, "fake", token, me if me is not None else fakes.load_fixture("me_keystone.json"))
    return transport, server


class Whoami(unittest.TestCase):
    def test_reads_api_auth_me_with_the_token(self):
        transport, _ = transport_with()
        user = auth.whoami(BASE, TOKEN, transport)
        self.assertEqual(user, User(id="u-100", employee_id="e-100", email="team28c@example.test",
                                    name="Team Twenty-Eight"))
        (call,) = transport.calls
        self.assertEqual((call.method, call.url), ("GET", BASE + "/api/auth/me"))
        self.assertEqual(call.headers["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(call.timeout, 15)

    def test_the_user_can_sit_under_a_user_key(self):
        transport, _ = transport_with(me={"user": fakes.load_fixture("me_keystone.json")})
        self.assertEqual(auth.whoami(BASE, TOKEN, transport).id, "u-100")

    def test_missing_employee_id_is_none(self):
        transport, _ = transport_with(me={"id": "u-1", "email": "a@b.test", "name": "A"})
        user = auth.whoami(BASE, TOKEN, transport)
        self.assertIsNone(user.employee_id)

    def test_ids_are_text_even_when_the_platform_sends_numbers(self):
        transport, _ = transport_with(me={"id": 42, "employee_id": 7, "email": "a@b.test", "name": "A"})
        user = auth.whoami(BASE, TOKEN, transport)
        self.assertEqual((user.id, user.employee_id), ("42", "7"))

    def test_no_token_is_login_expired_without_a_request(self):
        transport, _ = transport_with()
        for token in (None, ""):
            with self.assertRaises(LoginExpired):
                auth.whoami(BASE, token, transport)
        self.assertEqual(transport.calls, [])

    def test_401_is_login_expired(self):
        transport, server = transport_with()
        server.expire()
        with self.assertRaises(LoginExpired):
            auth.whoami(BASE, TOKEN, transport)

    def test_a_protocol_error_from_the_transport_is_unreachable(self):
        import http.client

        def broken(*args):
            raise http.client.BadStatusLine("garbage")
        with self.assertRaises(Unreachable):
            auth.whoami("https://fake.test", "tok", broken)

    def test_wrong_token_is_login_expired(self):
        transport, _ = transport_with()
        with self.assertRaises(LoginExpired):
            auth.whoami(BASE, "stale", transport)

    def test_timeout_and_connection_failure_are_unreachable(self):
        for exc in (TimeoutError("timed out"), ConnectionRefusedError("refused")):
            transport, server = transport_with()
            server.down(exc)
            with self.assertRaises(Unreachable):
                auth.whoami(BASE, TOKEN, transport)

    def test_other_failures_are_tool_failed_with_a_reason(self):
        transport, server = transport_with(me=HttpStatus(500))
        with self.assertRaises(ToolFailed) as cm:
            auth.whoami(BASE, TOKEN, transport)
        self.assertIn("HTTP 500", cm.exception.reason)
        for bad in ("not json", "[]", '{"name": "no id"}'):
            with self.assertRaises(ToolFailed, msg=bad):
                auth.whoami(BASE, TOKEN, lambda *a, _b=bad: (200, "application/json", _b))

    def test_each_failure_maps_to_one_of_the_three_messages(self):
        """P-B-2 and P-B-3: the lines the three parts of a system show."""
        self.assertEqual(failure_message("Keystone", LoginExpired()), "Keystone: login expired — log in again")
        self.assertEqual(failure_message("Suryodaya", Unreachable("timeout")),
                         "Suryodaya: can't be reached right now — try again later")
        self.assertEqual(failure_message("Keystone", ToolFailed("HTTP 500")),
                         "Keystone: couldn't be read (reason: HTTP 500) — report it if it repeats")


class Load(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        self.dir = Path(tmp.name) / ".config" / "agentswitch"

    def write_file(self, name, content):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / (name + ".json")).write_text(content)

    def env(self, **extra):
        return dict({"HOME": self.home}, **extra)

    def test_the_login_file_is_read_in_local_mode(self):
        self.write_file("keystone", json.dumps({"token": "from-file", "signature": "S"}))
        self.assertEqual(auth.load("keystone", "local", self.env()), "from-file")

    def test_the_system_variable_overrides_the_file(self):
        self.write_file("keystone", json.dumps({"token": "from-file"}))
        env = self.env(AGENTSWITCH_KEYSTONE_TOKEN="from-env", AGENTSWITCH_SURYODAYA_TOKEN="other")
        self.assertEqual(auth.load("keystone", "local", env), "from-env")
        self.assertEqual(auth.load("suryodaya", "local", env), "other")

    def test_harness_token_wins_and_no_file_is_read(self):
        self.write_file("keystone", json.dumps({"token": "from-file"}))
        env = self.env(AGENTSWITCH_TOKEN="harness", AGENTSWITCH_KEYSTONE_TOKEN="sys-env")
        self.assertEqual(auth.load("keystone", "harness", env), "harness")
        self.assertEqual(auth.load("keystone", "harness", self.env()), None)  # not even the file

    def test_nothing_found_is_none(self):
        self.assertIsNone(auth.load("keystone", "local", self.env()))

    def test_a_broken_or_empty_file_is_none(self):
        for content in ("not json", "[]", json.dumps({"token": ""}), json.dumps({"signature": "S"}), '{"token": 5}'):
            self.write_file("keystone", content)
            self.assertIsNone(auth.load("keystone", "local", self.env()), content)

    def test_empty_variables_do_not_override(self):
        self.write_file("keystone", json.dumps({"token": "from-file"}))
        self.assertEqual(auth.load("keystone", "local", self.env(AGENTSWITCH_KEYSTONE_TOKEN="")), "from-file")

    def test_the_real_home_is_never_used_when_env_has_home(self):
        self.assertEqual(auth.token_path("keystone", self.env()), self.dir / "keystone.json")


class LoadSignature(unittest.TestCase):
    """auth.load_signature: BRIEFING_SIGNATURE, else the name stored at login; no file in harness mode."""

    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        self.dir = Path(tmp.name) / ".config" / "agentswitch"

    def write_file(self, name, content):
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / (name + ".json")).write_text(content)

    def env(self, **extra):
        return dict({"HOME": self.home}, **extra)

    def test_the_login_file_signature_is_read_per_system(self):
        self.write_file("keystone", json.dumps({"token": "t", "signature": "Keystone Name"}))
        self.write_file("suryodaya", json.dumps({"token": "t", "signature": "Surya Name"}))
        self.assertEqual(auth.load_signature("keystone", "local", self.env()), "Keystone Name")
        self.assertEqual(auth.load_signature("suryodaya", "local", self.env()), "Surya Name")

    def test_brief_signature_overrides_the_file_for_every_system(self):
        self.write_file("keystone", json.dumps({"token": "t", "signature": "Keystone Name"}))
        env = self.env(BRIEFING_SIGNATURE="  Env Name ")
        self.assertEqual(auth.load_signature("keystone", "local", env), "Env Name")
        self.assertEqual(auth.load_signature("suryodaya", "local", env), "Env Name")

    def test_no_file_and_no_variable_is_none_so_the_caller_uses_the_whoami_name(self):
        self.assertIsNone(auth.load_signature("keystone", "local", self.env()))

    def test_blank_values_count_as_unset(self):
        self.write_file("keystone", json.dumps({"token": "t", "signature": "   "}))
        self.assertIsNone(auth.load_signature("keystone", "local", self.env(BRIEFING_SIGNATURE="  ")))

    def test_a_broken_or_wrong_typed_file_is_none(self):
        for content in ("not json", "[]", json.dumps({"signature": 5}), json.dumps({"token": "t"})):
            self.write_file("keystone", content)
            self.assertIsNone(auth.load_signature("keystone", "local", self.env()), content)

    def test_harness_mode_reads_the_variable_but_never_the_file(self):
        self.write_file("keystone", json.dumps({"token": "t", "signature": "File Name"}))
        self.assertIsNone(auth.load_signature("keystone", "harness", self.env()))
        self.assertEqual(auth.load_signature("keystone", "harness", self.env(BRIEFING_SIGNATURE="Env Name")),
                         "Env Name")


class Login(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.home = tmp.name
        self.env = {"HOME": self.home}
        self.path = Path(tmp.name) / ".config" / "agentswitch" / "keystone.json"
        self.transport, self.server = transport_with()
        self.server.login_handler = lambda body: {"token": "new-token-999", "user": {"id": "u-100"}}
        self.asked = []
        self.passwords = []

    def getpass_fn(self, prompt="Password: "):
        self.passwords.append(prompt)
        return "hunter2-password"

    def input_fn(self, prompt=""):
        self.asked.append(prompt)
        return "team28c@example.test" if "mail" in prompt.lower() else "Sandip"

    def login(self):
        auth.login(KEYSTONE, self.env, transport=self.transport, getpass_fn=self.getpass_fn,
                   input_fn=self.input_fn)

    def test_password_is_asked_with_getpass_and_posted_to_login(self):
        self.login()
        self.assertEqual(len(self.passwords), 1)
        (call,) = [c for c in self.transport.calls if c.path == "/api/auth/login"]
        self.assertEqual(call.method, "POST")
        self.assertEqual(call.json, {"email": "team28c@example.test", "password": "hunter2-password"})

    def test_token_and_signature_are_stored_with_mode_0600_and_the_password_is_not(self):
        self.login()
        self.assertEqual(json.loads(self.path.read_text()), {"token": "new-token-999", "signature": "Sandip"})
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertNotIn("hunter2", self.path.read_text())
        self.assertEqual(stat.S_IMODE(os.stat(self.path.parent).st_mode) & 0o077, 0)

    def test_the_stored_token_is_what_load_returns(self):
        self.login()
        self.assertEqual(auth.load("keystone", "local", self.env), "new-token-999")

    def test_signature_is_asked_the_first_time_only(self):
        self.login()
        first_questions = [q for q in self.asked if "mail" not in q.lower()]
        self.assertEqual(len(first_questions), 1)
        self.asked.clear()
        self.server.login_handler = lambda body: {"token": "second-token"}
        self.login()
        self.assertEqual([q for q in self.asked if "mail" not in q.lower()], [])
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved, {"token": "second-token", "signature": "Sandip"})

    def test_an_old_file_with_looser_mode_ends_up_0600(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"token": "old", "signature": "Old Name"}))
        os.chmod(self.path, 0o644)
        self.login()
        self.assertEqual(stat.S_IMODE(os.stat(self.path).st_mode), 0o600)
        self.assertEqual(json.loads(self.path.read_text())["signature"], "Old Name")

    def test_a_rejected_login_changes_nothing(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"token": "old", "signature": "S"}))

        def refuse(body):
            raise HttpStatus(401, "bad credentials")
        self.server.login_handler = refuse
        with self.assertRaises(LoginRejected) as cm:
            self.login()
        self.assertNotIn("hunter2", str(cm.exception))
        self.assertEqual(json.loads(self.path.read_text())["token"], "old")

    def test_unreachable_login_raises_unreachable(self):
        self.server.down()
        with self.assertRaises(Unreachable):
            self.login()
        self.assertFalse(self.path.exists())

    def test_a_reply_without_a_token_is_tool_failed_and_writes_nothing(self):
        self.server.login_handler = lambda body: {"user": {}}
        with self.assertRaises(ToolFailed):
            self.login()
        self.assertFalse(self.path.exists())

    def test_no_temporary_files_are_left_behind(self):
        self.login()
        self.assertEqual([p.name for p in self.path.parent.iterdir()], ["keystone.json"])


if __name__ == "__main__":
    unittest.main()
