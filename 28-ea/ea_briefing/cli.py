"""The command: arguments, the `login` subcommand, wiring, and exit codes (G1, G6-AC4, G6-AC5).

    briefing.py [--system suryodaya|keystone] [--tz system=zone ...] [--json PATH]
    briefing.py login <system>

After the briefing is printed and saved, each proposed reply is offered for approval on the terminal (stderr);
an approved reply is saved as an unsent draft in the mailbox, the file is saved again with an Approval section,
and the `--json` file is written last. Nothing is ever sent. With stdin at end of file nothing is saved.

Exit codes: 0 the briefing ran (even with missing parts), 1 nothing could be fetched (or a login
failed), 2 a usage or configuration error. The briefing goes to stdout; progress lines, warnings and
notices go to stderr. `main` takes everything it needs as arguments so tests run it without a network,
a terminal or the real environment.
"""
import argparse
import getpass as _getpass
import io
import os
import sys
import time
from datetime import datetime, timezone

from . import approval, auth, render, runner, store
from .draft_writer import DraftWriter
from .config import SYSTEMS, ConfigError, resolve
from .mcp import McpClient, McpError, failure_message

EPILOG = ("Settings come from the environment: BRIEFING_DIR (where briefings are saved), "
          "AGENTSWITCH_<SYSTEM>_TOKEN, and for the Eval Harness AGENTSWITCH_TOKEN, AGENTSWITCH_INSTANCE and "
          "AGENTSWITCH_BASE_URL together. Replies are drafted by a model: OPENAI_BASE_URL with OPENAI_MODEL "
          "(and OPENAI_API_KEY if it needs one), else BRIEFING_LLM_URL (default http://127.0.0.1:8111/v1) with "
          "BRIEFING_LLM_MODEL; BRIEFING_SIGNATURE overrides the name replies are signed with. "
          "Editing a reply opens $EDITOR, then nano, then asks for typed lines. "
          "Run 'briefing.py login <system>' once per system.")


class SystemClock:
    """The real clock: `now()` (aware), `monotonic()` and the machine-local date for file names."""

    def now(self):
        return datetime.now(timezone.utc)

    def monotonic(self):
        return time.monotonic()

    def local_date(self):
        return datetime.now().date()


class _Parser(argparse.ArgumentParser):
    def __init__(self, **kw):
        super().__init__(prog="briefing.py", allow_abbrev=False, **kw)


def _run_parser():
    p = _Parser(description="Today's briefing of mail needing an answer (with proposed replies), meetings and "
                            "overdue tasks from Suryodaya and Keystone. Afterwards you approve each proposed "
                            "reply one at a time; an approved reply is saved as an unsent draft in your mailbox, "
                            "which is the only change this command makes. Nothing is ever sent, and with no "
                            "terminal input nothing is saved.", epilog=EPILOG)
    p.add_argument("--system", help="read only this system (suryodaya or keystone)")
    p.add_argument("--tz", action="append", metavar="SYSTEM=ZONE",
                   help="time zone for one system, e.g. --tz keystone=Europe/Amsterdam (repeatable)")
    p.add_argument("--json", metavar="PATH", help="also write the result as JSON to PATH")
    return p


def _login_parser():
    p = _Parser(description="Log in to one system and keep the token in ~/.config/agentswitch/<system>.json "
                            "(mode 0600). The password is asked for and never stored.")
    p.add_argument("system", help="suryodaya or keystone")
    return p


def _say(line, stream=None):
    print(line, file=stream or sys.stderr)


def _login(argv, env, stdin, transport, getpass_fn):
    args = _login_parser().parse_args(argv)
    name = args.system.strip().lower()
    if name not in SYSTEMS:
        _say("Unknown system '%s' — use suryodaya or keystone" % args.system)
        return 2
    system = SYSTEMS[name]

    def ask(prompt):
        sys.stderr.write(prompt)
        sys.stderr.flush()
        return stdin.readline()

    try:
        auth.login(system, env, transport=transport, getpass_fn=getpass_fn or _getpass.getpass, input_fn=ask)
    except auth.LoginInputMissing as exc:
        _say(str(exc))
        return 2
    except auth.LoginRejected as exc:
        _say(str(exc))
        return 1
    except McpError as exc:
        _say(failure_message(system.label, exc))
        return 1
    print("Logged in to %s — token saved to %s" % (system.label, auth.token_path(name, env)))
    return 0


def main(argv=None, env=None, stdin=None, clock=None, transport=None, getpass_fn=None, editor=None):
    """Run the command and return its exit code. `editor` (tests) replaces the editor chain of the approval step."""
    argv = list(sys.argv[1:] if argv is None else argv)
    env = os.environ if env is None else env
    stdin = sys.stdin if stdin is None else stdin
    if stdin is None:   # no console at all: the approval step then sees the end of the input
        stdin = io.StringIO("")
    clock = clock or SystemClock()
    if argv and argv[0] == "login":
        return _login(argv[1:], env, stdin, transport, getpass_fn)

    args = _run_parser().parse_args(argv)
    try:
        settings = resolve(args, env)
    except ConfigError as exc:
        _say(str(exc))
        return 2
    for line in settings.warnings + settings.notes:
        _say(line)

    tokens = {s.name: auth.load(s.name, settings.mode, env) for s in settings.systems}
    # OPENAI_API_KEY is a secret even when it is ignored (no OPENAI_BASE_URL): it may still turn up in mail (G5-AC6)
    secrets = [t for t in tokens.values() if t] + [k for k in (settings.model.api_key, env.get("OPENAI_API_KEY")) if k]
    signatures = {s.name: auth.load_signature(s.name, settings.mode, env) for s in settings.systems}
    writer = DraftWriter(settings.model, transport, clock)
    outcome = runner.run(settings.systems, tokens, clock, transport, _say, writer, signatures)
    results, day = outcome.results, clock.local_date()

    text = render.markdown(results, outcome.elapsed, day)
    sys.stdout.write(store.redact(text, secrets))
    sys.stdout.flush()

    saved = store.save(text, results, settings.briefing_dir, day, [s.name for s in settings.systems], secrets)
    if saved.message:
        _say(saved.message)
    summary = None
    if not render.nothing_fetched(results):
        summary = _approve(results, settings, tokens, transport, stdin, editor or approval.external_editor(env),
                           secrets, saved.status == "saved")
        if saved.status == "saved":   # a second save only after a first one that worked: the same file, replaced
            again = store.save(text + render.approval_section(summary), results, settings.briefing_dir, day,
                               [s.name for s in settings.systems], secrets)
            if again.message:
                _say(again.message)
    if args.json:
        written = store.save_json(render.json_result(results, outcome.elapsed, day, outcome.model, summary),
                                  args.json, secrets)
        if written.message:
            _say(written.message)
    return 1 if render.nothing_fetched(results) else 0


def _approve(results, settings, tokens, transport, stdin, editor, secrets, file_kept):
    """The approval step: one client per system that has proposals; every line it shows goes to stderr, redacted.
    `file_kept`: the briefing file was saved, so it will be rewritten with the text of a failed save."""
    proposals = approval.proposals(results)
    labels = {s.name: s.label for s in settings.systems}
    bases = {s.name: s.base_url for s in settings.systems}
    clients = {name: McpClient(bases[name], tokens[name], transport, label=labels[name])
               for name in {p.system for p in proposals}}
    return approval.run(proposals, clients, stdin, editor, lambda line: _say(store.redact(line, secrets)),
                        file_kept)
