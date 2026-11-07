# Retrospective: implement — 2026-11-05-daily-briefing

**Date:** 2026-11-07
**Phase:** Implement
**Session driver:** `implement` (Slice 3: Approve drafts into the mailbox)
**Artifact reviewed:** 28-ea/docs/sdlc/changes/2026-11-05-daily-briefing/plan.md (the implement path keeps no implementation-log.md; slice status, verify evidence and deviations live in plan.md)
**Self-review:** CAP_REACHED — round-2 findings fixed, not re-reviewed
**Self-review iterations:** 2
**Template version:** default/0.2.1

**Plugin version:** 0.7.7
**Agent Sessions:** claude-code: a341ba67-5d61-4111-8552-e420bc970ec3

---

## Summary

Slice 3 went smoothly: one chunk-worker, one live save made by the user, one end-review round with a fix-worker, and the user approved all five deviation rows in one line. There were three points of friction. Commit signing failed under the sandbox, so the same slice holds both signed and unsigned commits. The user's live save did not match the plan's write count. The orchestrator hit a shell dead-end in its own verify run.

## Friction Timeline

| #   | Moment (quote / paraphrase) | Bucket | Placement target | De-dup |
| --- | --------------------------- | ------ | ---------------- | ------ |
| 1   | The orchestrator's first commit failed with "Couldn't load public key /Users/jadhavsa/.ssh/id_ed25519.pub … fatal: failed to write commit object", and it then committed outside the sandbox. The first worker's 8 commits came back with "They are unsigned: signing needs `~/.ssh`, which the sandbox denies". The worker did not say how it made them unsigned. The fix-worker retried outside the sandbox and signed its commit. So the slice holds both signed and unsigned commits | `skill` | HUMAN-REVIEW (branch 3: no shared skill owns commit rules), see Placement Audit | none (no skill mentions commit signing; grep of `signing`, `gpgsign`, `unsigned` under `skills/` matched only bugfix's unrelated CI example) |
| 4   | The plan said "one approved save on Keystone (diff exactly one new draft)", and the hand-off said "`s` then Enter on reply 1. Then plain Enter (skip) on every later offer". The user saved three drafts, which the orchestrator learned only from the worker's snapshot diff ("the user's saves added three drafts, not one"). The orchestrator then had to ask, and the user said "save all three daraf" | `skill` | `5 → skills/implement/SKILL.md` step 4 (human steps) | partial-match → skills/implement/SKILL.md:75 |
| 5   | The orchestrator's final-verification run put the test command in a shell variable. zsh did not split it ("no such file or directory: python3 -m unittest discover …"), so every verify had to be run again | `context` | n/a | n/a |
| 6   | Same moment as #4, from the user's side: three saves where the plan expected one | `hint` | n/a | n/a |
| 7   | Same moment as #1, from the user's side: the sandbox can't read the SSH signing key | `hint` | n/a | n/a |

Rows 2–3 of the first draft (copies of row 1 for the other worker prompts) were folded into row 1 when it went to HUMAN-REVIEW.

---

## `context` — Missing / Bad Context

| #   | Finding | Where it should live | Proposed change |
| --- | ------- | -------------------- | --------------- |
| 5   | The user's shell is zsh, which does not word-split an unquoted variable, so `T="python3 -m unittest …"; $T -p x` fails. The quote is in row 5 | Agent memory (user-specific environment, cross-project) | Add a `feedback` memory: "User's shell is zsh: never store a multi-word command in a variable and run `$VAR`; use a shell function (`T() { python3 -m unittest … "$@"; }`) or write the command out. **Why:** the slice 3 verify run had to be redone. **How to apply:** any Bash call that reuses a command." |

## `skill` — Bad Skill Instructions

> _Inline guidance:_ Pick the skill via the Placement Decision Tree in `skills/retrospective/SKILL.md` — do not write the skill path until the tree resolves. Common confusions: tool-use rules → shared skill (`grill-me`, `retrospective`), not the phase skill they were observed in; process-pieces present in multiple phase skills → mirror to all (one row per skill), do not centralize.

**Placement and specificity check.** For rows 1 and 4, every project-specificity indicator is NO: git commit signing, the sandbox and a live write handed to a human have nothing to do with this project.

- **Row 1** reaches branch 3. It is a tool-use rule, and every skill that commits needs it: the orchestrator in `implement`, the three worker prompts, `light-implement`, `review-implementation`, `prd`, `plan` and the retrospective's own commit. But `skills/shared/` holds only `subagent-self-review.md`, so no shared skill owns commit rules, and the finding goes to HUMAN-REVIEW.
- **Row 4** falls under branch 5. Only `implement` holds the "needing a human" hand-off text; grep found none in `light-implement` or `implement-deprecated`.

| #   | Skill (per decision tree) | Section / line | Problem (with quote) | Proposed diff |
| --- | ------------------------- | -------------- | -------------------- | ------------- |
| 1   | HUMAN-REVIEW. Candidate: a new shared reference, `skills/shared/committing.md`, cited from every skill that commits. The interim alternative is to mirror the rule into each commit step | Every commit step: `implement/SKILL.md` step 4's trailer paragraph, `implement/worker-prompt.md` step 5, `light-implement/chunk-worker-prompt.md` and `implement-deprecated/chunk-worker-prompt.md` step 5 (line 43 onward), plus the commit steps of `light-implement`, `review-implementation`, `prd`, `plan` and `retrospective` | No skill says what to do when a commit fails on signing. In this session the orchestrator retried outside the sandbox, the first worker produced unsigned commits by a method it didn't report, and the fix-worker retried outside the sandbox. The result is mixed signing in one slice | **Add** this rule, either to the shared reference or to each commit step: "If a commit fails on signing (for example, the key is under a path the sandbox denies), retry the same commit outside the sandbox. Never turn signing off (`--no-gpg-sign`, `-c commit.gpgsign=false`), and never make a commit unsigned that the repository would sign. If the retry is refused, stop and report the signing error." |
| 4   | `skills/implement/SKILL.md` | Step 4, the sentence "For a task the plan marks as needing a human, decide before dispatch which steps need the user's own access … you hand those steps to the user." (line 75) | The hand-off gave the keys to type but not the outcome the plan expects. When the user did more than the plan said (three saves, not one), the orchestrator learned it only after the worker had run the verify ("the user's saves added three drafts, not one") and then had to ask ("save all three daraf") | **Reword** the end of the sentence. Before: "…you hand those steps to the user." After: "…you hand those steps to the user, with the outcome the plan expects, such as 'exactly one new draft', and ask them to say so before they start if they mean to do more or less. Record their answer, so the evidence check expects the outcome they chose." |

## `template` — Template Changes

> _Inline guidance:_ Template scope is the template the phase actually loaded — if `<project>/docs/sdlc/templates/<phase>.md` or the legacy `<project>/docs/agentic/templates/<phase>.md` exists, target the one the phase loaded; otherwise target the plugin template (default or variant) the phase used. Attachment level: `section-inline` for rules that constrain one section's content, `top-guidance` for whole-artifact rules, `structural` for adding/reordering sections.

None. The plan template did not cause friction in this session.

## `hint` — Hints for the User

| #   | Hint | Why it helps |
| --- | ---- | ------------ |
| 6   | Next time a step asks you to write to a live system (here, "type `s` on reply 1, then Enter"), and you want to do more or less than that, say so in your reply before you run it. For example: "I'll save all three." | The evidence check then expects the right count, and no follow-up question is needed after the diff comes back. |
| 7   | Decide up front how commits get signed in agent sessions. Either set up signing that works under the sandbox without opening `~/.ssh` to it, or accept that each signed commit runs outside the sandbox through the permission prompt, and say which one at the start of the session. | Then no worker has to guess, and no commits end up unsigned in a slice where the others are signed. |

---

## Placement Audit (HUMAN-REVIEW queue)

| #   | Finding | Why ambiguous | Decision tree branch reached |
| --- | ------- | ------------- | ---------------------------- |
| 1   | Commit-signing rule (Friction Timeline row 1) | A tool-use rule that every committing skill needs, but `skills/shared/` holds no commit reference, so it's unclear whether to create a new shared reference or mirror the rule into about nine commit steps | 3 — owned by a shared skill, but none exists |

**Round-2 self-review finding (fixed, not re-reviewed), verbatim:** "Hint #7: 'Consider letting the sandbox read your SSH signing key, or set signing so it works under the sandbox (manage this with `/sandbox`).' Problem: the first option tells the user to open `~/.ssh` to the sandbox. That weakens credential protection, and the session's sandbox guidance says not to suggest adding sensitive paths such as `~/.ssh/*` to the allowlist. The row's 'Why it helps' text and Next Action 'Hint #7: decide whether to change the sandbox settings for commit signing' carry the same suggestion. Fix: drop the 'letting the sandbox read your SSH signing key' option. Keep only the neutral option, for example 'set up commit signing that works under the sandbox, or accept that signed commits run outside the sandbox through the permission prompt'. Reword 'Why it helps' and the Next Action to match."

---

## Proposed Next Actions

- [ ] Finding #1: decide where the commit-signing rule lives (a new `skills/shared/committing.md`, or mirrored into each commit step), then apply it in a new session.
- [ ] Finding #4: in a new session, reword the human-step sentence in `skills/implement/SKILL.md` step 4.
- [ ] Finding #5: save the zsh memory.
- [ ] Hint #7: choose how commits get signed in agent sessions (signing that works under the sandbox, or signed commits through the permission prompt).

## Notes

The three Keystone drafts the user saved stay in the mailbox, unsent, as intended (implementation deviation row 30).
