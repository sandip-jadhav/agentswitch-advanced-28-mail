"""approval.py: proposals, the offer loop, the save with its re-check and read-back, the summary (P-B-38 to P-B-44).

The seam: `approval.run(proposals, clients, stdin, editor, say)`. `stdin` is a scripted text stream
(`io.StringIO` is not a terminal; `fakes.TtyInput` is), `editor` is `fakes.FakeEditor`, `say` collects lines.
Platforms are `FakeTransport` servers holding real mail rows, read through `mail.read` so a proposal is
built the way the command builds it.
"""
import io
import unittest
from unittest import mock

from ea_briefing import approval, mail
from ea_briefing.approval import ApprovalSummary, EditorUnavailable, Proposal
from ea_briefing.auth import User
from ea_briefing.draft_writer import Draft, DraftFailed
from ea_briefing.mcp import McpClient, PartResult
from ea_briefing.render import SystemResult
from tests.fakes import (FakeEditor, FakeTransport, HttpStatus, MAILBOX_ROW, ToolError, TtyInput, message_row,
                         serve_mail)

BASE = "https://fake.test"
OTHER_BASE = "https://other.test"
USER = User("u-100", "e-100", "team28c@example.test", "Team")
SAVE_PROMPT = "[s]ave / [e]dit then save / [Enter] skip / [q] stop"


def question(i, **kw):
    kw.setdefault("subject", "Question %d" % i)
    kw.setdefault("sender", "p%d@example.test" % i)
    return message_row("m%d" % i, kw.pop("subject"), kw.pop("sender"), "2026-11-0%dT09:00:00Z" % i, **kw)


class World:
    """One system with a mailbox and `rows`; `result` is its SystemResult with a Draft for every message."""

    def __init__(self, rows, name="keystone", label="Keystone", base=BASE, mailbox=MAILBOX_ROW, drafts=None):
        self.transport = FakeTransport()
        self.server = self.transport.server(base, name, "tok")
        self.server.rows("Mailbox.list", [mailbox] if mailbox else [])
        self.server.rows("EmailAccount.list", [])
        serve_mail(self.server, rows)
        self.client = McpClient(base, "tok", self.transport, label=label)
        part = mail.read(self.client, USER).data
        for message in part.needs_answer:
            outcome = (drafts or {}).get(message.id) or Draft("Hello,\n\nReply to %s\n\nTeam" % message.subject,
                                                              summary="s")
            part.drafts[message.id] = outcome
        missing = PartResult.missing("x")
        self.result = SystemResult(name, label, PartResult.complete(part), missing, missing)
        self.name = name

    def proposals(self):
        return approval.proposals([self.result])

    def creates(self):
        return self.server.created


def go(world, stdin_text="", stdin=None, editor=None, proposals=None, clients=None, **kw):
    lines = []
    summary = approval.run(world.proposals() if proposals is None else proposals,
                           clients or {world.name: world.client},
                           stdin if stdin is not None else io.StringIO(stdin_text),
                           editor or FakeEditor(), lines.append, **kw)
    return summary, "\n".join(lines), lines


class Proposals(unittest.TestCase):
    def test_every_valid_draft_is_a_proposal_oldest_first_per_system(self):
        w = World([question(3), question(1), question(2)])
        props = w.proposals()
        self.assertEqual([p.message.id for p in props], ["m1", "m2", "m3"])
        p = props[0]
        self.assertEqual((p.system, p.label), ("keystone", "Keystone"))
        self.assertIsInstance(p, Proposal)
        self.assertEqual(p.draft.reply, "Hello,\n\nReply to Question 1\n\nTeam")
        self.assertEqual((p.mailbox.id, p.mailbox.address), ("mb-1", "team28c@example.test"))

    def test_a_failed_draft_is_not_a_proposal(self):
        w = World([question(1), question(2)], drafts={"m1": DraftFailed("model unavailable")})
        self.assertEqual([p.message.id for p in w.proposals()], ["m2"])

    def test_systems_keep_the_order_of_the_results(self):
        a = World([question(1)], "suryodaya", "Suryodaya", BASE)
        b = World([question(2)], "keystone", "Keystone", OTHER_BASE)
        props = approval.proposals([a.result, b.result])
        self.assertEqual([(p.system, p.message.id) for p in props], [("suryodaya", "m1"), ("keystone", "m2")])
        props = approval.proposals([b.result, a.result])
        self.assertEqual([p.system for p in props], ["keystone", "suryodaya"])

    def test_a_system_whose_mail_part_is_missing_gives_none(self):
        w = World([question(1)])
        missing = PartResult.missing("Keystone: no mailbox for this login — ask the course team to set one up")
        gone = SystemResult("suryodaya", "Suryodaya", missing, missing, missing)
        self.assertEqual([p.system for p in approval.proposals([gone, w.result])], ["keystone"])

    def test_no_results_and_no_mail_give_no_proposals(self):
        self.assertEqual(approval.proposals([]), [])
        self.assertEqual(World([]).proposals(), [])


class NoProposals(unittest.TestCase):
    def test_it_says_so_reads_no_input_and_ends(self):
        """P-B-38."""
        stdin = TtyInput("s\n")
        summary, text, lines = go(World([]), stdin=stdin)
        self.assertEqual(lines, ["No drafts to approve"])
        self.assertEqual(stdin.reads, 0)
        self.assertEqual((summary.saved, summary.edited, summary.skipped, summary.not_saved, summary.failed),
                         (0, 0, 0, 0, []))


class TheOffer(unittest.TestCase):
    def test_each_reply_is_offered_with_sender_subject_text_and_the_four_choices(self):
        w = World([question(1), question(2)])
        _, text, lines = go(w, "\n\n")
        self.assertEqual(text.count(SAVE_PROMPT), 2)
        for needle in ("p1@example.test", "Question 1", "Reply to Question 1", "p2@example.test", "Question 2",
                       "Reply to Question 2", "1 of 2", "2 of 2", "Keystone"):
            self.assertIn(needle, text)
        self.assertLess(text.index("Question 1"), text.index("Question 2"))

    def test_the_summary_line_is_the_last_line(self):
        _, _, lines = go(World([question(1)]), "\n")
        self.assertEqual(lines[-1], "0 saved (0 edited), 1 skipped, 0 not saved")

    def test_enter_and_any_other_input_skip(self):
        w = World([question(1), question(2), question(3)])
        summary, _, _ = go(w, "\nhello\n  \n")
        self.assertEqual(w.creates(), [])
        self.assertEqual((summary.saved, summary.skipped, summary.not_saved), (0, 3, 0))

    def test_the_choice_ignores_case_and_spaces(self):
        w = World([question(1)])
        summary, _, _ = go(w, "  s \n")
        self.assertEqual(summary.saved, 1)


class Saving(unittest.TestCase):
    def test_s_saves_the_proposed_text_with_the_threading_fields(self):
        """P-B-39, P-B-40."""
        w = World([question(1, references="<old@mail.test>")])
        summary, text, _ = go(w, "s\n")
        (args,) = w.creates()
        self.assertEqual(args, {
            "company_id": "co-1", "mailbox_id": "mb-1", "from_email": "team28c@example.test",
            "to": "p1@example.test", "subject": "Re: Question 1",
            "body_text": "Hello,\n\nReply to Question 1\n\nTeam", "folder": "drafts", "is_draft": True,
            "thread_id": "th-m1", "in_reply_to": "<m1@mail.test>", "references": "<old@mail.test> <m1@mail.test>"})
        self.assertEqual((summary.saved, summary.edited, summary.skipped, summary.not_saved), (1, 0, 0, 0))
        self.assertLess(text.index("Saving draft…"), text.rindex("Saved"))
        self.assertLess(text.index("Saved"), text.index("1 saved (0 edited), 0 skipped, 0 not saved"))

    def test_saving_draft_shows_before_the_platform_is_written(self):
        w = World([question(1)])
        seen = []
        original = w.server._handlers["EmailMessage.create"]

        def spy(arguments):
            seen.append(True)
            return original(arguments)

        w.server.handler("EmailMessage.create", spy)
        lines = []
        approval.run(w.proposals(), {"keystone": w.client}, io.StringIO("s\n"), FakeEditor(),
                     lambda line: lines.append((line, bool(seen))))
        self.assertIn(("Saving draft…", False), lines)
        self.assertIn(("Saved", True), lines)

    def test_references_start_from_the_original_message_id_when_there_are_none(self):
        w = World([question(1)])
        go(w, "s\n")
        self.assertEqual(w.creates()[0]["references"], "<m1@mail.test>")

    def test_the_message_id_is_not_added_to_references_twice(self):
        w = World([question(1, references="<m1@mail.test>")])
        go(w, "s\n")
        self.assertEqual(w.creates()[0]["references"], "<m1@mail.test>")

    def test_a_subject_that_already_starts_with_re_is_not_prefixed_again(self):
        w = World([question(1, subject="RE: Hello"), question(2, subject="Hello"), question(3, subject="re:x")])
        go(w, "s\ns\ns\n")
        self.assertEqual([a["subject"] for a in w.creates()], ["RE: Hello", "Re: Hello", "re:x"])

    def test_the_recipient_is_the_senders_address_only(self):
        w = World([question(1, sender="Priya Patel <priya@example.test>")])
        go(w, "s\n")
        self.assertEqual(w.creates()[0]["to"], "priya@example.test")

    def test_empty_values_are_left_out_of_the_arguments(self):
        row = question(1)
        row["thread_id"] = None
        w = World([row])
        go(w, "s\n")
        self.assertNotIn("thread_id", w.creates()[0])

    def test_the_mailbox_comes_from_the_part_and_falls_back_on_the_rows_mailbox_id(self):
        w = World([question(1, mailbox_id="mb-row")], mailbox={"id": "mb-part", "address": "me@box.test"})
        go(w, "s\n")
        self.assertEqual((w.creates()[0]["mailbox_id"], w.creates()[0]["from_email"]), ("mb-part", "me@box.test"))
        w = World([question(1, mailbox_id="mb-row")], mailbox={"address": "me@box.test"})
        go(w, "s\n")
        self.assertEqual(w.creates()[0]["mailbox_id"], "mb-row")

    def test_a_missing_company_id_or_mailbox_address_is_not_saved_and_nothing_is_sent_to_the_platform(self):
        row = question(1)
        row["company_id"] = None
        w = World([row])
        summary, text, _ = go(w, "s\n")
        self.assertEqual(w.creates(), [])
        self.assertIn("Draft not saved: ", text)
        self.assertIn("company", text)
        self.assertEqual((summary.saved, summary.not_saved, len(summary.failed)), (0, 1, 1))
        w = World([question(1)], mailbox={"id": "mb-1"})
        summary, text, _ = go(w, "s\n")
        self.assertEqual(w.creates(), [])
        self.assertIn("mailbox address", text)
        self.assertEqual(summary.not_saved, 1)

    def test_each_save_uses_one_create_call(self):
        w = World([question(1), question(2)])
        go(w, "s\ns\n")
        self.assertEqual(len(w.transport.tool_calls("EmailMessage.create")), 2)
        self.assertLessEqual(w.transport.tool_names(), {"EmailMessage.get", "EmailMessage.create",
                                                        "Mailbox.list", "EmailAccount.list", "EmailMessage.list"})


class RecheckAndReadBack(unittest.TestCase):
    def test_the_original_is_read_again_just_before_the_create_and_the_new_draft_just_after(self):
        """P-B-41, SD-D-15."""
        w = World([question(1)])
        before = len(w.transport.calls)
        go(w, "s\n")
        later = [(c.tool, (c.arguments or {}).get("id")) for c in w.transport.calls[before:]]
        self.assertEqual(later, [("EmailMessage.get", "m1"), ("EmailMessage.create", None),
                                 ("EmailMessage.get", "new-1")])

    def test_an_answered_original_is_not_saved(self):
        w = World([question(1)])
        for row in w.server.mail_rows:
            row["is_answered"] = True
        summary, text, _ = go(w, "s\n")
        self.assertIn("Already answered — draft not saved", text)
        self.assertEqual(w.creates(), [])
        self.assertEqual((summary.saved, summary.not_saved), (0, 1))
        (failed,) = summary.failed
        self.assertEqual((failed.system, failed.message_id, failed.subject), ("keystone", "m1", "Question 1"))
        self.assertIn("answered", failed.reason)
        self.assertEqual(failed.text, "Hello,\n\nReply to Question 1\n\nTeam")

    def test_a_re_read_that_fails_is_not_saved(self):
        w = World([question(1)])
        w.server.fail("EmailMessage.get", ToolError("gone"))
        summary, text, _ = go(w, "s\n")
        self.assertIn("Draft not saved: ", text)
        self.assertEqual(w.creates(), [])
        self.assertEqual((summary.saved, summary.not_saved), (0, 1))

    def test_a_read_back_that_fails_says_check_the_drafts_folder_and_does_not_retry(self):
        w = World([question(1)])
        w.server.fail_when("EmailMessage.get", lambda a: str(a.get("id")).startswith("new-"), ToolError("nope"))
        summary, text, _ = go(w, "s\n")
        self.assertIn("Draft may not be saved — check your Drafts folder", text)
        self.assertEqual(len(w.creates()), 1)
        self.assertEqual(len(w.transport.tool_calls("EmailMessage.create")), 1)
        self.assertEqual((summary.saved, summary.not_saved), (0, 1))
        self.assertEqual(summary.failed[0].text, "Hello,\n\nReply to Question 1\n\nTeam")

    def test_a_read_back_row_that_is_not_a_draft_is_not_trusted(self):
        w = World([question(1)])
        original = w.server._handlers["EmailMessage.get"]

        def odd(arguments):
            row = dict(original(arguments))
            if arguments["id"].startswith("new-"):
                row.update(folder="outbox", is_draft=False)
            return row

        w.server.handler("EmailMessage.get", odd)
        summary, text, _ = go(w, "s\n")
        self.assertIn("Draft may not be saved — check your Drafts folder", text)
        self.assertEqual((summary.saved, summary.not_saved), (0, 1))

    def test_a_create_reply_without_an_id_cannot_be_read_back(self):
        w = World([question(1)])
        w.server.handler("EmailMessage.create", lambda a: {"ok": True})
        summary, text, _ = go(w, "s\n")
        self.assertIn("Draft may not be saved — check your Drafts folder", text)
        self.assertEqual(summary.not_saved, 1)

    def test_a_connection_failure_during_the_create_may_have_saved_it(self):
        w = World([question(1)])
        w.server.fail("EmailMessage.create", ConnectionResetError("reset"))
        summary, text, _ = go(w, "s\n")
        self.assertIn("Draft may not be saved — check your Drafts folder", text)
        self.assertEqual(len(w.transport.tool_calls("EmailMessage.create")), 1)
        self.assertEqual(summary.not_saved, 1)


class RefusedSave(unittest.TestCase):
    def test_a_refused_save_shows_the_message_and_the_approved_text_and_the_next_reply_is_offered(self):
        """P-B-42."""
        w = World([question(1), question(2)])
        w.server.fail_when("EmailMessage.create", lambda a: a["subject"] == "Re: Question 1",
                           ToolError("mailbox is read-only"))
        editor = FakeEditor("Edited text\nsecond line")
        summary, text, _ = go(w, "e\ns\n", stdin=TtyInput("e\ns\n"), editor=editor)
        self.assertIn("Draft not saved: mailbox is read-only — your text is shown below and kept in this run's "
                      "briefing file; save it by hand", text)
        self.assertIn("Edited text\nsecond line", text)
        self.assertEqual(text.count(SAVE_PROMPT), 2)
        self.assertEqual([a["subject"] for a in w.creates()], ["Re: Question 2"])
        self.assertEqual((summary.saved, summary.edited, summary.skipped, summary.not_saved), (1, 0, 0, 1))
        (failed,) = summary.failed
        self.assertEqual(failed.text, "Edited text\nsecond line")
        self.assertIn("mailbox is read-only", failed.reason)
        self.assertEqual((failed.label, failed.subject), ("Keystone", "Question 1"))

    def test_the_text_is_shown_after_the_failure_line(self):
        w = World([question(1)])
        w.server.fail("EmailMessage.create", ToolError("refused"))
        _, text, _ = go(w, "s\n")
        self.assertLess(text.index("Draft not saved: refused"), text.rindex("Reply to Question 1"))

    def test_an_http_failure_on_create_is_a_refused_save(self):
        w = World([question(1)])
        w.server.fail("EmailMessage.create", HttpStatus(500, "boom"))
        summary, text, _ = go(w, "s\n")
        self.assertIn("Draft not saved: HTTP 500", text)
        self.assertEqual((summary.saved, summary.not_saved), (0, 1))


class EditThenSave(unittest.TestCase):
    def test_the_editor_gets_the_proposed_reply_and_its_result_is_saved(self):
        w = World([question(1)])
        editor = FakeEditor("A better reply")
        summary, _, _ = go(w, stdin=TtyInput("e\n"), editor=editor)
        self.assertEqual(editor.texts, ["Hello,\n\nReply to Question 1\n\nTeam"])
        self.assertEqual(w.creates()[0]["body_text"], "A better reply")
        self.assertEqual((summary.saved, summary.edited), (1, 1))

    def test_an_edit_that_changes_nothing_is_a_plain_save(self):
        w = World([question(1)])
        summary, _, _ = go(w, stdin=TtyInput("e\n"), editor=FakeEditor())   # returns the text unchanged
        self.assertEqual((summary.saved, summary.edited), (1, 0))

    def test_an_empty_or_blank_edit_is_a_skip(self):
        w = World([question(1), question(2)])
        summary, text, _ = go(w, stdin=TtyInput("e\ne\n"), editor=FakeEditor("", " \n\t\n"))
        self.assertEqual(w.creates(), [])
        self.assertEqual((summary.saved, summary.skipped, summary.not_saved), (0, 2, 0))

    def test_without_a_terminal_e_is_a_skip_and_the_editor_is_not_opened(self):
        """P-B-44."""
        w = World([question(1)])
        editor = FakeEditor("never")
        summary, text, _ = go(w, "e\n", editor=editor)
        self.assertEqual(editor.texts, [])
        self.assertEqual(w.creates(), [])
        self.assertIn("terminal", text)
        self.assertEqual((summary.saved, summary.skipped), (0, 1))

    def test_a_missing_editor_falls_back_on_typed_lines_ended_by_a_full_stop(self):
        w = World([question(1)])
        summary, text, _ = go(w, stdin=TtyInput("e\nFirst typed line\nSecond line\n.\n"), editor=FakeEditor("missing"))
        self.assertEqual(w.creates()[0]["body_text"], "First typed line\nSecond line")
        self.assertEqual((summary.saved, summary.edited), (1, 1))
        self.assertIn("full stop", text)

    def test_typed_lines_end_at_the_end_of_input_and_nothing_typed_is_a_skip(self):
        w = World([question(1)])
        summary, _, _ = go(w, stdin=TtyInput("e\nOnly line\n"), editor=FakeEditor("missing"))
        self.assertEqual(w.creates()[0]["body_text"], "Only line")
        w = World([question(1)])
        summary, _, _ = go(w, stdin=TtyInput("e\n.\n"), editor=FakeEditor("missing"))
        self.assertEqual((w.creates(), summary.skipped), ([], 1))

    def test_ctrl_c_in_the_editor_stops(self):
        w = World([question(1), question(2)])
        summary, text, _ = go(w, stdin=TtyInput("e\n"), editor=FakeEditor(KeyboardInterrupt()))
        self.assertEqual(w.creates(), [])
        self.assertEqual((summary.saved, summary.skipped, summary.not_saved), (0, 0, 2))


class StopAndEnd(unittest.TestCase):
    def test_q_stops_and_the_reply_it_was_chosen_on_and_the_rest_are_not_saved(self):
        """P-B-42, Story 5 Scenario 4."""
        w = World([question(i) for i in range(1, 6)])
        summary, text, _ = go(w, "s\nq\n")
        self.assertEqual(len(w.creates()), 1)
        self.assertEqual(text.count(SAVE_PROMPT), 2)
        self.assertEqual((summary.saved, summary.edited, summary.skipped, summary.not_saved), (1, 0, 0, 4))
        self.assertIn("1 saved (0 edited), 0 skipped, 4 not saved", text)

    def test_skips_before_a_stop_stay_skips(self):
        w = World([question(1), question(2), question(3)])
        summary, _, _ = go(w, "\nq\n")
        self.assertEqual((summary.skipped, summary.not_saved), (1, 2))

    def test_end_of_input_skips_everything_and_creates_nothing(self):
        """P-B-44."""
        w = World([question(1), question(2), question(3)])
        summary, text, lines = go(w, "")
        self.assertEqual(w.transport.tool_calls("EmailMessage.create"), [])
        self.assertEqual((summary.saved, summary.edited, summary.skipped, summary.not_saved), (0, 0, 3, 0))
        self.assertEqual(lines[-1], "0 saved (0 edited), 3 skipped, 0 not saved")
        self.assertEqual(text.count(SAVE_PROMPT), 1)   # the end of input is noticed once; no further offers

    def test_a_closed_or_broken_stdin_is_the_end_of_input(self):
        """A harness may close stdin: the briefing is already out, so this must skip, not crash."""
        closed = io.StringIO("s\n")
        closed.close()
        w = World([question(1), question(2)])
        summary, _, lines = go(w, stdin=closed)
        self.assertEqual((w.creates(), summary.skipped, summary.not_saved), ([], 2, 0))

        class Broken:
            def readline(self):
                raise OSError("bad file descriptor")

            def isatty(self):
                return False

        summary, _, _ = go(World([question(1)]), stdin=Broken())
        self.assertEqual(summary.skipped, 1)

    def test_a_piped_s_saves_one_and_the_end_of_input_skips_the_rest(self):
        """P-B-44."""
        w = World([question(1), question(2)])
        summary, _, _ = go(w, "s\n")
        self.assertEqual(len(w.creates()), 1)
        self.assertEqual((summary.saved, summary.skipped, summary.not_saved), (1, 1, 0))

    def test_ctrl_c_at_the_prompt_counts_as_stop_and_the_summary_is_still_returned(self):
        """P-B-43."""
        w = World([question(1), question(2), question(3)])
        stdin = TtyInput("s\n", before_read=lambda n: (_ for _ in ()).throw(KeyboardInterrupt()) if n == 2 else None)
        summary, text, lines = go(w, stdin=stdin)
        self.assertEqual(len(w.creates()), 1)
        self.assertEqual((summary.saved, summary.skipped, summary.not_saved), (1, 0, 2))
        self.assertEqual(lines[-1], "1 saved (0 edited), 0 skipped, 2 not saved")

    def test_ctrl_c_while_saving_counts_that_reply_as_not_saved_and_stops(self):
        w = World([question(1), question(2)])
        w.server.fail("EmailMessage.create", KeyboardInterrupt())
        summary, text, _ = go(w, "s\ns\n")
        self.assertEqual((summary.saved, summary.not_saved), (0, 2))
        self.assertEqual(len(summary.failed), 1)
        self.assertIn("Drafts folder", text)


class LoginExpired(unittest.TestCase):
    """P-B-43: a system's offers stop, the other system's go on."""

    def two(self):
        keystone = World([question(1), question(2)], "keystone", "Keystone", BASE)
        surya = World([question(3, subject="Surya question")], "suryodaya", "Suryodaya", OTHER_BASE)
        clients = {"keystone": keystone.client, "suryodaya": surya.client}
        return keystone, surya, clients, keystone.proposals() + surya.proposals()

    def test_keystone_stops_and_suryodaya_is_still_offered(self):
        keystone, surya, clients, props = self.two()
        keystone.server.expire()
        summary, text, _ = go(keystone, "s\ns\n", proposals=props, clients=clients)
        self.assertIn("Keystone: login expired — remaining drafts not saved; log in again and rerun", text)
        self.assertEqual(len(surya.creates()), 1)
        self.assertEqual(text.count(SAVE_PROMPT), 2)     # Keystone's first, then Suryodaya's; Keystone's second never
        self.assertEqual((summary.saved, summary.skipped, summary.not_saved), (1, 0, 2))
        self.assertEqual([f.message_id for f in summary.failed], ["m1"])

    def test_the_message_comes_once(self):
        keystone, surya, clients, props = self.two()
        keystone.server.expire()
        _, text, _ = go(keystone, "s\ns\n", proposals=props, clients=clients)
        self.assertEqual(text.count("login expired"), 1)

    def test_an_expiry_at_the_create_keeps_the_approved_text(self):
        keystone, surya, clients, props = self.two()
        original = keystone.server._handlers["EmailMessage.create"]
        keystone.server.fail("EmailMessage.create", HttpStatus(401, "unauthorized"))
        summary, text, _ = go(keystone, "s\ns\n", proposals=props, clients=clients)
        self.assertIn("login expired", text)
        self.assertEqual(summary.failed[0].text, "Hello,\n\nReply to Question 1\n\nTeam")
        self.assertEqual(len(surya.creates()), 1)


class FailureKinds(unittest.TestCase):
    """A1: every failed save says what kind it was, so the file and the JSON can word it right."""

    def kinds(self, summary):
        return [f.kind for f in summary.failed]

    def test_a_refused_create_a_failed_re_read_and_a_missing_company_are_refused(self):
        w = World([question(1)])
        w.server.fail("EmailMessage.create", ToolError("refused"))
        self.assertEqual(self.kinds(go(w, "s\n")[0]), ["refused"])
        w = World([question(1)])
        w.server.fail("EmailMessage.get", ToolError("gone"))
        self.assertEqual(self.kinds(go(w, "s\n")[0]), ["refused"])
        row = question(1)
        row["company_id"] = None
        self.assertEqual(self.kinds(go(World([row]), "s\n")[0]), ["refused"])

    def test_an_answered_original_is_answered(self):
        w = World([question(1)])
        for row in w.server.mail_rows:
            row["is_answered"] = True
        self.assertEqual(self.kinds(go(w, "s\n")[0]), ["answered"])

    def test_a_connection_failure_a_failed_read_back_and_a_create_without_an_id_are_unconfirmed(self):
        w = World([question(1)])
        w.server.fail("EmailMessage.create", ConnectionResetError("reset"))
        self.assertEqual(self.kinds(go(w, "s\n")[0]), ["unconfirmed"])
        w = World([question(1)])
        w.server.fail_when("EmailMessage.get", lambda a: str(a.get("id")).startswith("new-"), ToolError("nope"))
        self.assertEqual(self.kinds(go(w, "s\n")[0]), ["unconfirmed"])
        w = World([question(1)])
        w.server.handler("EmailMessage.create", lambda a: {"ok": True})
        self.assertEqual(self.kinds(go(w, "s\n")[0]), ["unconfirmed"])

    def test_a_login_that_expires_before_the_create_is_expired_and_at_the_read_back_unconfirmed(self):
        w = World([question(1)])
        w.server.fail("EmailMessage.create", HttpStatus(401, "unauthorized"))
        self.assertEqual(self.kinds(go(w, "s\n")[0]), ["expired"])
        w = World([question(1)])
        w.server.fail_when("EmailMessage.get", lambda a: str(a.get("id")).startswith("new-"),
                           HttpStatus(401, "unauthorized"))
        summary, text, _ = go(w, "s\n")
        self.assertEqual(self.kinds(summary), ["unconfirmed"])
        self.assertIn("Draft may not be saved — check your Drafts folder", text)
        self.assertIn("login expired", text)

    def test_ctrl_c_while_saving_is_interrupted(self):
        w = World([question(1)])
        w.server.fail("EmailMessage.create", KeyboardInterrupt())
        self.assertEqual(self.kinds(go(w, "s\n")[0]), ["interrupted"])


class NoRetryOnLoginExpiredAtReadBack(unittest.TestCase):
    def two(self):
        keystone = World([question(1), question(2)], "keystone", "Keystone", BASE)
        surya = World([question(3, subject="Surya question")], "suryodaya", "Suryodaya", OTHER_BASE)
        clients = {"keystone": keystone.client, "suryodaya": surya.client}
        return keystone, surya, clients, keystone.proposals() + surya.proposals()

    def test_keystones_offers_stop_when_the_login_expires_at_the_read_back_and_suryodaya_is_still_offered(self):
        keystone, surya, clients, props = self.two()
        keystone.server.fail_when("EmailMessage.get", lambda a: str(a.get("id")).startswith("new-"),
                                  HttpStatus(401, "unauthorized"))
        summary, text, _ = go(keystone, "s\ns\ns\n", proposals=props, clients=clients)
        self.assertEqual(len(keystone.creates()), 1)           # the second Keystone reply is never offered
        self.assertEqual(len(surya.creates()), 1)
        self.assertEqual(text.count(SAVE_PROMPT), 2)
        self.assertEqual((summary.saved, summary.skipped, summary.not_saved), (1, 0, 2))
        self.assertIn("Keystone: login expired — remaining drafts not saved; log in again and rerun", text)


class UnexpectedErrors(unittest.TestCase):
    """B3: an error nobody planned for costs that one reply, never the summary, the file or the JSON."""

    def test_an_editor_that_raises_counts_that_reply_as_not_saved_and_the_next_is_offered(self):
        w = World([question(1), question(2)])
        summary, text, lines = go(w, stdin=TtyInput("e\ns\n"), editor=FakeEditor(ValueError()))
        self.assertEqual([a["subject"] for a in w.creates()], ["Re: Question 2"])
        self.assertEqual((summary.saved, summary.skipped, summary.not_saved), (1, 0, 1))
        (failed,) = summary.failed
        self.assertEqual((failed.message_id, failed.reason), ("m1", "unexpected error: ValueError"))
        self.assertEqual(failed.text, "Hello,\n\nReply to Question 1\n\nTeam")
        self.assertIn("unexpected error: ValueError", text)
        self.assertEqual(lines[-1], "1 saved (0 edited), 0 skipped, 1 not saved")

    def test_an_error_while_saving_is_unconfirmed_and_keeps_the_approved_text(self):
        w = World([question(1)])
        w.server.fail("EmailMessage.create", RuntimeError("odd"))
        summary, text, _ = go(w, "s\n")
        self.assertEqual((summary.saved, summary.not_saved), (0, 1))
        (failed,) = summary.failed
        self.assertEqual((failed.kind, failed.reason), ("unconfirmed", "unexpected error: RuntimeError"))
        self.assertEqual(failed.text, "Hello,\n\nReply to Question 1\n\nTeam")
        self.assertIn("check your Drafts folder", text)

    def test_the_editor_file_is_read_with_undecodable_bytes_replaced(self):
        def run(argv):
            with open(argv[-1], "wb") as f:
                f.write(b"caf\xe9 reply")
            return type("R", (), {"returncode": 0})()

        editor = approval.external_editor({"EDITOR": "vim"}, run=run, which=lambda n: "/bin/" + n)
        self.assertEqual(editor("x"), "caf\ufffd reply")

    def test_a_failure_to_make_the_temp_file_is_one_unexpected_error_not_a_crash(self):
        with mock.patch("ea_briefing.approval.tempfile.mkstemp", side_effect=OSError("no space")):
            editor = approval.external_editor({"EDITOR": "vim"}, run=lambda argv: None, which=lambda n: "/bin/" + n)
            with self.assertRaises(OSError):
                editor("x")
        w = World([question(1)])
        with mock.patch("ea_briefing.approval.tempfile.mkstemp", side_effect=OSError("no space")):
            summary, _, _ = go(w, stdin=TtyInput("e\n"), editor=approval.external_editor(
                {"EDITOR": "vim"}, run=lambda argv: None, which=lambda n: "/bin/" + n))
        self.assertEqual((summary.saved, summary.not_saved), (0, 1))
        self.assertEqual(summary.failed[0].reason, "unexpected error: OSError")


class TrailingNewline(unittest.TestCase):
    """B4: editors add a final newline; that is not an edit."""

    def test_an_edit_that_only_adds_a_newline_is_a_plain_save_of_the_original_text(self):
        w = World([question(1)])
        reply = "Hello,\n\nReply to Question 1\n\nTeam"
        summary, _, _ = go(w, stdin=TtyInput("e\n"), editor=FakeEditor(reply + "\n"))
        self.assertEqual((summary.saved, summary.edited), (1, 0))
        self.assertEqual(w.creates()[0]["body_text"], reply)

    def test_typed_lines_and_real_edits_lose_their_trailing_whitespace_too(self):
        w = World([question(1)])
        summary, _, _ = go(w, stdin=TtyInput("e\n"), editor=FakeEditor("Changed reply \n\n"))
        self.assertEqual((summary.edited, w.creates()[0]["body_text"]), (1, "Changed reply"))
        w = World([question(1)])
        go(w, stdin=TtyInput("e\nTyped\n\n\n.\n"), editor=FakeEditor("missing"))
        self.assertEqual(w.creates()[0]["body_text"], "Typed")


class FileKeptWording(unittest.TestCase):
    """B8: the failure line says "kept in this run's briefing file" only when the file will be rewritten."""

    def test_the_default_says_the_file_keeps_the_text(self):
        w = World([question(1)])
        w.server.fail("EmailMessage.create", ToolError("refused"))
        _, text, _ = go(w, "s\n")
        self.assertIn("kept in this run's briefing file", text)

    def test_when_the_file_will_not_be_rewritten_the_line_does_not_say_so(self):
        w = World([question(1)])
        w.server.fail("EmailMessage.create", ToolError("refused"))
        _, text, _ = go(w, "s\n", file_kept=False)
        self.assertIn("Draft not saved: refused — your text is shown below; save it by hand", text)
        self.assertNotIn("briefing file", text)


class SummaryText(unittest.TestCase):
    def test_the_line(self):
        s = ApprovalSummary(saved=2, edited=1, skipped=0, not_saved=3)
        self.assertEqual(s.line(), "2 saved (1 edited), 0 skipped, 3 not saved")
        self.assertEqual(s.total, 5)
        self.assertEqual(ApprovalSummary().total, 0)

    def test_a_summary_starts_empty_and_each_has_its_own_failed_list(self):
        a, b = ApprovalSummary(), ApprovalSummary()
        a.failed.append(1)
        self.assertEqual(b.failed, [])


class ExternalEditor(unittest.TestCase):
    """P-B-39: $EDITOR, then nano, then typed lines."""

    def make(self, env, present, edit=lambda path: None, code=0):
        runs = []

        def which(name):
            return "/bin/" + name if name in present else None

        def run(argv):
            runs.append(argv)
            edit(argv[-1])
            return type("R", (), {"returncode": code})()

        return approval.external_editor(env, run=run, which=which), runs

    def test_editor_variable_is_used_first_with_its_arguments(self):
        def edit(path):
            with open(path, encoding="utf-8") as f:
                self.assertEqual(f.read(), "original")
            with open(path, "w", encoding="utf-8") as f:
                f.write("changed")

        editor, runs = self.make({"EDITOR": "vim -u NONE"}, {"vim", "nano"}, edit)
        self.assertEqual(editor("original"), "changed")
        self.assertEqual(runs[0][:3], ["vim", "-u", "NONE"])

    def test_nano_when_there_is_no_editor_variable_or_it_is_not_installed(self):
        for env in ({}, {"EDITOR": "nosuchedit"}, {"EDITOR": ""}):
            editor, runs = self.make(env, {"nano"})
            self.assertEqual(editor("same"), "same")
            self.assertEqual(runs[0][0], "nano", env)

    def test_neither_available_raises_editor_unavailable(self):
        editor, runs = self.make({"EDITOR": "vim"}, set())
        with self.assertRaises(EditorUnavailable):
            editor("x")
        self.assertEqual(runs, [])

    def test_an_editor_that_cannot_start_hands_over_to_the_next(self):
        calls = []

        def run(argv):
            calls.append(argv[0])
            if argv[0] == "vim":
                raise OSError("exec failed")
            return type("R", (), {"returncode": 0})()

        editor = approval.external_editor({"EDITOR": "vim"}, run=run, which=lambda n: "/bin/" + n)
        self.assertEqual(editor("same"), "same")
        self.assertEqual(calls, ["vim", "nano"])

    def test_an_editor_that_exits_with_an_error_means_nothing_to_save(self):
        editor, _ = self.make({"EDITOR": "vim"}, {"vim"}, code=1)
        self.assertEqual(editor("same"), "")

    def test_the_temporary_file_is_removed(self):
        paths = []
        editor, _ = self.make({"EDITOR": "vim"}, {"vim"}, lambda path: paths.append(path))
        editor("x")
        import os
        self.assertFalse(os.path.exists(paths[0]))


if __name__ == "__main__":
    unittest.main()
