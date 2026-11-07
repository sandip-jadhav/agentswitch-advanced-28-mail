# PRD: Seat 28 EA — Daily Briefing (Sandip)

**Date:** 2026-11-05
**Status:** Approved
**Template version:** default/0.2.2
**References:** Seat brief `28-ea/README.md` in github.com/theschoolofai/agentswitch-advanced; top-level `README.md` and `BUG_REPORT_FORMAT.md` in the same repository; seat 28 group chat (instructor brief 02/11/2026, Nishanth's Stage 1 plan); Varun's briefing, PR #10 (for comparison only)

**Plugin version:** 0.7.5
**Agent Sessions:** claude-code: 73ee6291-e518-4395-a411-790a2b91d801
**Self-review:** CAP_REACHED — round-2 findings fixed, not re-reviewed. Round-2 findings, verbatim in short: (1) FR-004 duration and FR-007 had no acceptance scenario; (2) PRD-D-16 rationale clashed with FR-005 when a rerun is partial; (3) FR-042 summary format differed between scenarios; (4) "copy the text from this run's briefing file" loses edits after "edit then save"; (5) FR-023 clash rule unclear with all-day events; (6) command-line interface wording not recorded as a decision; (7) "fix the folder" gave no clear next step; (8) PRD-D-5 FR impact missed FR-020; (9) NFR "behave the same on both systems" was not a non-functional delta.

---

<!-- hint: Keep the body narrative, readable top-to-bottom. Reference-heavy material (decisions log, glossary) belongs in appendices at the end of the document. -->

## TL;DR

A daily briefing for one seat-28 login, built as an executive assistant over the AgentSwitch platform. One run reads both systems, Suryodaya and Keystone, and shows three things: unread mail that needs an answer (each with a proposed reply), today's meetings and your overdue tasks.
Proposed replies are only drafts. A reply is saved to the mailbox only after you approve it, one at a time. Nothing is ever sent, booked or changed on its own.
This is Sandip's entry for the seat's first task (15 November), where three versions are compared and the best becomes the base for version 1 (30 November).
The full briefing ships for 15 November. Approved draft saving ships then too if the Keystone sample mailbox exists in time; otherwise it moves to 30 November.

## Overview

- **Purpose**: Give the seat-28 user one morning view, across both systems, of what needs their attention today, with reply drafts ready to approve.
- **User Value**: Today mail, meetings and tasks have to be checked separately on two systems, in two time zones, and every reply written by hand. The briefing gathers it in one read, filters out noise and does the first draft of each reply, while the person keeps every decision.
- **Scope**: A briefing a person runs on demand for their own login on Suryodaya and Keystone. It covers unread mail needing a reply (with proposed drafts), today's meetings, the person's own overdue tasks, an optional approval step that saves chosen drafts to the mailbox, and a dated copy of each briefing. It reads data and creates mailbox drafts only after approval. It never sends, books, or changes anything else.

## User Stories

The user in every story is the **seat-28 user**: one person working under their own seat login (e.g. `team28c`) on both systems.

| Story | Description | Priority |
|-------|-------------|----------|
| Story 1: Run the briefing | Run one command and get a briefing covering both systems, with each part showing its results or why it is missing, saved as a dated file | P1 |
| Story 2: Mail that needs an answer | See unread mail that expects a reply, each with a summary and a safe proposed draft, with no-reply noise listed separately | P1 |
| Story 3: Today's meetings | See today's meetings per system, in that system's time zone, with clashes and declined meetings marked | P1 |
| Story 4: My overdue tasks | See open tasks assigned to or created by me that are past due, most overdue first | P1 |
| Story 5: Approve drafts into the mailbox | Choose, per proposed draft, to save, edit then save, skip or stop, so approved drafts land in the mailbox unsent | P1 (ships 15 Nov only if a mailbox exists in time) |

### Story 1: Run the briefing

- **As a** seat-28 user
- **I want to** run the briefing once and get mail, meetings and tasks from Suryodaya and Keystone together
- **So that** I have one morning read covering both systems, and I know right away if any part of it is missing

#### Acceptance Scenarios

**Scenario 1: Happy Path**
- **Given** a valid login on both Suryodaya and Keystone, with a mailbox, meetings and tasks on each
- **When** the user runs the briefing
- **Then** the briefing shows one section per system, each with Mail, Meetings and Tasks parts
- **And** it ends with a status line saying every part on both systems is complete and how many seconds the briefing took
- **And** the same briefing is saved as a readable file named for today's date

**Scenario 2: Error Path**
- **Given** a valid login on Suryodaya and an expired login on Keystone
- **When** the user runs the briefing
- **Then** the Suryodaya section shows its results in full
- **And** each Keystone part shows "Keystone: login expired — log in again"
- **And** the status line says which parts are complete and which are missing

**Scenario 3: Edge Case — limit to one system**
- **Given** a valid login on both systems, and a full briefing file for today already saved
- **When** the user runs the briefing limited to Keystone
- **Then** only the Keystone section is shown, and the status line names only Keystone
- **And** it is saved as a separate file named for today's date and Keystone, while today's full briefing file stays unchanged

**Scenario 4: Edge Case — rerun the same day**
- **Given** a full briefing file for today already exists
- **When** the user runs the full briefing again
- **Then** today's full briefing file is replaced by the new briefing, and earlier days' files are unchanged

**Scenario 5: Edge Case — nothing could be fetched**
- **Given** a full briefing file for today already saved, and neither system can be reached
- **When** the user runs the briefing
- **Then** each part shows its reason, the status line says nothing was complete, and the user sees "Nothing fetched — today's earlier briefing file kept"
- **And** today's earlier file is unchanged and no new file is written

**Scenario 6: Edge Case — nothing is changed by a run**
- **Given** a snapshot of the user's mailbox, calendar and tasks on both systems
- **When** the user runs the briefing and skips every proposed reply
- **Then** mailbox, calendar and tasks are unchanged on both systems: no message sent or saved, no meeting accepted, declined or booked, no task created, changed or deleted

#### Edge Cases

> *Inline guidance:* Specify the pending (loading / in-progress), error, and edge states PER STORY — a story without them is incomplete. Design each state with the user's cognitive load in mind: what the user sees, what they must understand, and how they recover.

| Edge Case | Scenario | Expected Behavior |
|-----------|----------|-------------------|
| In progress | Briefing is fetching from both systems | One short progress line per system ("Reading Keystone…"), so the run never looks frozen |
| Nothing to report | Both systems return no mail, meetings or overdue tasks | Each part says "Nothing for today" instead of showing empty space; status line still says complete |
| One system limited | User limits the run to one system | Only that system is fetched and shown; saved to a file named for the date and that system, so today's full briefing is not replaced |
| Same-day rerun | A file for today already exists | Today's file of the same kind (full or that system) is replaced; other files are untouched |
| Both systems fail | Neither system can be reached | Each part shows its reason; status line says nothing was complete; no file is written and any earlier file for today is left unchanged, with the message "Nothing fetched — today's earlier briefing file kept" |

#### Error Handling

> *Inline guidance:* Error messages must respect the user's cognitive-load constraints: say what happened and what to do next, in plain language.

| Error Condition | User Message | System Action |
|-----------------|--------------|---------------|
| Login expired on a system | "Keystone: login expired — log in again" | Skip that system's parts, keep the rest, record it in the status line |
| System unreachable | "Suryodaya: can't be reached right now — try again later" | Skip that system's parts, keep the rest, record it in the status line |
| A part fails for another reason | "Keystone meetings: couldn't be read (reason: …) — report it if it repeats" | Show the platform's reason in plain words, keep the other parts |
| Briefing file can't be saved | "Briefing shown but not saved to file: … — copy it from the screen, or check that the briefing folder exists and you can write to it, then rerun" | Still print the full briefing |

### Story 2: Mail that needs an answer

- **As a** seat-28 user
- **I want to** see the unread mail that expects a reply from me, each with a short summary and a proposed reply
- **So that** I can answer quickly without reading every message first, and without wasting time on mail that needs no answer

#### Acceptance Scenarios

**Scenario 1: Happy Path**
- **Given** a mailbox holding an unread message from a person asking a question, which has not been answered and is not a draft
- **When** the user runs the briefing
- **Then** the Mail part lists that message with sender, subject, received time and a one-line summary of what it asks
- **And** it shows a proposed reply of at most 120 words that opens with a greeting, is written in the language of the original message and ends signed with the user's name

**Scenario 2: Error Path**
- **Given** the login has no mailbox on a system (the situation on seat-28 logins as of 5 November)
- **When** the user runs the briefing
- **Then** that system's Mail part shows "Suryodaya: no mailbox for this login — ask the course team to set one up" and no drafts
- **And** that system's Meetings and Tasks parts are still shown

**Scenario 3: Edge Case — mail that expects no reply**
- **Given** unread messages from a no-reply address, an automated notification, a newsletter and a message the user sent themselves
- **When** the user runs the briefing
- **Then** none of them gets a proposed reply
- **And** each one is listed as a single line under "Skipped — no reply expected", with the reason it was skipped

**Scenario 4: Edge Case — mail asking for a commitment**
- **Given** an unread message asking the user to agree to a date, a payment or attending a meeting
- **When** the user runs the briefing
- **Then** the proposed reply acknowledges the message and says the user will confirm, without agreeing to the date, amount or attendance
- **And** the entry is marked "needs your decision"

**Scenario 5: Edge Case — already answered or draft**
- **Given** an unread message that the user has already replied to, and a draft message in the mailbox
- **When** the user runs the briefing
- **Then** neither appears in the list of mail needing an answer

#### Edge Cases

| Edge Case | Scenario | Expected Behavior |
|-----------|----------|-------------------|
| In progress | Proposed replies are being written | Progress line "Drafting replies (3 of 7)…" so a slow step is visible |
| No mail needing an answer | Everything unread is skipped or there is no unread mail | Mail part says "No mail needs an answer"; skipped list still shown if not empty |
| Commitment requested | Message asks for a date, payment or attendance | Non-committal draft plus "needs your decision" mark |
| Non-English mail | Message is written in another language | Summary in English; proposed reply in the message's language. When no summary can be written (model unavailable, drafting time limit reached, or the summary didn't come back in English), the message shows its first sentence in its own language, labelled "(no summary — first line shown)" |
| Many messages | Inbox holds many unread messages needing an answer | All are listed, oldest first, each with a summary. Each gets its own proposed reply, except that drafting stops when the run's time limit is reached (to keep the 60-second limit). Messages left over show "No draft for this message (time limit) — reply by hand" |

#### Error Handling

| Error Condition | User Message | System Action |
|-----------------|--------------|---------------|
| No mailbox for the login | "Suryodaya: no mailbox for this login — ask the course team to set one up" | Skip that system's Mail part, keep the others |
| A message can't be read back | "1 message couldn't be opened (subject: …) — skipped; open it in the mailbox, and report it if it repeats" | List it by subject, propose no draft, continue with the rest |
| A proposed reply can't be written (including when the run's drafting time limit is reached) | "No draft for this message (…) — reply by hand" | Keep the message in the list without a draft, with its summary, or with its first sentence labelled "(no summary — first line shown)" when no summary could be written |

### Story 3: Today's meetings

- **As a** seat-28 user
- **I want to** see today's meetings on each system, in that system's time zone
- **So that** I know my day at a glance and spot clashes before they hurt

#### Acceptance Scenarios

**Scenario 1: Happy Path**
- **Given** Suryodaya has two meetings today in IST (India Standard Time) and an all-day event
- **When** the user runs the briefing
- **Then** the Suryodaya Meetings part lists the all-day event first, then the meetings by start time
- **And** each meeting shows its title, start and end time in IST, and its organiser
- **And** the all-day event does not make either meeting a "clash"

**Scenario 2: Error Path**
- **Given** the calendar on Keystone can't be read
- **When** the user runs the briefing
- **Then** the Keystone Meetings part shows "Keystone meetings: couldn't be read — …" and the other parts are still shown

**Scenario 3: Edge Case — time zones per system**
- **Given** a Keystone meeting at 22:00 US Eastern time today, which is already tomorrow in IST
- **When** the user runs the briefing
- **Then** it appears under Keystone's today, because Keystone's day is US Eastern (America/New_York)
- **And** Keystone times are shown in US Eastern, Suryodaya times in IST, each labelled with its zone

**Scenario 4: Edge Case — clash, declined, cancelled**
- **Given** today holds two overlapping meetings, one meeting the user declined and one cancelled meeting
- **When** the user runs the briefing
- **Then** the overlapping meetings are both marked "clash"
- **And** the declined meeting is shown and marked "declined"
- **And** the cancelled meeting is not shown

**Scenario 5: Edge Case — change a system's time zone**
- **Given** Suryodaya has a meeting at 09:00 IST today
- **When** the user runs the briefing with Suryodaya's time zone set to US Eastern
- **Then** Suryodaya's "today" is the US Eastern day, the meeting is listed only if it overlaps that day, and its times are shown and labelled in US Eastern

#### Edge Cases

| Edge Case | Scenario | Expected Behavior |
|-----------|----------|-------------------|
| In progress | Calendar is being read | Covered by the per-system progress line ("Reading Keystone…") from Story 1 |
| No meetings today | Calendar has nothing today | "No meetings today" |
| Meeting spans midnight | Meeting starts yesterday and ends today, or starts today and ends tomorrow | Included, because it overlaps today |
| Meeting already over | Meeting earlier today has ended | Still listed, so the user can follow up |
| Other time zone requested | User changes a system's time zone for a run | That system's "today" and all its times use the chosen zone |

#### Error Handling

| Error Condition | User Message | System Action |
|-----------------|--------------|---------------|
| Calendar can't be read | "Keystone meetings: couldn't be read (reason: …) — report it if it repeats" | Skip that part, keep the rest |
| Unknown time zone requested | "Unknown time zone '…' — using IST for Suryodaya" (or US Eastern for Keystone) | Fall back to the system's default zone and say so |

### Story 4: My overdue tasks

- **As a** seat-28 user
- **I want to** see the open tasks I own or created that are past their due date
- **So that** I deal with what is mine without wading through everyone else's backlog

#### Acceptance Scenarios

**Scenario 1: Happy Path**
- **Given** the user has three open tasks assigned to or created by them, due 1, 5 and 10 days ago
- **When** the user runs the briefing
- **Then** the Tasks part lists them most overdue first, each with title, due date and how many days overdue

**Scenario 2: Error Path**
- **Given** tasks can't be read on Suryodaya
- **When** the user runs the briefing
- **Then** the Suryodaya Tasks part shows "Suryodaya tasks: couldn't be read — …" and the other parts are still shown

**Scenario 3: Edge Case — other people's overdue tasks**
- **Given** the login can see 63 overdue tasks on Keystone, none assigned to or created by the user
- **When** the user runs the briefing
- **Then** the Keystone Tasks part says "No overdue tasks of yours"
- **And** one line says "63 other overdue tasks are visible to this login"

**Scenario 4: Edge Case — due today, done, no due date**
- **Given** the user has a task due today, a completed task due last week, and an open task with no due date
- **When** the user runs the briefing
- **Then** none of the three is listed as overdue

#### Edge Cases

| Edge Case | Scenario | Expected Behavior |
|-----------|----------|-------------------|
| In progress | Tasks are being read | Covered by the per-system progress line ("Reading Keystone…") from Story 1 |
| No overdue tasks of mine | All of the user's tasks are on time | "No overdue tasks of yours", plus the count line for others' tasks |
| Overdue boundary | Task is due today in the system's time zone | Not overdue; overdue means the due date is before today in that system's time zone |
| Many overdue tasks | User has a long overdue list | All listed, most overdue first |

#### Error Handling

| Error Condition | User Message | System Action |
|-----------------|--------------|---------------|
| Tasks can't be read | "Suryodaya tasks: couldn't be read (reason: …) — report it if it repeats" | Skip that part, keep the rest |
| Others' count can't be determined | "Count of other overdue tasks unavailable — your own list above is complete" | Show the user's own tasks without the count line |

### Story 5: Approve drafts into the mailbox

- **As a** seat-28 user
- **I want to** decide, one proposed reply at a time, whether to save it as a draft in my mailbox
- **So that** good drafts are waiting in my mailbox, while nothing is saved without my say and nothing is ever sent

#### Acceptance Scenarios

**Scenario 1: Happy Path**
- **Given** the briefing has shown two proposed replies for messages on Keystone
- **When** the user chooses "save" for the first and "edit then save" for the second, changing its text
- **Then** both are saved as unsent draft replies to their messages in the Keystone mailbox, the second with the edited text
- **And** nothing is sent
- **And** a closing summary says "2 saved (1 edited), 0 skipped, 0 not saved"

**Scenario 2: Error Path**
- **Given** the user chooses "save" for a proposed reply
- **When** the mailbox refuses the save
- **Then** the user sees "Draft not saved: … — your text is shown below and kept in this run's briefing file; save it by hand"
- **And** the text shown and kept is the one the user approved, including any edits
- **And** the remaining proposed replies are still offered

**Scenario 3: Edge Case — accidental key press**
- **Given** a proposed reply is being offered
- **When** the user presses Enter without choosing
- **Then** it counts as "skip" and nothing is saved

**Scenario 4: Edge Case — stop part way**
- **Given** five proposed replies, of which the user has saved one
- **When** the user chooses "stop" on the second
- **Then** no further replies are offered, the remaining four (including the one on which "stop" was chosen) stay unsaved, and the summary says "1 saved (0 edited), 0 skipped, 4 not saved"

**Scenario 5: Edge Case — rerun after saving**
- **Given** the user saved a draft for a message earlier today
- **When** the user runs the briefing again
- **Then** that message shows "draft already saved" with the saved text, no new reply is proposed for it, and the saved draft is not changed

**Scenario 6: Edge Case — answered meanwhile**
- **Given** a proposed reply for a message that the user answered by hand after the briefing was produced
- **When** the user chooses "save" for it
- **Then** nothing is saved and the user sees "Already answered — draft not saved"

#### Edge Cases

| Edge Case | Scenario | Expected Behavior |
|-----------|----------|-------------------|
| In progress | A draft is being saved | "Saving draft…" then "Saved" or the failure message, before the next reply is offered |
| No proposed replies | Briefing has no drafts to offer | Approval step is skipped with "No drafts to approve" |
| Message answered meanwhile | The message was answered after the briefing was produced | Not saved; user sees "Already answered — draft not saved" |
| No mailbox | System has no mailbox for the login | No approval step for that system |
| Empty edit | User clears all text in "edit then save" | Treated as skip; nothing saved |

#### Error Handling

| Error Condition | User Message | System Action |
|-----------------|--------------|---------------|
| Mailbox refuses the save | "Draft not saved: … — your text is shown below and kept in this run's briefing file; save it by hand" | Continue with the next reply |
| Login expired during approval | "Keystone: login expired — remaining drafts not saved; log in again and rerun" | Stop offering that system's drafts; summary lists what was not saved |
| Saved draft can't be confirmed | "Draft may not be saved — check your Drafts folder" | Do not retry automatically, to avoid duplicates |

## Story Dependencies

- Story 2, 3 and 4 each depend on Story 1: they are parts of the briefing that Story 1 runs, shows and saves.
- Story 5 depends on Story 2: only replies proposed in Story 2 can be approved. It also depends on a mailbox existing for the login.
- Story 3 and Story 4 are independent of each other and of Story 2.

## Functional Requirements

**Briefing run (Story 1)**
- <a id="fr-001"></a>**FR-001**: System MUST produce, on demand, one briefing covering Suryodaya and Keystone, with a separate labelled section per system, each holding Mail, Meetings and Tasks parts.
- <a id="fr-002"></a>**FR-002**: System MUST let the user limit a run to one system.
- <a id="fr-003"></a>**FR-003**: System MUST, when a part cannot be fetched, show a plain one-line reason with what to do next, and still show every other part.
- <a id="fr-004"></a>**FR-004**: System MUST end each briefing with a status line naming which parts on which systems were complete, and how long the briefing took, excluding approval time.
- <a id="fr-005"></a>**FR-005**: System MUST save each briefing as a readable file named for its date, plus the system's name when the run was limited to one system; a rerun on the same day replaces only the file of the same name.
- <a id="fr-007"></a>**FR-007**: System MUST NOT write a file when no part on any system could be fetched, and MUST leave any earlier file for that day unchanged.
- <a id="fr-006"></a>**FR-006**: System SHOULD show a short progress line while it reads each system and while it writes proposed replies.

**Mail (Story 2)**
- <a id="fr-010"></a>**FR-010**: System MUST list unread inbox messages that are not drafts and not already answered, as mail needing an answer.
- <a id="fr-011"></a>**FR-011**: System MUST leave out of that list, and propose no reply for, messages from no-reply or automated senders, newsletters, and messages the user sent; it MUST list each such message on one line under "Skipped — no reply expected" with its reason.
- <a id="fr-012"></a>**FR-012**: System MUST show, for each message needing an answer, oldest first, sender, subject, received time, a one-line summary of what it asks, and a proposed reply.
- <a id="fr-013"></a>**FR-013**: Proposed replies MUST be at most 120 words, open with a greeting, be in the language of the original message and be signed with the user's name.
- <a id="fr-014"></a>**FR-014**: Proposed replies MUST NOT commit the user to a date, money or attendance; for such messages the reply MUST say the user will confirm, and the entry MUST be marked "needs your decision".
- FR-013's language rule and FR-014 are met by the drafting model's instructions and the system's checks, and confirmed by the user's review before any save. They cannot be fully proven by code, which is one reason no reply is ever saved without approval.

**Meetings (Story 3)**
- <a id="fr-020"></a>**FR-020**: System MUST list every non-cancelled calendar event that overlaps today, where today is IST for Suryodaya and US Eastern (America/New_York) for Keystone by default.
- <a id="fr-021"></a>**FR-021**: System MUST let the user change a system's time zone for a run; times MUST be shown in, and labelled with, that system's zone.
- <a id="fr-022"></a>**FR-022**: System MUST list all-day events first, then meetings by start time, each with title, start and end time and organiser.
- <a id="fr-023"></a>**FR-023**: System MUST mark overlapping meetings as "clash" and meetings the user declined as "declined"; all-day events never cause a clash.

**Tasks (Story 4)**
- <a id="fr-030"></a>**FR-030**: System MUST list open tasks assigned to or created by the user whose due date is before today in that system's time zone, most overdue first, with title, due date and days overdue.
- <a id="fr-031"></a>**FR-031**: System MUST add one line with the count of other overdue tasks visible to the login.

**Approval (Story 5)**
- <a id="fr-040"></a>**FR-040**: System MUST offer each proposed reply for approval with the choices save, edit then save, skip and stop; no choice (e.g. Enter alone) MUST count as skip.
- <a id="fr-041"></a>**FR-041**: System MUST save a reply to the mailbox only after the user chooses save or edit then save for that reply, and only as an unsent draft reply to its message.
- <a id="fr-042"></a>**FR-042**: System MUST end the approval step with a summary in the form "N saved (M edited), N skipped, N not saved", where replies left after "stop" (including the one on which it was chosen) and failed saves count as not saved.
- <a id="fr-043"></a>**FR-043**: System MUST NOT overwrite or change a draft that already exists in the mailbox.
- <a id="fr-044"></a>**FR-044**: System MUST show a message that already has a draft saved by the user as "draft already saved" with the saved text, and MUST NOT propose a new reply for it.
- <a id="fr-045"></a>**FR-045**: System MUST NOT save a draft for a message that was answered after the briefing was produced, and MUST tell the user so.
- <a id="fr-046"></a>**FR-046**: When an approved save fails, System MUST show the approved text, including any edits, and keep it in this run's briefing file.

**Safety (all stories)**
- <a id="fr-050"></a>**FR-050**: System MUST NOT send mail, accept, decline or book meetings, or create, change or delete any task or other record. The only record it creates is a draft the user approved under FR-041.

## Success Metrics

> *Inline guidance:* Every metric must be measurable — state the number and where it is read from. Include at least one guardrail metric: something that must NOT get worse while this feature succeeds.

| Metric | What it tells us | Direction |
|--------|------------------|-----------|
| Share of in-scope acceptance scenarios that pass, per system: target 100%, read from the test run report. A scenario is in scope on a system when its release has shipped and its dependency exists there; mail scenarios (Story 2 except its no-mailbox error path, and Story 5) count only on a system with a mailbox | The briefing does what it promises on both systems, as the seat's definition of finished requires | up |
| Share of proposed replies saved without editing: target at least 50%, read from the approval summaries (FR-042) over the test runs | Quality of the proposed replies; the likely deciding point in the 15 November comparison | up |
| Guardrail: number of messages sent, meetings booked, and records created or changed without approval — target 0, read by comparing mailbox, calendar and tasks before and after each test run | The seat's core rule: the agent drafts and proposes, a person approves | must not degrade (stays 0) |
| Guardrail: time for a full two-system briefing, excluding approval time — target under 60 seconds, read from the status line (FR-004) | Efficiency; usage is monitored and heavy use can be limited | must not degrade |

## Release Slicing

> *Inline guidance:* Cut the scope into shippable releases: v1 is the smallest cut that delivers the core user value, fast-follow lands right after, later is planned but not committed. Reference story / FR IDs instead of restating them.

| Release | Contents (stories / FRs) | Rationale for this cut |
|---------|--------------------------|------------------------|
| v1 — first task, 15 Nov | Story 1–4 (FR-001–FR-031, FR-050) | The full briefing the first task asks for, with proposed replies shown in it. It works today on meetings and tasks, and on mail as soon as a mailbox exists |
| fast-follow — 15 Nov if the Keystone sample mailbox exists in time, else 30 Nov version 1 | Story 5 (FR-040–FR-046) | Saving approved drafts can only be shown working with a mailbox, which depends on the platform team; detecting already-saved drafts (FR-044) only matters once drafts can be saved |
| later | Scheduled runs; replies that accept or decline meetings or commit to dates (see Out of Scope) | Not needed for the first task; booking-by-reply needs its own approval rules |

## Non-Functional Requirements

> *Inline guidance:* Record ONLY requirements that are non-standard for this change or project — deltas from the product's existing baseline. Never restate standards the product already meets (baseline performance budgets, standard authentication, org-wide accessibility policy). If nothing deviates from the baseline, write "None — baseline applies."

- Briefing output and briefing files must never contain login passwords or tokens. This is stricter than usual because the files are produced next to code in a public repository.
- A full two-system briefing must finish in under 60 seconds, excluding time spent approving, when the platform answers in its usual time, because platform usage is monitored and can be limited. Lists are never cut short to meet the limit; drafting is. The status line shows the real time, so a slow platform is visible.
## Dependencies

| Dependency | Type | Status |
|------------|------|--------|
| Seat-28 login `team28c` on Suryodaya and on Keystone | Required | Available (since 3 Nov) |
| Mail, calendar and task access for seat 28 on both systems | Required | Available (since 3 Nov) |
| A mailbox with sample mail for `team28c` on Keystone | Required for Story 2 mail and Story 5 | Pending — platform team, after the Release 16 test |
| A mailbox on Suryodaya | Optional (Story 2 and 5 on Suryodaya) | Pending — none announced |
| Test meetings and tasks created under the user's own login | Required for testing Story 3 and 4 | Available — create own test data |

## Out of Scope

- Sending any mail, or saving a draft without per-draft approval.
- Accepting, declining, booking or moving meetings, including replies that commit to a date or attendance.
- Creating, completing, reassigning or changing tasks.
- Changing or deleting any record the user did not create, including existing drafts.
- Scheduled or automatic briefing runs.
- Drive (files) content in the briefing.
- Briefings for other people's logins or mailboxes.

## Decisions

> *Inline guidance:* This is an appendix — reference-heavy sections like this decisions log (or a glossary, if you add one) belong in appendices at the end of the document so the body stays narrative. Record every non-trivial product decision (scope trade-offs, persona choices, priority calls). Each row gets a stable ID (`PRD-D-1`, `PRD-D-2`, …) so downstream artifacts can reference it. If there are no decisions, write "None."

| ID | Title | Context | Options | Choice | Rationale | Source | FR impact | Consequences |
|----|-------|---------|---------|--------|-----------|--------|-----------|--------------|
| <a id="prd-d-1"></a>PRD-D-1 | Where drafts live | Brief says "each reply as a draft"; rules say a person approves any record change | Briefing only; briefing plus approved save; auto-save every draft | Briefing plus a per-draft approved save to the mailbox | Meets "reply as a draft" literally while keeping a person on every record change; sets this version apart from one that only shows replies | Seat brief; grilling Q1 | FR-012, FR-040, FR-041 | Needs a mailbox; adds Story 5 |
| <a id="prd-d-2"></a>PRD-D-2 | Systems per run | Each system has its own login and data | One system per run; both, sectioned; both, merged | Both systems, a labelled section each, with an option to limit to one | One morning read; clear which system a draft belongs to | Grilling Q2 | FR-001, FR-002 | Partial failure must be handled per system (PRD-D-10) |
| <a id="prd-d-3"></a>PRD-D-3 | Which mail needs an answer | Unread includes newsletters and no-reply mail | All unread; unread minus no-reply noise, listed; only mail with a direct request | Unread, not draft, not answered, minus no-reply/automated/newsletter/own mail, each skipped one listed | Avoids pointless drafts but nothing disappears silently; testable | Grilling Q3 | FR-010, FR-011 | Skipped list in each Mail part |
| <a id="prd-d-4"></a>PRD-D-4 | Meaning of "today" | Users and data span IST and US Eastern | IST for all; local zone; per-system zone | Per system: Suryodaya IST, Keystone US Eastern (America/New_York); changeable per run | The user's direction; the US zone taken as Eastern because the seat lead works in it | Grilling Q4 (user answer) | FR-020, FR-021, FR-030 | Overdue boundary also follows each system's zone |
| <a id="prd-d-5"></a>PRD-D-5 | Meeting presentation | Calendars hold all-day, declined and overlapping events | Plain list; sorted with clashes and declined marked; accepted only, from now | All-day first, then by start time; clashes and declined marked; cancelled hidden; past meetings today kept | A briefing, not a dump; keeps follow-ups visible | Grilling Q4 | FR-020, FR-022, FR-023 | All-day events never cause a clash |
| <a id="prd-d-6"></a>PRD-D-6 | Whose overdue tasks | A login sees 60–70 overdue tasks made by others | All visible; mine plus a count of others; all, capped | Assigned to or created by the user, most overdue first, plus a count of others | Shows what the user must act on; count keeps the wider picture | Grilling Q5; Varun's test report | FR-030, FR-031 | Before own test tasks exist the Tasks part may show only the count |
| <a id="prd-d-7"></a>PRD-D-7 | How the briefing starts and is kept | "Daily" but trigger not specified | On demand; on demand plus dated file; scheduled | On demand, printed and saved as a dated file, rerun replaces that day's file; approval in the same run | Approval needs the person present; file is evidence for demo and comparison | Grilling Q6 | FR-005, FR-040 | Scheduling deferred to later |
| <a id="prd-d-8"></a>PRD-D-8 | Approval choices | How much control at save time | Yes/no; save/edit/skip/stop with skip default; approve all | Save, edit then save, skip, stop; no choice means skip | Fix drafts before saving; accidental key press saves nothing | Grilling Q7 | FR-040, FR-041, FR-042 | none |
| <a id="prd-d-9"></a>PRD-D-9 | Draft content | User judges each draft from the briefing alone | Sender/subject/draft; plus summary with non-committal draft; drafts that may commit | Sender, subject, received time, one-line summary, short polite draft in the mail's language, signed; never commits to date, money or attendance, marked "needs your decision" | Fast approval; follows "never books without a person" even if approved quickly | Grilling Q8 | FR-012, FR-013, FR-014 | Booking-by-reply deferred to later |
| <a id="prd-d-10"></a>PRD-D-10 | Partial failure | No mailbox today; logins expire every 7 days | Stop the run; continue with explicit reasons; hide failed parts | Continue; each failed part gives a plain reason and next step; closing status line | Mail is unavailable today; an empty inbox must not look like a broken one; reasons feed bug reports | Grilling Q9 | FR-003, FR-004 | Applies to every story's error handling |
| <a id="prd-d-11"></a>PRD-D-11 | Same-day draft already saved | A rerun would propose a second draft | Propose again; show "draft already saved"; offer to replace | Show "draft already saved" with its text; propose nothing new; never overwrite | No duplicates; no overwrite of a record | Grilling Q10 | FR-043, FR-044 | Ships with Story 5 |
| <a id="prd-d-12"></a>PRD-D-12 | 15 November cut | Draft saving needs a mailbox the platform hasn't created yet | All by 15 Nov; full briefing with gated save; meetings and tasks only | Full briefing (Story 1–4) for 15 Nov; Story 5 on 15 Nov only if the Keystone sample mailbox exists in time, else 30 Nov | Keeps the first-task promise honest against an outside dependency | Grilling Q11; instructor notices 4 Nov | FR-040–FR-046 | If the mailbox slips, raise it in the weekly update |
| <a id="prd-d-13"></a>PRD-D-13 | Success measures | Seat's definition of finished plus template's guardrail rule | Tests only; four measures with guardrails; without time limit | Scenarios pass on both systems; unedited-save share; zero unapproved changes; under 60 s | Correctness, draft quality and safety are each measured; time limit covers monitored usage | Grilling Q12; top-level README rule 6 | none | Test runs must snapshot data before and after |
| <a id="prd-d-14"></a>PRD-D-14 | Long lists | Inbox or task list may be long | Cap the list; show all | Show all, mail oldest first, tasks most overdue first | Decided by the agent, not grilled: a cap would hide items without a rule for which; revisit if briefings get too long | internal | FR-012, FR-030 | none |
| <a id="prd-d-15"></a>PRD-D-15 | Measurable reply and quality targets | Self-review: "short, polite" and the unedited-save metric had no checkable number | Leave unquantified; set numbers | Replies at most 120 words, opening with a greeting; at least 50% of proposed replies saved without editing | Decided by the agent, not grilled: makes FR-013 and the quality metric testable; adjust after the first real-mail runs | internal | FR-013 | Targets are first guesses, open to change at review |
| <a id="prd-d-16"></a>PRD-D-16 | Saving a one-system run and an empty run | Self-review: a one-system rerun would replace today's full briefing; a run that fetched nothing would replace a good file | Replace today's file; separate file per system; no file when nothing fetched | One-system runs are saved to a file named for the date and that system; a run that fetches nothing writes no file and keeps the earlier one | Decided by the agent, not grilled: a one-system run or an empty run never replaces today's full briefing | internal | FR-005, FR-007 | Up to three files per day (full, Suryodaya, Keystone); a full rerun that is only partly complete still replaces today's full file, and its status line shows what is missing |
| <a id="prd-d-17"></a>PRD-D-17 | How the user interacts | Stories assume a person runs it and approves in the same sitting | Interactive command in a terminal; chat assistant; web page | An interactive command the user runs in a terminal, with prompts for approval and files in a briefing folder | Decided by the agent, not grilled: follows from on-demand runs with approval in the same run (PRD-D-7) and matches how the seat's other briefings are run | internal | FR-040 | A chat or web front end is out of scope for the first task |
