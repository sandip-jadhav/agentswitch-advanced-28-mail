"""meetings.py: day window per zone, overlap, ordering, labels, clash and declined (P-B-11 to P-B-14)."""
import unittest
from datetime import date, datetime, timezone
from zoneinfo import ZoneInfo

from ea_briefing import meetings
from ea_briefing.auth import User
from ea_briefing.mcp import McpClient
from tests import fakes
from tests.fakes import FakeClock, FakeTransport, ToolError, attendee, event_row

BASE = "https://fake.test"
TOKEN = "tok"
IST, NY = ZoneInfo("Asia/Kolkata"), ZoneInfo("America/New_York")
USER = User("u-100", "e-100", "Team28C@example.test", "Team")


def read(events, zone=IST, now=fakes.FIXED_NOW, filters=True, page_size=100, server=None, **kwargs):
    transport = FakeTransport()
    srv = transport.server(BASE, "fake", TOKEN)
    srv.rows("CalendarEvent.list", events, filters=filters)
    if server:
        server(srv)
    client = McpClient(BASE, TOKEN, transport, page_size=page_size, label="Keystone")
    result = meetings.read(client, USER, zone, FakeClock(now), **kwargs)
    result.transport = transport
    return result


def titles(result):
    return [m.title for m in result.data.meetings]


def ev(id, start, end, **kw):
    return event_row(id, kw.pop("title", id), start, end, **kw)


class DayWindow(unittest.TestCase):
    def test_today_is_the_calendar_day_in_the_systems_zone(self):
        rows = [ev("in", "2026-11-06T03:30:00Z", "2026-11-06T04:00:00Z"),      # 09:00 IST on 6 Nov
                ev("before", "2026-11-05T10:00:00Z", "2026-11-05T11:00:00Z"),  # 5 Nov IST
                ev("after", "2026-11-06T20:00:00Z", "2026-11-06T21:00:00Z")]   # 7 Nov 01:30 IST
        r = read(rows)
        self.assertTrue(r.ok)
        self.assertEqual(r.data.day, date(2026, 11, 6))
        self.assertEqual(titles(r), ["in"])

    def test_the_same_events_fall_on_different_days_in_another_zone(self):
        rows = [ev("late", "2026-11-07T03:00:00Z", "2026-11-07T03:30:00Z")]  # 22:00 EST on 6 Nov = 08:30 IST on 7 Nov
        self.assertEqual(titles(read(rows, NY)), ["late"])
        self.assertEqual(titles(read(rows, IST)), [])

    def test_the_day_follows_the_clock_in_that_zone(self):
        now = datetime(2026, 11, 6, 20, 0, tzinfo=timezone.utc)  # 7 Nov 01:30 in IST, still 6 Nov in New York
        self.assertEqual(read([], IST, now).data.day, date(2026, 11, 7))
        self.assertEqual(read([], NY, now).data.day, date(2026, 11, 6))

    def test_events_spanning_midnight_are_included_both_ways(self):
        rows = [ev("from_yesterday", "2026-11-05T17:00:00Z", "2026-11-05T19:00:00Z"),  # 22:30 -> 00:30 IST
                ev("to_tomorrow", "2026-11-06T17:30:00Z", "2026-11-06T19:00:00Z")]     # 23:00 -> 00:30 IST
        self.assertEqual(titles(read(rows)), ["from_yesterday", "to_tomorrow"])

    def test_events_touching_the_day_edges_are_not_in_the_day(self):
        rows = [ev("ends_at_midnight", "2026-11-05T18:00:00Z", "2026-11-05T18:30:00Z"),   # ends 00:00 IST on 6 Nov
                ev("starts_at_next_midnight", "2026-11-06T18:30:00Z", "2026-11-06T19:00:00Z")]
        self.assertEqual(titles(read(rows)), [])

    def test_a_day_with_a_clock_change_ends_at_local_midnight(self):
        now = datetime(2026, 11, 1, 15, 0, tzinfo=timezone.utc)  # 1 Nov in New York: the day DST ends
        rows = [ev("last", "2026-11-02T04:30:00Z", "2026-11-02T04:45:00Z"),   # 23:30 EST on 1 Nov
                ev("next_day", "2026-11-02T05:00:00Z", "2026-11-02T05:30:00Z")]  # 00:00 EST on 2 Nov
        self.assertEqual(titles(read(rows, NY, now)), ["last"])

    # Probed live 2026-11-06: the platform stores offset-less LOCAL wall-clock times (Keystone's start
    # between 06:00 and 16:00 with timezone America/New_York; Suryodaya's at about 10:00 with Asia/Kolkata
    # or no timezone at all). They are not UTC.
    def test_a_naive_timestamp_is_local_time_in_the_rows_own_timezone(self):
        rows = [ev("n", "2026-11-06T09:00:00", "2026-11-06T09:30:00", zone_name="America/New_York")]
        r = read(rows, NY)
        self.assertEqual(titles(r), ["n"])
        self.assertEqual(r.data.meetings[0].time_text, "09:00–09:30 EST")

    def test_a_naive_timestamp_without_a_row_timezone_is_local_time_in_the_systems_zone(self):
        for zone, label in ((IST, "IST"), (NY, "EST")):
            r = read([ev("n", "2026-11-06T10:00:00", "2026-11-06T10:30:00")], zone)
            self.assertEqual([m.time_text for m in r.data.meetings], ["10:00–10:30 " + label])

    def test_a_row_timezone_wins_over_the_platform_and_display_zones(self):
        rows = [ev("n", "2026-11-06T09:00:00", "2026-11-06T09:30:00", zone_name="America/New_York")]
        r = read(rows, IST, naive_zone=IST)
        self.assertEqual([m.time_text for m in r.data.meetings], ["19:30–20:00 IST"])

    def test_a_naive_timestamp_without_a_row_timezone_is_read_in_the_platform_zone_not_the_display_zone(self):
        # Suryodaya's 10:00 (IST) with the display overridden to New York is 23:30 EST on 5 Nov.
        rows = [ev("n", "2026-11-06T10:00:00", "2026-11-06T10:30:00")]
        self.assertEqual(titles(read(rows, NY, naive_zone=IST)), [])
        on_the_5th = datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc)
        r = read(rows, NY, now=on_the_5th, naive_zone=IST)
        self.assertEqual(titles(r), ["n"])
        self.assertTrue(r.data.meetings[0].time_text.startswith("23:30–"), r.data.meetings[0].time_text)
        self.assertTrue(r.data.meetings[0].time_text.endswith(" EST"))

    def test_a_naive_timestamp_later_in_the_platform_day_lands_on_the_same_display_day(self):
        rows = [ev("n", "2026-11-06T17:00:00", "2026-11-06T17:30:00")]   # 17:00 IST = 06:30 EST on 6 Nov
        r = read(rows, NY, naive_zone=IST)
        self.assertEqual([m.time_text for m in r.data.meetings], ["06:30–07:00 EST"])

    def test_a_naive_timestamp_just_after_local_midnight_is_in_that_day(self):
        rows = [ev("n", "2026-11-06T00:30:00", "2026-11-06T01:00:00", zone_name="America/New_York")]
        self.assertEqual(titles(read(rows, NY)), ["n"])

    def test_a_row_timezone_that_is_not_a_zone_falls_back_to_the_systems_zone(self):
        r = read([ev("n", "2026-11-06T10:00:00", "2026-11-06T10:30:00", zone_name="Mars/Olympus")])
        self.assertEqual([m.time_text for m in r.data.meetings], ["10:00–10:30 IST"])

    def test_an_explicit_offset_wins_over_the_row_timezone(self):
        rows = [ev("n", "2026-11-06T10:00:00+05:30", "2026-11-06T10:30:00+05:30", zone_name="America/New_York")]
        self.assertEqual([m.time_text for m in read(rows).data.meetings], ["10:00–10:30 IST"])


class AllDayAndCancelled(unittest.TestCase):
    def test_all_day_events_are_listed_by_date(self):
        rows = [ev("today", "2026-11-06", "2026-11-06", all_day=True),
                ev("multi", "2026-11-04", "2026-11-08", all_day=True),
                ev("yesterday", "2026-11-05", "2026-11-05", all_day=True),
                ev("tomorrow", "2026-11-07", "2026-11-07", all_day=True)]
        self.assertEqual(sorted(titles(read(rows))), ["multi", "today"])

    def test_an_all_day_event_with_an_exclusive_midnight_end_does_not_spill_into_the_next_day(self):
        rows = [ev("holiday", "2026-11-05T00:00:00Z", "2026-11-06T00:00:00Z", all_day=True)]
        self.assertEqual(titles(read(rows)), [])

    # Probed live 2026-11-06: all-day rows carry date-only text and the END DATE IS EXCLUSIVE
    # ("USA: Christmas Day" is 2027-12-25 to 2027-12-26); a one-day event also appears as same-day dates.
    def test_a_date_only_all_day_end_is_exclusive(self):
        rows = [ev("yesterdays_holiday", "2026-11-05", "2026-11-06", all_day=True),
                ev("todays_holiday", "2026-11-06", "2026-11-07", all_day=True),
                ev("trip", "2026-11-04", "2026-11-07", all_day=True)]
        self.assertEqual(sorted(titles(read(rows))), ["todays_holiday", "trip"])

    def test_an_all_day_event_ending_at_the_end_of_its_day_stays_on_that_day(self):
        rows = [ev("founders", "2026-11-05T00:00:00", "2026-11-05T23:59:59", all_day=True)]
        self.assertEqual(titles(read(rows)), [])
        self.assertEqual(titles(read(rows, now=datetime(2026, 11, 5, 6, 0, tzinfo=timezone.utc))), ["founders"])

    def test_cancelled_events_are_hidden(self):
        rows = [ev("a", "2026-11-06T03:30:00Z", "2026-11-06T04:00:00Z", status="cancelled"),
                ev("b", "2026-11-06T04:30:00Z", "2026-11-06T05:00:00Z", status="canceled"),
                ev("c", "2026-11-06T05:30:00Z", "2026-11-06T06:00:00Z", status="Cancelled"),
                ev("d", "2026-11-06T06:30:00Z", "2026-11-06T07:00:00Z", status="tentative"),
                ev("e", "2026-11-06T07:30:00Z", "2026-11-06T08:00:00Z", status="confirmed")]
        self.assertEqual(titles(read(rows)), ["d", "e"])

    def test_rows_that_cannot_be_placed_are_skipped(self):
        rows = [{"id": "x", "title": "no times"}, ev("bad", "soon", "later"),
                ev("ok", "2026-11-06T03:30:00Z", "2026-11-06T04:00:00Z")]
        self.assertEqual(titles(read(rows)), ["ok"])


class OrderingAndLabels(unittest.TestCase):
    ROWS = [ev("c", "2026-11-06T09:30:00Z", "2026-11-06T10:30:00Z", title="Retro"),
            ev("a", "2026-11-06", "2026-11-06", all_day=True, title="Holiday", organizer=""),
            ev("b", "2026-11-06T03:30:00Z", "2026-11-06T04:30:00Z", title="Planning", organizer="boss@example.test"),
            ev("d", "2026-11-06", "2026-11-06", all_day=True, title="Another all-day")]

    def test_all_day_first_then_by_start(self):
        self.assertEqual(titles(read(self.ROWS)), ["Another all-day", "Holiday", "Planning", "Retro"])

    def test_each_meeting_has_zone_labelled_times_title_and_organiser(self):
        by_title = {m.title: m for m in read(self.ROWS).data.meetings}
        self.assertEqual(by_title["Planning"].time_text, "09:00–10:00 IST")
        self.assertEqual(by_title["Planning"].organizer, "boss@example.test")
        self.assertEqual(by_title["Retro"].time_text, "15:00–16:00 IST")
        self.assertEqual(by_title["Holiday"].organizer, "—")
        self.assertEqual(by_title["Holiday"].time_text, "all day")
        self.assertTrue(by_title["Holiday"].all_day)
        self.assertEqual(by_title["Planning"].id, "b")

    def test_times_are_in_the_systems_zone_and_labelled_with_it(self):
        rows = [ev("k", "2026-11-07T03:00:00Z", "2026-11-07T03:30:00Z")]
        self.assertEqual(read(rows, NY).data.meetings[0].time_text, "22:00–22:30 EST")

    def test_a_midnight_spanning_meeting_shows_the_date_of_the_part_outside_today(self):
        rows = [ev("s", "2026-11-05T17:00:00Z", "2026-11-05T19:00:00Z")]  # 22:30 on 5 Nov -> 00:30 IST
        self.assertEqual(read(rows).data.meetings[0].time_text, "11-05 22:30–00:30 IST")

    def test_a_missing_title_gets_a_placeholder(self):
        row = ev("x", "2026-11-06T03:30:00Z", "2026-11-06T04:00:00Z")
        row["title"] = ""
        self.assertEqual(titles(read([row])), ["(no title)"])

    def test_no_events_is_a_complete_empty_part(self):
        r = read([])
        self.assertTrue(r.ok)
        self.assertEqual(r.data.meetings, [])


class ClashAndDeclined(unittest.TestCase):
    def marks(self, rows, **kw):
        return {m.title: (m.clash, m.declined) for m in read(rows, **kw).data.meetings}

    def test_overlapping_timed_meetings_both_clash(self):
        rows = [ev("a", "2026-11-06T04:00:00Z", "2026-11-06T05:00:00Z"),
                ev("b", "2026-11-06T04:30:00Z", "2026-11-06T05:30:00Z"),
                ev("c", "2026-11-06T08:00:00Z", "2026-11-06T09:00:00Z")]
        self.assertEqual(self.marks(rows), {"a": (True, False), "b": (True, False), "c": (False, False)})

    def test_back_to_back_meetings_do_not_clash(self):
        rows = [ev("a", "2026-11-06T04:00:00Z", "2026-11-06T05:00:00Z"),
                ev("b", "2026-11-06T05:00:00Z", "2026-11-06T06:00:00Z")]
        self.assertEqual(self.marks(rows), {"a": (False, False), "b": (False, False)})

    def test_a_declined_meeting_still_clashes_and_is_marked_declined(self):
        rows = [ev("a", "2026-11-06T04:00:00Z", "2026-11-06T05:00:00Z"),
                ev("b", "2026-11-06T04:30:00Z", "2026-11-06T05:30:00Z",
                   attendees=[attendee("team28c@EXAMPLE.test", "Declined")])]
        self.assertEqual(self.marks(rows), {"a": (True, False), "b": (True, True)})

    def test_someone_elses_decline_is_not_mine(self):
        rows = [ev("a", "2026-11-06T04:00:00Z", "2026-11-06T05:00:00Z",
                   attendees=[attendee("other@example.test", "declined"), attendee("team28c@example.test", "accepted")])]
        self.assertEqual(self.marks(rows), {"a": (False, False)})

    def test_all_day_events_clash_with_nothing(self):
        rows = [ev("a", "2026-11-06T04:00:00Z", "2026-11-06T05:00:00Z"),
                ev("b", "2026-11-06T04:30:00Z", "2026-11-06T05:30:00Z"),
                ev("day", "2026-11-06", "2026-11-06", all_day=True),
                ev("day2", "2026-11-06", "2026-11-06", all_day=True)]
        marks = self.marks(rows)
        self.assertEqual(marks["day"], (False, False))
        self.assertEqual(marks["day2"], (False, False))
        self.assertEqual(marks["a"], (True, False))

    def test_a_cancelled_meeting_causes_no_clash(self):
        rows = [ev("a", "2026-11-06T04:00:00Z", "2026-11-06T05:00:00Z"),
                ev("gone", "2026-11-06T04:10:00Z", "2026-11-06T04:40:00Z", status="cancelled")]
        self.assertEqual(self.marks(rows), {"a": (False, False)})

    def test_clash_is_judged_on_real_instants_not_on_offsets_in_the_text(self):
        rows = [ev("a", "2026-11-06T10:00:00+05:30", "2026-11-06T11:00:00+05:30"),   # 04:30Z-05:30Z
                ev("b", "2026-11-06T05:00:00Z", "2026-11-06T06:00:00Z")]
        self.assertEqual(self.marks(rows), {"a": (True, False), "b": (True, False)})


class FetchAndFilters(unittest.TestCase):
    def test_server_filters_only_narrow_the_fetch(self):
        r = read([])
        (call,) = r.transport.tool_calls("CalendarEvent.list")
        # Probed live 2026-11-06: lt:/gt:/gte:/lte: work, but compare the stored TEXT, which holds local
        # wall-clock times, date-only all-day dates and mixed offsets. So the filter is date-only with a day
        # of slack on each side (never excluding a row the overlap check would keep).
        self.assertEqual(call.arguments["start_at"], "lt:2026-11-08")
        self.assertEqual(call.arguments["end_at"], "gte:2026-11-05")

    def test_the_server_filter_keeps_events_the_overlap_check_keeps(self):
        # Local naive times: an event 00:30-01:00 New York time ends, as text, before the day's start
        # in UTC (05:00), which an instant-style filter on the text would drop.
        rows = [ev("early", "2026-11-06T00:30:00", "2026-11-06T01:00:00", zone_name="America/New_York"),
                ev("holiday", "2026-11-06", "2026-11-07", all_day=True),
                ev("late", "2026-11-06T23:00:00", "2026-11-06T23:30:00", zone_name="America/New_York"),
                ev("next_day", "2026-11-07T10:00:00", "2026-11-07T11:00:00", zone_name="America/New_York"),
                ev("old", "2026-10-30T10:00:00", "2026-10-30T11:00:00", zone_name="America/New_York")]
        with_filters, without = read(rows, NY, filters=True), read(rows, NY, filters=False)
        self.assertEqual(titles(with_filters), titles(without))
        self.assertEqual(titles(with_filters), ["holiday", "early", "late"])

    def test_a_server_that_ignores_the_filters_gives_the_same_list(self):
        rows = [ev("in", "2026-11-06T03:30:00Z", "2026-11-06T04:00:00Z"),
                ev("far", "2026-11-13T04:00:00Z", "2026-11-13T05:00:00Z"),
                ev("old", "2026-10-30T04:00:00Z", "2026-10-30T05:00:00Z"),
                ev("day", "2026-11-06", "2026-11-06", all_day=True),
                ev("gone", "2026-11-06T04:00:00Z", "2026-11-06T05:00:00Z", status="cancelled")]
        with_filters, without = read(rows, filters=True), read(rows, filters=False)
        self.assertEqual(titles(without), titles(with_filters))
        self.assertEqual(titles(without), ["day", "in"])

    def test_all_pages_are_read(self):
        rows = [ev("e%d" % i, "2026-11-06T0%d:00:00Z" % i, "2026-11-06T0%d:30:00Z" % i) for i in range(1, 6)]
        r = read(rows, page_size=2)
        self.assertEqual(titles(r), ["e1", "e2", "e3", "e4", "e5"])
        self.assertEqual(len(r.transport.tool_calls("CalendarEvent.list")), 3)

    def test_only_the_calendar_tool_is_called(self):
        self.assertEqual(read([]).transport.tool_names(), {"CalendarEvent.list"})


class Failures(unittest.TestCase):
    def test_a_tool_error_makes_the_part_missing_with_its_reason(self):
        r = read([], server=lambda s: s.fail("CalendarEvent.list", ToolError("calendar down")))
        self.assertFalse(r.ok)
        self.assertEqual(r.reason, "Keystone meetings: couldn't be read (reason: calendar down)"
                                   " — report it if it repeats")

    def test_login_expired_and_unreachable_have_their_own_lines(self):
        r = read([], server=lambda s: s.expire())
        self.assertEqual(r.reason, "Keystone: login expired — log in again")
        r = read([], server=lambda s: s.down(TimeoutError("slow")))
        self.assertEqual(r.reason, "Keystone: can't be reached right now — try again later")

    def test_a_failure_on_page_two_makes_the_part_missing(self):
        rows = [ev("e%d" % i, "2026-11-06T0%d:00:00Z" % i, "2026-11-06T0%d:30:00Z" % i) for i in range(1, 4)]
        r = read(rows, page_size=2, server=lambda s: s.fail_when(
            "CalendarEvent.list", lambda a: a["offset"] > 0, ToolError("page 2 broke")))
        self.assertFalse(r.ok)
        self.assertIn("page 2 broke", r.reason)


class DefaultFixtures(unittest.TestCase):
    def test_the_default_world_events_read_as_the_scenarios_expect(self):
        rows = fakes.load_fixture("events.json")
        s = read(rows["suryodaya"])
        self.assertEqual([(m.title, m.time_text) for m in s.data.meetings],
                         [("Daily standup", "09:00–09:30 IST"), ("Design review", "14:30–15:30 IST")])
        self.assertEqual(s.data.meetings[1].organizer, "—")
        k = read(rows["keystone"], NY)
        self.assertEqual([(m.title, m.time_text) for m in k.data.meetings], [("Class sync", "10:00–10:30 EST")])


if __name__ == "__main__":
    unittest.main()
