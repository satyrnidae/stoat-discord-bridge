---
name: implement-issue
description: Implement a triaged issue (bug/feat/chore) end to end - worktree isolation, a plan-then-red-test-then-implement-then-regression cycle per phase with /code-review low checkpoints and single-scope commits, then a pull request with closing keywords.
argument-hint: "[issue-number]"
---

# Implement issue: plan → red test → implement → regression, PR at the end

Takes a triaged issue (one that's been through `/triage-issue`, or otherwise
carries a `bug`/`feat`/`chore` label and a recorded plan) from a clean
worktree to an open pull request, working the plan one phase at a time under
strict TDD, with a review checkpoint and a single-scope commit per phase.

## Posting identity

Everything this skill writes to GitHub (labels, comments, the PR) goes out
as the **satyrnidaebot** GitHub App, not the repo owner's account. In the
headless pipeline runs, `gh` already is the app's wrapper
(`/home/claude/triage-webhook/bin/gh`, first on PATH). In an interactive
session, check `command -v gh`: if it isn't that wrapper but the wrapper
exists, call the wrapper by its full path for every `gh` command below. If
the wrapper doesn't exist (another machine or user), plain `gh` posts as
whoever is logged in - say so when you report back. `git push` is unaffected
(it uses the SSH key, not `gh`).

## Progress comments

Narrate the work on GitHub as you go, not just in the session - in the
headless pipeline nobody reads the transcript, and the bot's own comments
don't feed back into it (the harness only forwards the owner's). Short and
factual, one comment per milestone, unwrapped paragraphs like every other
comment here (`gh issue comment <n> --body-file <tmpfile>`):

- **Start** (step 1): the phase breakdown you derived from the plan, as a
  numbered list, plus anything you're reading differently from the plan.
- **Each phase committed** (step 2.6): which phase, the commit (short SHA +
  subject), the test it added, and anything worth knowing - a review
  finding you applied or deliberately skipped, a surprise in the code, a
  change to a later phase's plan.
- **Blocked or deviating**: as soon as it happens, not at the end - what's
  in the way and what you're doing about it. If you need the owner's call,
  ask and stop (the headless note explains how they reply).
- **PR open** (step 4): the PR link and a line on anything left for review.

Post on the issue until the PR exists; after that, follow-up runs (owner
feedback on the PR) reply on the PR instead. Skip the comments only when
the user running the skill interactively asks you to.

## 0. Resolve the issue and its plan

- Issue number comes from the argument. If omitted, try to infer it from the
  current branch name (`bug/<n>-...`, `feat/<n>-...`, `chore/<n>-...`, per
  `CLAUDE.md`'s branch-prefix convention); otherwise ask.
- Fetch it: `gh issue view <n> --json number,title,body,labels,url`.
- If it doesn't yet carry a `bug`/`feat`/`chore` label (still `investigation`
  / `planning`, or unlabeled), stop and say so - run `/triage-issue <n>`
  first, or confirm explicitly with the user, rather than implementing
  against no recorded plan.
- Read the plan already in the issue body (from the triage pipeline:
  Summary / Root Cause-Scope / Approach / Files affected / Tests / Open
  questions). Break "Approach" + "Files affected" into an ordered list of
  implementation phases - each phase should be one coherent, independently
  testable unit of work (roughly one file/module's worth of change, and
  later, one commit).
- If the body has no such structured plan (the issue was labeled by hand,
  skipping triage), do a lightweight version of triage's planning step
  yourself first - re-derive the same phase breakdown from the issue text
  and codebase - before continuing. You don't need `/triage-issue`'s
  comment/label choreography here, just its planning rigor.

## 1. Worktree

- If this session is already isolated for this work (already on a
  `bug/<n>-.../feat/<n>-.../chore/<n>-...` branch, already inside a
  worktree, or launched with worktree isolation by the harness) - use it as
  is, don't create a second one.
- Otherwise, create one with `EnterWorktree`, then create/check out a branch
  inside it named per `CLAUDE.md`'s Contributing convention:
  `bug/<n>-<slug>` / `feat/<n>-<slug>` / `chore/<n>-<slug>` (slug = a short
  kebab-case version of the issue title).
- Mark the issue as being worked: `gh issue edit <n> --add-label "in development"`.
  Do this regardless of whether a worktree/branch already existed - it's the
  signal that this issue has an active implementation session, not that a
  worktree was just created. Safe to re-run if it's already set.
- Post the start comment (see Progress comments).

## 2. The per-phase loop

For each phase from step 0, in order:

1. **Plan** - state the phase's concrete scope in a sentence or two (which
   file(s), what behavior) before touching code. This is a checkpoint
   against the plan already recorded on the issue, not a fresh planning
   pass - the issue has already been through investigation/planning, so
   don't re-enter `EnterPlanMode` for it. Only fall back to real plan mode
   if this issue skipped triage entirely and the approach is still
   genuinely open (see step 0's fallback).
2. **Red** - write a test capturing the phase's expected behavior and
   confirm it fails for the expected reason (not an unrelated error - a
   fixture bug, a typo, a bad import). Follow this repo's test layout from
   `CLAUDE.md` (`tests/` mirrors `src/`; a package that's outgrown one file
   gets its own `tests/<package>/` directory alongside a shared
   `conftest.py`).
3. **Implement** - write the minimum code to make that test pass. Don't
   reach ahead into a later phase's territory.
4. **Regression** - run the full test suite (`pytest`), not just the new
   test, before moving on. A phase isn't done until the whole suite is
   green.
5. **Checkpoint** - run `/code-review low` against the phase's diff
   (`Skill` with `skill: "code-review"`, `args: "low"`). Apply high-
   confidence findings before committing; use judgement on low-confidence
   ones rather than applying everything a low-effort pass surfaces
   unquestioned.
6. **Commit** - one single-scope commit for the phase (test + implementation
   + any review fixups together, unless a fixup is substantial enough to
   warrant its own commit - then split it out). Gitmoji-prefixed message per
   `CLAUDE.md`'s Contributing section (`✨`/`🐛`/`♻️`/`✅` as fits), body
   referencing the issue (`Refs #<n>` - not a closing keyword yet; that's
   reserved for the PR). Then post the phase's progress comment.

Repeat until every phase from the plan is implemented.

## 3. Final regression

Run `pytest` once more after the last phase (plus anything else `CLAUDE.md`'s
Commands section calls for, e.g. a build check) - one last confirmation the
whole branch is coherent, not just each phase in isolation.

## 4. Pull request

- Push the branch and open the PR (`gh pr create`), with the body ending in
  a GitHub closing keyword tied to the issue - `Closes #<n>` (or `Fixes #<n>`
  for a `bug`-labeled issue) - so merging auto-closes it.
- Summarize the phases as the PR's bullet points, framed around *why* each
  one exists (matching this repo's own commit/PR style), not a mechanical
  diff recap.
- Write the PR body's prose as unwrapped paragraphs - don't insert manual
  line breaks partway through a sentence or paragraph to keep lines short.
  Let the rendering client wrap it; a hard-wrapped body reads as jagged,
  broken lines on GitHub instead of flowing paragraphs.
- Once the PR is open, swap the issue's work-state label: `gh issue edit <n>
  --remove-label "in development" --add-label "merge pending"`. Development
  is done at this point (an open PR is what "merge pending" means) even
  though the issue itself isn't closed until the PR merges.
- Post the PR-open comment on the issue, and report the PR URL back to the
  user.

## Notes

- Narrate progress between phases (which phase, red/green status, review
  findings) in the session too, not only in the GitHub comments - this is a
  long-running loop, and an interactive user should be able to tell where it
  is without opening the issue.
- Don't skip the regression run to save time - interleaving it every phase
  is what catches cross-phase breakage while it's still cheap to localize.
- If a `/code-review low` checkpoint surfaces something that invalidates a
  later phase's planned approach, adjust that phase's plan before starting
  it rather than pushing forward against a plan you already know is stale.
- If committing inside the worktree fails with a git object-permission
  error, that's an environment/ACL issue, not a code problem - surface it to
  the user rather than working around it with `--no-verify` or similar.
- Work-state labels track exactly one active phase each: `in development`
  from step 1 until the PR opens, then `merge pending` from PR-open onward
  (step 4). If this run stops or errors out before a PR opens, leave
  `in development` set rather than removing it - it still accurately
  describes the issue's state, and the next run of this skill picks it back
  up. This skill doesn't remove `merge pending` itself - that's a signal for
  whatever merges the PR, not something this pipeline's scope covers.
