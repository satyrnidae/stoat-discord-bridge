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
   reserved for the PR).

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
- Once the PR is open, swap the issue's work-state label: `gh issue edit <n>
  --remove-label "in development" --add-label "merge pending"`. Development
  is done at this point (an open PR is what "merge pending" means) even
  though the issue itself isn't closed until the PR merges.
- Report the PR URL back to the user.

## Notes

- Narrate progress between phases (which phase, red/green status, review
  findings) even though each individual step is "silent, best-effort" work -
  this is a long-running loop, not a single tool call, and the user should
  be able to tell where it is without reading the transcript.
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
