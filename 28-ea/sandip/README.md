# Seat 28 EA: daily briefing (Sandip)

## How to run

Python 3.9+, no packages to install. Run from the repository root.

```
python3 28-ea/sandip/briefing.py login suryodaya     # once per system, again every 7 days
python3 28-ea/sandip/briefing.py login keystone
export OPENAI_BASE_URL=http://127.0.0.1:8111/v1 OPENAI_MODEL=gemini-2.5-flash   # glc, for proposed replies
python3 28-ea/sandip/briefing.py                     # print and save today's briefing
python3 -m unittest discover -s 28-ea/sandip/tests -t 28-ea/sandip              # offline tests
```

After the briefing, each proposed reply is offered: `s` saves it as an unsent draft, `e` edits it first, Enter
skips it and `q` stops. Without the model settings the briefing still runs, with no proposed replies. To skip
every reply, run with `< /dev/null`. The briefing is also saved to `28-ea/sandip/briefings/<date>.md`.

One command prints and saves today's briefing from Suryodaya and Keystone: unread mail that needs an
answer, each with a summary and a proposed reply, meetings in each system's own day, with clash and
declined marks, and your own overdue tasks. Afterwards you approve each proposed reply one at a time, and
an approved reply is saved as an unsent draft in your mailbox. That is the only thing the command writes:
nothing is ever sent, booked or otherwise changed on either system.

Status: slice 3 of 3. Mail is read, each message needing an answer gets a proposed reply, and the replies
can be approved into the mailbox (see "Approving drafts"). The mail field names, the model gateway's request
format and the draft fields were probed live (plan tasks 15 and 24); glc is not OpenAI-compatible (see "What
the live probe found").

Design and plan: `docs/sdlc/changes/2026-11-05-daily-briefing/` (`prd.md`, `solution-design.md`, `plan.md`).
Python 3.9+ and the standard library only.

## Login

Once per system, and again when the login expires (every 7 days):

```
python3 28-ea/sandip/briefing.py login suryodaya
python3 28-ea/sandip/briefing.py login keystone
```

It asks for your email, then the password with a hidden prompt (the password is never stored), and, the
first time only, the name to sign replies with. The token and that name go to
`~/.config/agentswitch/<system>.json`, mode 0600, outside the repository.

## Run

```
python3 28-ea/sandip/briefing.py                          # both systems
python3 28-ea/sandip/briefing.py --system keystone        # one system
python3 28-ea/sandip/briefing.py --tz keystone=Europe/Amsterdam
python3 28-ea/sandip/briefing.py --json result.json       # also write the result as JSON
```

Defaults: Suryodaya's day is Asia/Kolkata, Keystone's America/New_York. An unknown zone falls back to the
default with a line on stderr. "Reading Suryodaya…" style progress lines go to stderr; stdout holds only
the briefing. It ends with one `Status:` line naming each part of each system as complete or missing, and
the seconds the run took.

**Mail.** Per system, unread inbox mail needing an answer is listed oldest first with sender, subject,
received time in the system's zone, a one-line English summary and a proposed reply (greeting, body, your
signature). Fixed rules, never the model, decide what needs an answer: read, draft, answered and non-inbox
mail is not listed; mail from no-reply or automated senders, newsletters and your own address is listed
one line each under "Skipped — no reply expected" with its reason; a message that already has a draft of
yours shows "draft already saved" with its text; a message that can't be opened is named. A message the
model can't answer safely (no greeting, over 120 words, reply in another language, no summary in English)
shows "No draft for this message (…) — reply by hand", with its first line when there is no summary. A
message that asks you to agree to a date, amount or attendance is marked "needs your decision" and its
reply is redrafted to commit to nothing.

**The 45 s drafting limit.** Drafting starts as soon as a system's mail is read, takes the oldest message
first across both systems, and runs at most 4 model calls at a time. Each call times out after 15 s. The
45 s count from the start of the run and bound every model call: none starts at or after 45 s (the second
call for a message that needs your decision included), and a reply that arrives after 45 s is not used.
Such messages, and the ones still waiting, show their first line and "No draft for this message (time limit) —
reply by hand", so the whole run stays within about 60 s. "needs your decision" is set by a fixed keyword
check, not by the model, so it shows on every message that matches, with or without a draft. If the model
can't be reached, the rest of the run skips it and every message shows "(model unavailable)".
"Drafting replies (3 of 7)…" goes to stderr only.

Files go to `28-ea/sandip/briefings/` (git-ignored; they hold private data): `<date>.md` for a full run,
`<date>-<system>.md` for `--system`, by the date on your machine. A rerun replaces the file of the same
kind only. A run that fetched nothing writes no file ("Nothing fetched — today's earlier briefing file
kept"). If the file can't be written, the briefing is still printed and a line on stderr says why.

Exit codes: 0 the briefing ran, even with missing parts; 1 nothing could be fetched (or a login failed);
2 a usage or configuration error.

`--json` writes, per system and part, `complete` or the missing `reason`; the events with `clash` and
`declined`; your overdue task ids with `days_overdue`; and the count of other overdue tasks. For a complete
Mail part it adds `needs_answer` (each message id with a `draft_status`: `proposed`, `model_unavailable`,
`time_limit`, `invalid` or `needs_decision_redraft_failed`), `skipped` (id and reason), `unopened` and
`draft_saved` (ids); at the top level a `model` block holds `calls`, `failures` and `unreachable`. `calls` counts one per message request, not HTTP retries or the `/chat` fallback. It is
written also when nothing was fetched (exit 1).

## Approving drafts

After the briefing is printed and saved, each proposed reply is offered on the terminal, oldest first,
Suryodaya's before Keystone's. The offers, "Saving draft…", "Saved", failure lines and the summary go to
stderr; stdout holds only the briefing. Per reply you type:

| Key | Does |
|---|---|
| `s` | save the proposed text as a draft |
| `e` | edit the text, then save it: `$EDITOR`, else `nano`, else you type lines and end with a line holding only `.`. An empty text skips the reply. Needs a terminal |
| Enter, or anything else | skip this reply |
| `q` | stop: this reply and every later one stay unsaved |

With no proposed replies the step says "No drafts to approve" and ends. A save is one `EmailMessage.create`
as an unsent draft (`folder=drafts`, `is_draft=true`) in the mailbox the briefing read, addressed to the
sender, with a "Re: " subject and the original's thread, `in_reply_to` and `references`. Just before it the
original is read again: if it was answered meanwhile you see "Already answered — draft not saved". Right after
it the new draft is read back: if that fails you see "Draft may not be saved — check your Drafts folder", and
nothing is retried, so no second draft can appear. A mailbox that refuses the save gives "Draft not saved: …"
with your text shown below it, to save by hand. If the login expires, that system's remaining replies are not
offered ("Keystone: login expired — remaining drafts not saved; log in again and rerun") and the other system's
still are. Ctrl-C counts as stop.

The step ends with "N saved (M edited), N skipped, N not saved". Replies left after a stop or an expired login,
and failed saves, count as not saved. The briefing file is then saved again with an `## Approval` section
after the status line holding that summary and the approved text of every save that failed, and `--json`
(written last) adds an `approval` key with the same counts and the failed saves (`system`, `id`, `subject`,
`reason`, `text`). Elapsed seconds in the status line exclude approval time.

**Stdin at end of file saves nothing.** With stdin from `/dev/null` (the harness) every reply is skipped:
"0 saved (0 edited), N skipped, 0 not saved", and no `create` call is made. A piped `s` is a real answer and
makes one save; a piped `e` is a skip, because there is no terminal. A rerun shows a message that has a saved
draft of yours as "draft already saved" with the saved text, and proposes nothing for it.

## Settings

| Variable | Meaning |
|---|---|
| `BRIEFING_DIR` | Where briefings are saved (default `28-ea/sandip/briefings`) |
| `AGENTSWITCH_SURYODAYA_TOKEN`, `AGENTSWITCH_KEYSTONE_TOKEN` | Token for that system; overrides the login file |
| `SNAPSHOT_DIR` | Where `tools/snapshot.py` writes (default `28-ea/sandip/snapshots`, git-ignored) |
| `OPENAI_BASE_URL` | The model gateway; the request goes to `<OPENAI_BASE_URL>/chat/completions`, and when that answers 404 (glc does) to glc's own `<OPENAI_BASE_URL>/chat`. When set, `OPENAI_MODEL` is required (the run exits 2 without it) and the `BRIEFING_LLM_*` variables are ignored |
| `OPENAI_MODEL` | The model name sent with each request |
| `OPENAI_API_KEY` | Sent as `Authorization: Bearer …` when set; treated as a secret and shown as `***` anywhere it would appear |
| `BRIEFING_LLM_URL` | Used when `OPENAI_BASE_URL` is not set; default `http://127.0.0.1:8111/v1` (glc). Other `OPENAI_*` variables are ignored then |
| `BRIEFING_LLM_MODEL` | The model name for `BRIEFING_LLM_URL`. Unset is not an error: the run exits 0, stderr says "BRIEFING_LLM_MODEL not set — no drafts this run", no model call is made and each message shows "(model unavailable)" |
| `BRIEFING_SIGNATURE` | The name replies are signed with, for both systems. Else the name given at `login` for that system, else the name from `/api/auth/me` |

For glc: `OPENAI_BASE_URL=http://127.0.0.1:8111/v1 OPENAI_MODEL=gemini-2.5-flash` (glc needs no key). The model settings work in local and harness mode alike.

The drafting time limit is fixed, not a setting: 45 s from the start of the run, 4 calls at a time. The 15 s timeout applies to each HTTP request; waiting out a busy answer (429 or 503, up to 3 times, 3 s apart) and the `/chat` fallback are bounded by the 45 s limit, and a message the limit stops shows "time limit".

## Harness mode

For the Eval Harness. It starts when `AGENTSWITCH_TOKEN` or `AGENTSWITCH_INSTANCE` is set, and then all three
of `AGENTSWITCH_TOKEN`, `AGENTSWITCH_INSTANCE` (`suryodaya` or `keystone`) and `AGENTSWITCH_BASE_URL` are
required, so a harness token can never reach the live system by default. Otherwise the run exits 2 with one
plain line, as it does for a `--system` that names another instance. The run reads only that instance at that
URL, uses the harness token and no login file, and saves `<date>-<instance>.md`. `AGENTSWITCH_BASE_URL` alone
is ignored with a warning on stderr. Point the drafting model at the harness's gateway with `OPENAI_BASE_URL`,
`OPENAI_MODEL` and `OPENAI_API_KEY`.

```
AGENTSWITCH_TOKEN=... AGENTSWITCH_INSTANCE=keystone AGENTSWITCH_BASE_URL=https://... \
  python3 28-ea/sandip/briefing.py --json run/briefing.json < /dev/null
```

## Tests

From the repository root, offline (fake transport, fake clock, fixtures):

```
python3 -m unittest discover -s 28-ea/sandip/tests -t 28-ea/sandip
```

`tests/test_scenarios.py` runs `cli.main` end to end, one test per PRD scenario; the other `test_<module>.py`
files cover one module each.

## Safety rules

- The MCP client (`ea_briefing/mcp.py`) refuses, before any network call, every tool outside a fixed read
  allowlist. `EmailMessage.reply`, `send_email`, `Task.update` and the like cannot be called. The one write,
  `McpClient.create_draft`, is a separate method: it takes no tool name, and refuses unless it gets a fresh
  single-use approval token (minted only by the approval loop, once per reply you chose to save) and the
  arguments say `folder="drafts"` and `is_draft=true`. `call` and the allowlist stay read-only.
- A reply is saved only after you chose `s` or `e` for it in that run; the end of input never saves.
- The only subprocess the code starts is your text editor, and only for the human approval step: after you type
  `e` for a reply, and only when standard input is a terminal (otherwise `e` is a skip and no editor opens).
  It runs `$EDITOR`, else `nano`, as an argument list (no shell) on a temporary file holding the proposed reply,
  which is deleted afterwards. The edited text goes back to the same approval flow; the editor itself never
  saves or sends anything.
- No token appears on screen, in the Markdown file or in the JSON: loaded tokens are replaced by `***` before
  anything is printed or written, and the renderer is never given one.
- Files are written to a temporary file and renamed, so a failed write never damages an earlier file.
- Mail is sent to the model as quoted data and the model's answer is checked in code; the model never adds or
  removes a message, and a proposed reply is text in the briefing only. The model API key is redacted like a
  token.
- Briefings can hold mail content, so `briefings/` and `snapshots/` are git-ignored. Don't commit them.

### Guardrail check

`tools/snapshot.py` records the id and `updated_at` of mail, events and tasks on each system (no content, no
token) and diffs two snapshots. Read-only.

```
python3 28-ea/sandip/tools/snapshot.py before
python3 28-ea/sandip/briefing.py < /dev/null    # everything skipped
python3 28-ea/sandip/tools/snapshot.py after
python3 28-ea/sandip/tools/snapshot.py diff     # "No differences", exit 0
```

With one approved save in the run, the diff is exactly one new draft.

## What the live probe found

Plan task 1, a read-only probe of both systems with a `team28c` login (2026-11-06), confirmed the paging
(`limit` up to 1000 and `offset`; replies `{data, total, limit, offset}` with no `has_more`), the event and
task row fields, and the `/api/auth/me` shape. It corrected three things, now built in:
offset-less event times are local wall-clock (the row's `timezone`, else the system's zone), not UTC;
a date-only all-day end is exclusive; and the server's `lt:`/`gt:` filters compare the stored text, so the
fetch uses a date-only window with a day of slack and the client-side overlap check decides.
`assignee_id` links to an Employee (the login's `employee_id`, which is empty for `team28c`), so for this
login only `created_by` finds own tasks. The slice 2 probe (plan task 15, same day, read-only) confirmed every `EmailMessage` field and list filter the
mail reader uses on both systems (`mail.py` holds them in one place), that `EmailMessage.get` returns the row
itself and marks nothing read, and that Keystone now has a mailbox (`Mailbox.list` row with `email`) and 3 unread
inbox messages, while Suryodaya still has no mailbox. It refuted one assumption: glc is not OpenAI-compatible.
`/v1/chat/completions` answers 404; glc's own `POST /v1/chat` takes `{model, system, messages, max_tokens,
temperature}` and answers `{text}`, often with the JSON in a code fence. It drops a `system`-role message, so the
system prompt goes in its `system` field, and it answers 503 to calls made in quick succession (provider
cooldowns), so the writer waits 3 s and retries up to 3 times, never past the 45 s limit. `draft_writer.py` tries
`chat/completions` first and falls back to glc's API on a 404. The slice 3 probe (plan task 24, 2026-11-07,
read-only) found that `EmailMessage.create` requires `company_id`, `from_email` and `to`, takes no `created_by`,
and that inbox rows carry `company_id`, `mailbox_id` and `thread_id` (with `references` and `in_reply_to`
null); the Mailbox row's address field is `email` (`approval.py` builds the arguments in one place). Whether a
saved draft carries your `created_by` and the `in_reply_to` sent is confirmed by the live save, see the
plan's slice 3 live evidence.

## Timing record

Three live full runs on 2026-11-06 (both systems, `team28c` login, Mail is "no mailbox" on both). Each is under
the 60 s limit; the seconds are the status line's.

| Run | Date | Status-line seconds |
|---|---|---|
| 1 | 2026-11-06 | 11.7 |
| 2 | 2026-11-06 | 12.2 |
| 3 | 2026-11-06 | 12.6 |

Slice 2, three live full runs on 2026-11-06 (both systems, `team28c` login, `OPENAI_BASE_URL` pointing at glc with
`gemini-2.5-flash`, stdin from `/dev/null`). Keystone's 3 messages each got a proposed reply; Suryodaya has no
mailbox. Drafting waits on glc's cooldowns, so a run takes about 30 s, still under the 60 s limit.

| Run | Date | Status-line seconds |
|---|---|---|
| 1 | 2026-11-06 | 28.6 |
| 2 | 2026-11-06 | 27.1 |
| 3 | 2026-11-06 | 30.3 |
