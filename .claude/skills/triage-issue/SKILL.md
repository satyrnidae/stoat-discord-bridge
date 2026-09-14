---
name: triage-issue
description: Work a GitHub issue through this repo's intake pipeline - investigate an issue labeled "investigation", comment the findings, hand off to "planning", write an implementation plan into the issue body (preserving any linked images), then relabel it by type (bug/feat/chore/docs).
argument-hint: "[issue-number]"
---

# Triage issue: investigate → plan

Runs one or more issues through this repo's investigation → planning pipeline
end to end. Uses the `gh` CLI against the current repo (origin) - no need to
pass `--repo` unless the user names a different one.

## 1. Pick the issue(s)

- If an issue number was passed as the argument, use that issue. Check
  whether it currently carries the `investigation` label (`gh issue view <n>
  --json labels`). If it doesn't have it (and doesn't already have
  `planning` or a type label like `bug`/`feat`/`chore`/`docs` from a prior
  run of this pipeline), add it - `gh issue edit <n> --add-label
  investigation` - and proceed; an explicit issue number is itself the
  human signal to triage this issue now. If it's already further along
  (`planning` or a type label set), tell the user and confirm before
  restarting the pipeline on it, rather than silently reprocessing
  already-triaged work.
- If no argument was given, list every open issue labeled `investigation`:
  `gh issue list --label investigation --state open --json number,title,createdAt`,
  sort oldest first, and process them one at a time in that order. Tell the
  user up front how many you found before starting.
- If none are found, say so and stop - nothing to do.

## 2. Investigate

For the issue at hand:

- Fetch full context: `gh issue view <n> --json number,title,body,labels,comments,url`.
- Read the report closely - reproduction steps, error text, affected
  commands/config, and any linked images/screenshots in the body (markdown
  `![...](url)` image syntax or attached GitHub asset links).
- Search the codebase for the relevant subsystem (Explore/Grep/Read), using
  this repo's `CLAUDE.md` architecture map (service kind, `admin_commands`
  linker, storage repo, etc.) as your starting point rather than guessing
  cold.
- Check history where it helps - `git log` / `git blame` on the suspect
  files, and related prior issues/PRs (`gh pr list --search "..."`,
  `gh issue list --search "..."`) - especially if `CLAUDE.md` already
  documents a caveat or known-unverified area that matches.
- Reach a concrete conclusion: for a bug, a root-cause hypothesis with a
  confidence level and the specific files/functions involved; for a feature
  request, a concrete scope (what's in vs. out) and where it would plug into
  the existing architecture. Name any open question you couldn't resolve
  from static reading alone (this repo already flags several
  "unverified against a live server" areas in `CLAUDE.md` - say so if the
  issue lands in one of those).
- If this looks like a long investigation, or you're processing several
  issues in one run, consider forking (`Agent` with `subagent_type: "fork"`)
  per issue to keep exploration noise out of your own context.

## 3. Post the investigation summary

- Write a concise comment: what you found, root cause / scope, confidence,
  affected files, open questions. Post it via a temp file, not an inline
  `--body`, so markdown survives shell escaping:
  `gh issue comment <n> --body-file <tmpfile>`.
- Do not hand-wrap the comment text with manual line breaks (unlike this
  SKILL.md's own prose, which is wrapped for the editor) - write each
  paragraph as one continuous line and let GitHub's renderer soft-wrap it.
  GitHub markdown doesn't treat a bare newline as a line break, so
  hand-wrapped text can render with odd mid-sentence joins. Blank lines
  between paragraphs and list-item newlines are fine.
- Then flip the label: `gh issue edit <n> --remove-label investigation --add-label planning`.

## 4. Plan

- Design the concrete implementation: which files change, the shape of the
  fix/feature, what tests to add (this repo's `tests/` mirrors `src/` layout
  - see `CLAUDE.md`'s Tests section for the per-package split, including
  when a package outgrows one file and becomes its own `tests/<package>/`
  directory), and any edge cases or follow-on considerations.
- Keep the plan scoped to what `CLAUDE.md`'s engineering norms ask for - no
  speculative abstractions, no unrelated cleanup riding along.
- If the plan should mention how the change will land, use `CLAUDE.md`'s
  Contributing conventions: branch prefix (`bug/*`, `feat/*`, `chore/*`) and
  Gitmoji commit-message convention.

## 5. Update the title if the intent has drifted

- Compare the planned approach against the current title. Investigation
  often narrows or reshapes the original ask (a vague report turns out to be
  a different bug than the title implies, a "fix X" request turns into a
  scope change, etc.). If what you're actually planning to build/fix no
  longer matches the title, rewrite the title to reflect the real intent -
  don't leave a stale title pointing at the original (possibly mistaken)
  framing.
- `gh issue edit <n> --title "<new title>"`. Keep it in the same terse,
  descriptive style as the original (no issue-number/label text baked into
  it). If the title still fits, leave it alone.

## 6. Write the plan into the issue body

- Build the new body as the plan itself, with clear headings: Summary, Root
  Cause / Scope, Approach, Files affected, Tests, Open questions.
- Before overwriting, pull any image links out of the *current* body
  (markdown image syntax, or bare links to user-attached screenshots/GitHub
  asset URLs) and carry them forward - append them under a trailing
  `### Original report` or `### Attachments` section so screenshots attached
  to the original report aren't lost when the body is replaced. Carry
  forward any reproduction steps that are still relevant for whoever
  implements the plan, too - the goal is to replace "here's what's broken"
  with "here's the plan," not to erase evidence.
- Same rule as the comment in step 3: no manual line breaks within a
  paragraph - write each paragraph/bullet as one continuous line and let it
  soft-wrap. Headings and list-item boundaries are real breaks; mid-paragraph
  ones aren't.
- Overwrite with `gh issue edit <n> --body-file <tmpfile>`.

## 7. Relabel by type

- Remove `planning`, add the label matching the change's nature: `bug`,
  `feat`, `chore`, or `docs` (check `gh label list` if unsure which exist).
  Use `CLAUDE.md`'s branch-prefix categories (bug/feat/chore) as the primary
  guide; use `docs` for documentation-only issues.
- `gh issue edit <n> --remove-label planning --add-label <type>`.

## 8. Report back

After each issue (or at the end of a batch run), summarize to the user:
issue number/title (noting if it was renamed), what was found, the label
transitions made, and a link to the issue.

## Notes

- Treat each label transition as a signal other humans/tools rely on -
  don't skip a step or leave an issue in an inconsistent state (e.g. both
  `investigation` and `planning` set, or neither). If you must stop partway
  through an issue, say clearly which step you reached.
- This skill only *consumes* the `investigation` label - it never adds it
  itself; an issue reaches this pipeline by a human labeling it first.
- Every comment and body update written to GitHub (steps 3 and 6) must be
  free of manual line breaks within a paragraph - see those steps for why.
