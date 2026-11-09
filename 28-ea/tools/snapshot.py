#!/usr/bin/env python3
"""Guardrail helper (G5-AC5): did a briefing run change anything on the platforms?

    python3 28-ea/tools/snapshot.py before        # write snapshots/before.json
    python3 28-ea/briefing.py                      # the run under test
    python3 28-ea/tools/snapshot.py after         # write snapshots/after.json
    python3 28-ea/tools/snapshot.py diff          # empty diff = nothing changed (exit 0)

A snapshot holds, per system, the id and `updated_at` of every mail message (all folders, drafts
included), calendar event and task. No content and no token is stored. It reads through `McpClient`,
so it can only call the read tools on its allowlist. Systems and tokens come from the same settings as
the briefing (login files, `AGENTSWITCH_<SYSTEM>_TOKEN`, harness variables).
"""
import argparse
import json
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from ea_briefing import auth  # noqa: E402
from ea_briefing.config import ConfigError, resolve  # noqa: E402
from ea_briefing.mcp import McpClient, McpError, failure_message  # noqa: E402

DEFAULT_DIR = Path(__file__).resolve().parent.parent / "snapshots"
KINDS = {"mail": "EmailMessage.list", "events": "CalendarEvent.list", "tasks": "Task.list"}


def _entry(rows):
    return {str(row.get("id")): str(row.get("updated_at") or "") for row in rows if isinstance(row, dict)}


def take(systems, tokens, transport=None):
    """{system name: {kind: {id: updated_at}}}; a kind that couldn't be read is {"error": <reason>}."""
    snapshot = {}
    for system in systems:
        token = tokens.get(system.name)
        client = McpClient(system.base_url, token, transport, label=system.label)
        kinds = {}
        for kind, tool in KINDS.items():
            if not token:
                kinds[kind] = {"error": "%s: login expired — log in again" % system.label}
                continue
            try:
                kinds[kind] = {"items": _entry(client.list_all(tool))}
            except McpError as exc:
                kinds[kind] = {"error": failure_message(system.label, exc, kind)}
        snapshot[system.name] = kinds
    return snapshot


def diff(before, after):
    """Lines describing every difference between two snapshots. Empty when nothing changed."""
    lines = []
    for system in sorted(set(before) | set(after)):
        if system not in before or system not in after:
            lines.append("%s: only in the %s snapshot" % (system, "before" if system in before else "after"))
            continue
        for kind in KINDS:
            b, a = before[system].get(kind, {}), after[system].get(kind, {})
            if "error" in b or "error" in a:
                lines.append("%s %s: could not be compared (%s)" % (system, kind, b.get("error") or a.get("error")))
                continue
            b, a = b.get("items", {}), a.get("items", {})
            lines += ["%s %s: added %s" % (system, kind, i) for i in sorted(set(a) - set(b))]
            lines += ["%s %s: removed %s" % (system, kind, i) for i in sorted(set(b) - set(a))]
            lines += ["%s %s: changed %s (%s -> %s)" % (system, kind, i, b[i], a[i])
                      for i in sorted(set(a) & set(b)) if a[i] != b[i]]
    return lines


def _parser():
    p = argparse.ArgumentParser(description=__doc__.split("\n\n")[0], epilog="Read-only: it never changes a record.")
    p.add_argument("action", choices=("before", "after", "diff"))
    p.add_argument("--system", help="only this system (suryodaya or keystone)")
    p.add_argument("--dir", help="snapshot folder (default: SNAPSHOT_DIR, else 28-ea/snapshots)")
    return p


def main(argv=None, env=None, transport=None):
    args = _parser().parse_args(argv)
    env = os.environ if env is None else env
    folder = Path(args.dir or env.get("SNAPSHOT_DIR") or DEFAULT_DIR)
    if args.action == "diff":
        try:
            before, after = (json.loads((folder / n).read_text(encoding="utf-8")) for n in ("before.json", "after.json"))
        except (OSError, ValueError) as exc:
            print("Need both before.json and after.json in %s (%s)" % (folder, exc), file=sys.stderr)
            return 2
        lines = diff(before, after)
        print("\n".join(lines) if lines else "No differences")
        return 1 if lines else 0
    try:
        settings = resolve(argparse.Namespace(system=args.system, tz=None), env)
    except ConfigError as exc:
        print(str(exc), file=sys.stderr)
        return 2
    tokens = {s.name: auth.load(s.name, settings.mode, env) for s in settings.systems}
    snapshot = take(settings.systems, tokens, transport)
    try:
        folder.mkdir(parents=True, exist_ok=True)
        (folder / (args.action + ".json")).write_text(json.dumps(snapshot, indent=1, sort_keys=True), encoding="utf-8")
    except OSError as exc:
        print("Snapshot not saved: %s" % exc, file=sys.stderr)
        return 1
    counts = ", ".join("%s %s %s" % (name, kind, len(v.get("items", {}))) if "items" in v
                       else "%s %s unavailable" % (name, kind)
                       for name, kinds in snapshot.items() for kind, v in kinds.items())
    print("Wrote %s (%s)" % (folder / (args.action + ".json"), counts))
    return 0


if __name__ == "__main__":
    sys.exit(main())
