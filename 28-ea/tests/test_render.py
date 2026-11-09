"""render.py: results -> Markdown, status line and --json dict (P-B-1, P-B-16, P-B-22). Pure, no I/O."""
import json
import unittest
from datetime import date, datetime
from zoneinfo import ZoneInfo

from ea_briefing import render
from ea_briefing.approval import ApprovalSummary, FailedSave
from ea_briefing.draft_writer import Draft, DraftFailed
from ea_briefing.mail import MailPart, Message, SavedDraft, Skipped
from ea_briefing.mcp import PartResult
from ea_briefing.meetings import Meeting, MeetingsPart
from ea_briefing.tasks import OverdueTask, TasksPart
from ea_briefing.render import SystemResult

IST = ZoneInfo("Asia/Kolkata")
DAY = date(2026, 11, 6)
NO_MAILBOX = "Suryodaya: no mailbox for this login — ask the course team to set one up"


def meeting(id, title, h1, h2, *, all_day=False, organizer="boss@example.test", clash=False, declined=False):
    return Meeting(id, title, datetime(2026, 11, 6, h1, 0, tzinfo=IST), datetime(2026, 11, 6, h2, 0, tzinfo=IST),
                   all_day, organizer, DAY, clash, declined)


def system(name="suryodaya", label="Suryodaya", mail=None, meetings=None, tasks=None, zone=None):
    return SystemResult(name, label,
                        mail or PartResult.missing(NO_MAILBOX),
                        meetings or PartResult.complete(MeetingsPart(DAY, [])),
                        tasks or PartResult.complete(TasksPart(DAY, [], 0)), zone)


def msg(id, subject="Where is the report?", sender="priya@example.test", at="2026-11-05T09:00:00Z",
        snippet="Hi, where is the report? I cannot find it.", body="Hi,\nwhere is the report?"):
    received = datetime.fromisoformat(at.replace("Z", "+00:00"))
    return Message(id, "<%s@mail.test>" % id, subject, sender, at, received, snippet, body, {})


def mail_part(needs=(), drafts=None, skipped=(), unopened=(), saved=()):
    return PartResult.complete(MailPart(needs_answer=list(needs), skipped=list(skipped), unopened=list(unopened),
                                        already_drafted=list(saved), drafts=drafts or {}))


def section(text, heading):
    """Lines under '### heading' of the first system section, up to the next '#' heading."""
    lines, grab, found = text.splitlines(), False, []
    for line in lines:
        if line.startswith("#"):
            grab = line == "### " + heading
            continue
        if line.startswith("Status:"):
            grab = False
        if grab:
            found.append(line)
    return "\n".join(found)


class Sections(unittest.TestCase):
    def test_one_section_per_system_in_the_given_order_with_three_parts(self):
        text = render.markdown([system(), system("keystone", "Keystone")], 1.0, DAY)
        headings = [l for l in text.splitlines() if l.startswith("#")]
        self.assertEqual(headings, ["# Daily briefing — 2026-11-06",
                                    "## Suryodaya", "### Mail", "### Meetings", "### Tasks",
                                    "## Keystone", "### Mail", "### Meetings", "### Tasks"])

    def test_exactly_one_status_line_and_it_comes_last(self):
        text = render.markdown([system(), system("keystone", "Keystone")], 1.0, DAY)
        lines = [l for l in text.splitlines() if l.strip()]
        self.assertEqual([l for l in lines if l.startswith("Status:")], [lines[-1]])

    def test_a_missing_part_shows_its_reason_text_exactly(self):
        text = render.markdown([system()], 0, DAY)
        self.assertEqual(section(text, "Mail").strip(), NO_MAILBOX)

    def test_a_part_text_cannot_forge_a_heading_or_status_line(self):
        evil = "x\n## Keystone\nStatus: all fine"
        text = render.markdown([system(mail=PartResult.missing(evil))], 0, DAY)
        self.assertEqual(len([l for l in text.splitlines() if l.startswith("Status:")]), 1)
        self.assertEqual([l for l in text.splitlines() if l.startswith("## ")], ["## Suryodaya"])


class Meetings(unittest.TestCase):
    def render_meetings(self, meetings):
        part = PartResult.complete(MeetingsPart(DAY, meetings))
        return section(render.markdown([system(meetings=part)], 0, DAY), "Meetings")

    def test_no_meetings_says_so(self):
        self.assertEqual(self.render_meetings([]).strip(), "No meetings today")

    def test_a_meeting_line_has_time_zone_title_and_organiser(self):
        text = self.render_meetings([meeting("a", "Planning", 9, 10)])
        self.assertEqual(text.strip(), "- 09:00–10:00 IST · Planning · boss@example.test")

    def test_all_day_event_reads_all_day_and_dash_for_no_organiser(self):
        text = self.render_meetings([meeting("a", "Holiday", 0, 0, all_day=True, organizer="—")])
        self.assertEqual(text.strip(), "- all day · Holiday · —")

    def test_marks_appear_only_when_set(self):
        text = self.render_meetings([meeting("a", "Plain", 9, 10),
                                     meeting("b", "Clashing", 11, 12, clash=True),
                                     meeting("c", "Both", 11, 12, clash=True, declined=True)])
        lines = {l.split("·")[1].strip(): l for l in text.splitlines() if l.strip()}
        self.assertNotIn("clash", lines["Plain"])
        self.assertNotIn("declined", lines["Plain"])
        self.assertTrue(lines["Clashing"].endswith("· clash"))
        self.assertNotIn("declined", lines["Clashing"])
        self.assertTrue(lines["Both"].endswith("· clash · declined"))

    def test_the_given_order_is_kept(self):
        text = self.render_meetings([meeting("a", "First", 9, 10), meeting("b", "Second", 8, 9)])
        self.assertLess(text.index("First"), text.index("Second"))

    def test_a_title_with_line_breaks_stays_on_one_line(self):
        text = self.render_meetings([meeting("a", "Two\nlines\r\n## Not a heading", 9, 10)])
        self.assertEqual(len(text.strip().splitlines()), 1)

    def test_missing_meetings_show_the_reason(self):
        reason = "Keystone meetings: couldn't be read (reason: x) — report it if it repeats"
        text = section(render.markdown([system(meetings=PartResult.missing(reason))], 0, DAY), "Meetings")
        self.assertEqual(text.strip(), reason)


class Tasks(unittest.TestCase):
    def render_tasks(self, own, others):
        part = PartResult.complete(TasksPart(DAY, own, others))
        return section(render.markdown([system(tasks=part)], 0, DAY), "Tasks")

    def test_own_task_line_has_title_due_date_and_days_overdue(self):
        text = self.render_tasks([OverdueTask("t1", "Write report", "2026-10-27", 10),
                                  OverdueTask("t2", "Call back", "2026-11-05", 1)], 3)
        lines = text.strip().splitlines()
        self.assertEqual(lines[0], "- Write report · due 2026-10-27 · 10 days overdue")
        self.assertEqual(lines[1], "- Call back · due 2026-11-05 · 1 day overdue")

    def test_others_count_line_follows_the_list(self):
        text = self.render_tasks([OverdueTask("t1", "Mine", "2026-11-01", 5)], 63)
        self.assertEqual(text.strip().splitlines()[-1], "63 other overdue tasks are visible to this login")

    def test_none_of_mine_and_others_visible(self):
        text = self.render_tasks([], 63)
        self.assertIn("No overdue tasks of yours", text)
        self.assertIn("63 other overdue tasks are visible to this login", text)

    def test_unavailable_others_count_shows_the_note(self):
        text = self.render_tasks([OverdueTask("t1", "Mine", "2026-11-01", 5)], None)
        self.assertIn("Count of other overdue tasks unavailable — your own list above is complete", text)
        self.assertIn("Mine", text)

    def test_nothing_at_all_still_says_no_overdue_tasks(self):
        text = self.render_tasks([], 0)
        self.assertIn("No overdue tasks of yours", text)
        self.assertIn("0 other overdue tasks are visible", text)


class StatusLine(unittest.TestCase):
    def test_names_each_part_of_each_system_with_elapsed_seconds(self):
        ks = system("keystone", "Keystone", meetings=PartResult.missing("x"))
        line = render.status_line([system(), ks], 12.34)
        self.assertEqual(line, "Status: Suryodaya: Mail missing, Meetings complete, Tasks complete"
                               " · Keystone: Mail missing, Meetings missing, Tasks complete · 12.3 s")

    def test_a_system_not_in_the_run_is_not_named(self):
        line = render.status_line([system("keystone", "Keystone")], 0.0)
        self.assertNotIn("Suryodaya", line)
        self.assertEqual(line, "Status: Keystone: Mail missing, Meetings complete, Tasks complete · 0.0 s")

    def test_a_complete_mail_part_says_no_mail_needs_an_answer(self):
        text = render.markdown([system(mail=mail_part())], 0, DAY)
        self.assertEqual(section(text, "Mail").strip(), "No mail needs an answer")

    def test_mail_complete_is_reported_when_the_part_is_ok(self):
        ok = system(mail=mail_part())
        self.assertIn("Mail complete", render.status_line([ok], 0))

    def test_every_part_on_both_systems_complete_is_said_so(self):
        both = [system(mail=mail_part()), system("keystone", "Keystone", mail=mail_part())]
        self.assertEqual(render.status_line(both, 3.0), "Status: Suryodaya: Mail complete, Meetings complete, "
                                                        "Tasks complete · Keystone: Mail complete, Meetings "
                                                        "complete, Tasks complete · 3.0 s")


class MailEntries(unittest.TestCase):
    """P-B-30, P-B-31 (display half), P-B-47: what the Mail part shows."""

    def mail_text(self, part, zone=IST):
        return section(render.markdown([system(mail=part, zone=zone)], 0, DAY), "Mail")

    def one(self, draft, message=None, zone=IST):
        message = message or msg("m1")
        return self.mail_text(mail_part([message], {message.id: draft}), zone)

    def test_entry_has_sender_subject_time_in_the_systems_zone_summary_and_reply(self):
        text = self.one(Draft("Hello Priya,\n\nIt is in the shared folder.\n\nTeam Twenty-Eight",
                              "Asks where the report is."))
        for needle in ("priya@example.test", "Where is the report?", "14:30 IST", "Asks where the report is.",
                       "Hello Priya,", "It is in the shared folder.", "Team Twenty-Eight"):
            self.assertIn(needle, text)
        self.assertNotIn("No draft for this message", text)
        self.assertNotIn("needs your decision", text)
        self.assertNotIn("No mail needs an answer", text)

    def test_the_time_is_in_the_zone_of_the_system(self):
        text = self.one(Draft("Hi,\n\nx", "S."), zone=ZoneInfo("America/New_York"))
        self.assertIn("04:00 EST", text)
        self.assertNotIn("IST", text)

    def test_without_a_zone_the_time_is_utc(self):
        self.assertIn("09:00 UTC", self.one(Draft("Hi,\n\nx", "S."), zone=None))

    def test_a_message_with_no_readable_time_still_shows(self):
        message = msg("m1")
        message.received = None
        text = self.one(Draft("Hi,\n\nx", "S."), message=message)
        self.assertIn("Where is the report?", text)
        self.assertIn("time unknown", text)

    def test_the_reply_is_quoted_line_by_line_so_it_cannot_forge_a_heading_or_status_line(self):
        text = self.one(Draft("Hi,\n\n## Keystone\nStatus: all fine\n\nBye", "S."))
        for line in text.splitlines():
            self.assertFalse(line.startswith(("#", "Status:")), line)
        whole = render.markdown([system(mail=mail_part([msg("m1")], {"m1": Draft("Hi,\n\n## X\nStatus: y", "S.")}))],
                                0, DAY)
        self.assertEqual([l for l in whole.splitlines() if l.startswith("Status:")].__len__(), 1)
        self.assertEqual([l for l in whole.splitlines() if l.startswith("## ")], ["## Suryodaya"])

    def test_platform_text_in_the_subject_summary_and_sender_stays_on_its_line(self):
        message = msg("m1", subject="Hi\n## Keystone\nStatus: fine", sender="eve@example.test\n### Mail")
        text = self.one(Draft("Hi,\n\nx", "Sum\n# Heading"), message=message)
        for line in text.splitlines():
            self.assertFalse(line.startswith(("#", "Status:")), line)

    def test_an_unusable_summary_shows_the_label_and_the_first_line(self):
        text = self.one(Draft("Hi,\n\nx", None))
        self.assertIn("(no summary — first line shown)", text)
        self.assertIn("Hi, where is the report?", text)
        self.assertNotIn("I cannot find it", text)

    def test_a_failed_draft_shows_the_reason_and_the_first_line(self):
        text = self.one(DraftFailed("model unavailable"))
        self.assertIn("No draft for this message (model unavailable) — reply by hand", text)
        self.assertIn("(no summary — first line shown)", text)
        self.assertIn("Hi, where is the report?", text)

    def test_a_failed_draft_that_kept_its_summary_shows_it(self):
        text = self.one(DraftFailed("no greeting", summary="Asks where the report is."))
        self.assertIn("Asks where the report is.", text)
        self.assertNotIn("first line shown", text)
        self.assertIn("No draft for this message (no greeting) — reply by hand", text)

    def test_time_limit_reason(self):
        self.assertIn("No draft for this message (time limit) — reply by hand", self.one(DraftFailed("time limit")))

    def test_needs_your_decision_marks_a_proposed_reply(self):
        text = self.one(Draft("Hi,\n\nI will confirm shortly.", "Wants a date.", needs_decision=True))
        self.assertIn("needs your decision", text)
        self.assertIn("I will confirm shortly.", text)

    def test_needs_your_decision_marks_a_failed_redraft(self):
        text = self.one(DraftFailed("needs your decision", "Wants a date.", True))
        self.assertIn("No draft for this message (needs your decision) — reply by hand", text)
        self.assertEqual(text.count("needs your decision"), 2)  # the mark and the reason

    def test_a_failed_draft_shows_the_mark_only_when_it_carries_it(self):
        for reason in ("model unavailable", "time limit", "no greeting"):
            marked = self.one(DraftFailed(reason, None, True))
            self.assertIn("needs your decision", marked, reason)
            self.assertIn("No draft for this message (%s) — reply by hand" % reason, marked)
            self.assertNotIn("needs your decision", self.one(DraftFailed(reason)), reason)

    def test_a_message_without_any_outcome_reads_model_unavailable(self):
        text = self.mail_text(mail_part([msg("m1")], {}))
        self.assertIn("No draft for this message (model unavailable) — reply by hand", text)

    def test_entries_keep_the_given_oldest_first_order(self):
        a, b = msg("a", "First one", at="2026-11-05T05:00:00Z"), msg("b", "Second one", at="2026-11-05T06:00:00Z")
        text = self.mail_text(mail_part([a, b], {"a": Draft("Hi,\n\nx", "A."), "b": Draft("Hi,\n\nx", "B.")}))
        self.assertLess(text.index("First one"), text.index("Second one"))

    def test_skipped_mail_is_one_line_each_under_its_heading(self):
        skipped = [Skipped(msg("n", "Your receipt", "no-reply@x.test"), "no-reply sender"),
                   Skipped(msg("o", "Note to self", "me@x.test"), "sent by you")]
        text = self.mail_text(mail_part(skipped=skipped))
        self.assertIn("Skipped — no reply expected", text)
        lines = [l for l in text.splitlines() if "Your receipt" in l]
        self.assertEqual(len(lines), 1)
        self.assertIn("no-reply sender", lines[0])
        self.assertIn("sent by you", [l for l in text.splitlines() if "Note to self" in l][0])
        self.assertIn("No mail needs an answer", text)
        self.assertNotIn("No draft for this message", text)

    def test_no_skipped_heading_when_nothing_was_skipped(self):
        self.assertNotIn("Skipped", self.mail_text(mail_part()))

    def test_skipped_lines_come_after_the_entries(self):
        text = self.mail_text(mail_part([msg("m1")], {"m1": Draft("Hi,\n\nx", "S.")},
                                        skipped=[Skipped(msg("n", "Receipt"), "no-reply sender")]))
        self.assertLess(text.index("Where is the report?"), text.index("Skipped — no reply expected"))
        self.assertNotIn("No mail needs an answer", text)

    def test_an_unopened_message_has_the_exact_line_and_no_entry(self):
        text = self.mail_text(mail_part(unopened=[msg("u", "Cannot open this")]))
        self.assertIn("1 message couldn't be opened (subject: Cannot open this) — skipped; open it in the "
                      "mailbox, and report it if it repeats", text)
        self.assertNotIn("No draft for this message", text)

    def test_each_unopened_message_gets_its_own_line(self):
        text = self.mail_text(mail_part(unopened=[msg("u", "One"), msg("v", "Two")]))
        self.assertEqual(len([l for l in text.splitlines() if "couldn't be opened" in l]), 2)

    def test_a_saved_draft_shows_draft_already_saved_with_its_text_and_is_not_proposed_again(self):
        text = self.mail_text(mail_part(saved=[SavedDraft(msg("d", "Re: seat report"),
                                                          "Hi Priya,\n\nThe report is in the shared folder.")]))
        self.assertIn("draft already saved", text)
        self.assertIn("Re: seat report", text)
        self.assertIn("The report is in the shared folder.", text)
        self.assertNotIn("No draft for this message", text)
        for line in text.splitlines():
            self.assertFalse(line.startswith(("#", "Status:")), line)

    def test_nothing_unread_says_so(self):
        self.assertEqual(self.mail_text(mail_part()).strip(), "No mail needs an answer")


class Nothing(unittest.TestCase):
    def test_nothing_fetched_is_true_only_when_every_part_is_missing(self):
        gone = PartResult.missing("x")
        all_missing = SystemResult("suryodaya", "Suryodaya", gone, gone, gone)
        self.assertTrue(render.nothing_fetched([all_missing, all_missing]))
        self.assertFalse(render.nothing_fetched([all_missing, system()]))
        self.assertTrue(render.nothing_fetched([]))


class JsonResult(unittest.TestCase):
    def test_parts_events_tasks_and_others_count(self):
        sy = system(meetings=PartResult.complete(MeetingsPart(DAY, [
            meeting("a", "Budget", 9, 10, clash=True), meeting("b", "Vendor", 9, 10, clash=True, declined=True)])),
            tasks=PartResult.complete(TasksPart(DAY, [OverdueTask("t1", "Mine", "2026-11-01", 5)], 1)))
        ks = SystemResult("keystone", "Keystone", *[PartResult.missing("Keystone: login expired — log in again")] * 3)
        data = render.json_result([sy, ks], 2.5, DAY)
        s = data["systems"]["suryodaya"]
        self.assertEqual(s["mail"], {"complete": False, "reason": NO_MAILBOX})
        self.assertEqual(s["meetings"]["complete"], True)
        self.assertEqual({e["id"]: (e["clash"], e["declined"]) for e in s["meetings"]["events"]},
                         {"a": (True, False), "b": (True, True)})
        self.assertEqual(s["tasks"], {"complete": True,
                                      "own_overdue": [{"id": "t1", "title": "Mine", "due_date": "2026-11-01",
                                                       "days_overdue": 5}],
                                      "others_count": 1})
        for name in ("mail", "meetings", "tasks"):
            self.assertEqual(data["systems"]["keystone"][name],
                             {"complete": False, "reason": "Keystone: login expired — log in again"})
        self.assertEqual(data["date"], "2026-11-06")
        self.assertEqual(data["elapsed_seconds"], 2.5)

    def test_unavailable_others_count_is_null_and_the_part_complete(self):
        sy = system(tasks=PartResult.complete(TasksPart(DAY, [], None)))
        data = render.json_result([sy], 0, DAY)
        self.assertEqual(data["systems"]["suryodaya"]["tasks"],
                         {"complete": True, "own_overdue": [], "others_count": None})

    def test_mail_fields_and_the_model_block(self):
        needs = [msg("a", at="2026-11-05T05:00:00Z"), msg("b", at="2026-11-05T06:00:00Z"),
                 msg("c", at="2026-11-05T07:00:00Z"), msg("d", at="2026-11-05T08:00:00Z"),
                 msg("e", at="2026-11-05T09:00:00Z"), msg("f", at="2026-11-05T10:00:00Z")]
        drafts = {"a": Draft("Hi,\n\nx"), "b": DraftFailed("model unavailable"), "c": DraftFailed("time limit"),
                  "d": DraftFailed("reply over 120 words"), "e": DraftFailed("needs your decision", None, True),
                  "f": DraftFailed("no greeting")}
        part = mail_part(needs, drafts, skipped=[Skipped(msg("n"), "no-reply sender")], unopened=[msg("u")],
                         saved=[SavedDraft(msg("s"), "text")])
        model = {"calls": 7, "failures": 2, "unreachable": True}
        data = render.json_result([system(mail=part)], 1, DAY, model)
        mail = data["systems"]["suryodaya"]["mail"]
        self.assertEqual(mail, {
            "complete": True,
            "needs_answer": [{"id": "a", "draft_status": "proposed"}, {"id": "b", "draft_status": "model_unavailable"},
                             {"id": "c", "draft_status": "time_limit"}, {"id": "d", "draft_status": "invalid"},
                             {"id": "e", "draft_status": "needs_decision_redraft_failed"},
                             {"id": "f", "draft_status": "invalid"}],
            "skipped": [{"id": "n", "reason": "no-reply sender"}], "unopened": ["u"], "draft_saved": ["s"]})
        self.assertEqual(data["model"], model)
        self.assertEqual(json.loads(json.dumps(data)), data)

    def test_a_message_with_no_outcome_is_model_unavailable_in_json(self):
        data = render.json_result([system(mail=mail_part([msg("a")], {}))], 0, DAY)
        self.assertEqual(data["systems"]["suryodaya"]["mail"]["needs_answer"],
                         [{"id": "a", "draft_status": "model_unavailable"}])

    def test_no_model_block_without_model_counts(self):
        self.assertNotIn("model", render.json_result([system()], 0, DAY))

    def test_an_empty_complete_mail_part_has_empty_lists(self):
        mail = render.json_result([system(mail=mail_part())], 0, DAY)["systems"]["suryodaya"]["mail"]
        self.assertEqual(mail, {"complete": True, "needs_answer": [], "skipped": [], "unopened": [],
                                "draft_saved": []})

    def test_the_dict_is_plain_json(self):
        data = render.json_result([system(meetings=PartResult.complete(
            MeetingsPart(DAY, [meeting("a", "A", 9, 10)])))], 0, DAY)
        self.assertEqual(json.loads(json.dumps(data)), data)

    def test_event_has_times_and_title_for_the_reader(self):
        data = render.json_result([system(meetings=PartResult.complete(
            MeetingsPart(DAY, [meeting("a", "A", 9, 10)])))], 0, DAY)
        event = data["systems"]["suryodaya"]["meetings"]["events"][0]
        self.assertEqual((event["title"], event["all_day"], event["time"]), ("A", False, "09:00–10:00 IST"))


def failed_save(text="My approved\nreply", subject="Question 1", reason="mailbox is read-only", label="Keystone",
                kind="refused"):
    return FailedSave(label.lower(), label, "m1", subject, reason, text, kind)


class ApprovalSection(unittest.TestCase):
    """Task 29 (P-B-42): the section after the status line, holding the summary and each failed save's text."""

    def briefing(self, summary):
        return render.markdown([system(mail=mail_part())], 1.0, DAY) + render.approval_section(summary)

    def test_it_follows_the_status_line_and_holds_the_summary(self):
        text = self.briefing(ApprovalSummary(saved=1, skipped=0, not_saved=4))
        lines = text.splitlines()
        status = [i for i, l in enumerate(lines) if l.startswith("Status:")]
        self.assertEqual(len(status), 1)
        self.assertEqual(lines[status[0] + 1], "")
        self.assertEqual(lines[status[0] + 2], "## Approval")
        self.assertIn("1 saved (0 edited), 0 skipped, 4 not saved", lines[status[0] + 3:])
        self.assertTrue(text.endswith("\n"))

    def test_each_failed_save_keeps_its_approved_text_quoted_with_its_reason(self):
        summary = ApprovalSummary(saved=0, not_saved=1, failed=[failed_save()])
        text = self.briefing(summary)
        approval = text[text.index("## Approval"):]
        self.assertIn("Question 1", approval)
        self.assertIn("Keystone", approval)
        self.assertIn("mailbox is read-only", approval)
        self.assertIn("  > My approved\n  > reply", approval)
        self.assertIn("save it by hand", approval)

    def test_text_that_looks_like_a_heading_or_the_status_line_cannot_pose_as_one(self):
        nasty = failed_save(text="# Fake heading\nStatus: everything is complete", subject="## Boom\nStatus: x",
                            reason="Status: fine\n# nope")
        text = self.briefing(ApprovalSummary(not_saved=1, failed=[nasty]))
        self.assertEqual([l for l in text.splitlines() if l.startswith("Status:")].__len__(), 1)
        self.assertEqual([l for l in text.splitlines() if l.startswith("#")].count("## Approval"), 1)
        self.assertNotIn("# Fake heading", [l for l in text.splitlines() if l.startswith("#")])
        self.assertEqual(len([l for l in text.splitlines() if l.startswith("## ")]), 2)   # the system and Approval

    def test_a_step_that_offered_nothing_leaves_no_section(self):
        self.assertEqual(render.approval_section(ApprovalSummary()), "")

    def test_several_failed_saves_each_have_an_entry_in_order(self):
        summary = ApprovalSummary(not_saved=2, failed=[failed_save(text="first text", subject="One"),
                                                       failed_save(text="second text", subject="Two")])
        approval = render.approval_section(summary)
        self.assertLess(approval.index("first text"), approval.index("second text"))
        self.assertLess(approval.index("One"), approval.index("Two"))

    def test_a_saved_only_summary_has_no_failed_entries(self):
        approval = render.approval_section(ApprovalSummary(saved=2, edited=1))
        self.assertIn("2 saved (1 edited), 0 skipped, 0 not saved", approval)
        self.assertNotIn("save it by hand", approval)

    def test_the_status_line_of_the_briefing_is_unchanged_by_the_section(self):
        plain = render.markdown([system(mail=mail_part())], 1.0, DAY)
        self.assertTrue(self.briefing(ApprovalSummary(saved=1)).startswith(plain))

    def section(self, kind, reason="x"):
        return render.approval_section(ApprovalSummary(not_saved=1, failed=[failed_save(kind=kind, reason=reason)]))

    def test_a_refused_or_expired_save_keeps_the_text_to_save_by_hand(self):
        for kind in ("refused", "expired"):
            section = self.section(kind)
            self.assertIn("Your approved text, kept here so you can save it by hand:", section, kind)
            self.assertIn("  > My approved\n  > reply", section, kind)

    def test_an_unconfirmed_save_says_check_the_drafts_folder_and_not_to_retry(self):
        for kind in ("unconfirmed", "interrupted"):
            section = self.section(kind, reason="may not be saved")
            self.assertIn("Draft may not be saved — check your Drafts folder", section, kind)
            self.assertIn("do not retry, to avoid duplicates", section, kind)
            self.assertNotIn("save it by hand", section, kind)

    def test_an_answered_original_says_no_draft_was_saved(self):
        section = self.section("answered", reason="already answered")
        self.assertIn("Already answered — draft not saved", section)
        self.assertNotIn("save it by hand", section)
        self.assertNotIn("Drafts folder", section)

    def test_a_failed_save_without_a_kind_reads_as_refused(self):
        failed = FailedSave("keystone", "Keystone", "m1", "S", "r", "t")
        self.assertEqual(failed.kind, "refused")

    def test_the_section_is_text_only_no_secret_is_passed_in(self):
        import inspect
        self.assertEqual(list(inspect.signature(render.approval_section).parameters), ["summary"])


class JsonApproval(unittest.TestCase):
    """Task 29 (P-B-45): the `approval` key of the --json dict."""

    def test_the_summary_is_added_with_the_failed_saves(self):
        summary = ApprovalSummary(saved=2, edited=1, skipped=1, not_saved=1, failed=[failed_save()])
        data = render.json_result([system()], 1, DAY, approval=summary)
        self.assertEqual(data["approval"], {
            "saved": 2, "edited": 1, "skipped": 1, "not_saved": 1,
            "failed": [{"system": "keystone", "id": "m1", "subject": "Question 1", "kind": "refused",
                        "reason": "mailbox is read-only", "text": "My approved\nreply"}]})
        self.assertEqual(json.loads(json.dumps(data)), data)

    def test_each_failed_save_carries_its_kind(self):
        kinds = ("refused", "unconfirmed", "answered", "expired", "interrupted")
        summary = ApprovalSummary(not_saved=5, failed=[failed_save(kind=k) for k in kinds])
        data = render.json_result([system()], 1, DAY, approval=summary)
        self.assertEqual([f["kind"] for f in data["approval"]["failed"]], list(kinds))

    def test_an_empty_summary_is_still_there(self):
        data = render.json_result([system()], 1, DAY, approval=ApprovalSummary())
        self.assertEqual(data["approval"], {"saved": 0, "edited": 0, "skipped": 0, "not_saved": 0, "failed": []})

    def test_no_key_when_the_step_did_not_run(self):
        self.assertNotIn("approval", render.json_result([system()], 1, DAY))
        self.assertNotIn("approval", render.json_result([system()], 1, DAY, {"calls": 0}))

    def test_the_other_keys_are_unchanged(self):
        base = render.json_result([system()], 1, DAY, {"calls": 1, "failures": 0, "unreachable": False})
        data = render.json_result([system()], 1, DAY, {"calls": 1, "failures": 0, "unreachable": False},
                                  approval=ApprovalSummary())
        data.pop("approval")
        self.assertEqual(data, base)


if __name__ == "__main__":
    unittest.main()
