---
name: review-and-ship
description: Review the current branch for bugs, intent fit, and test coverage; run or write tests; commit focused work; open or update a PR.
---

# Review and ship

## Trigger

Reviewing changes before shipping. Close key issues, verify behavior, and open or update a PR.

## Workflow

1. Gather context: diff against the discovered base branch, uncommitted changes, recent commits, changed files, and user intent from conversation context available to the current Codex run.
2. Run targeted tests for changed behavior. If no focused tests exist, decide whether to add them or document the gap.
3. Review for correctness, regressions, security, and intent fit. Use parallel subagents only when the user or applicable project/skill instructions explicitly request delegation.
4. Fix critical issues before finalizing and re-run affected tests.
5. Commit selective files with a concise message.
6. Push branch and open or update a PR.

## Suggested Checks

```bash
# Discover the base branch first; do not assume it is main.
git diff origin/<base-branch>...HEAD
git status
gh pr checks --json name,bucket,state,workflow,link
```

## Guardrails

- Prioritize correctness, security, and regressions over style-only comments.
- Keep commits focused and avoid unrelated file changes.
- If pre-commit checks fail, fix the issues rather than bypassing hooks.
- Use `gh pr checks` instead of GitHub Actions-only commands when judging PR readiness.
- Verify `gh --version` and `gh auth status` before GitHub operations. If unavailable, report the prerequisite instead of installing or authenticating silently.
- Follow the repository `AGENTS.md` proposal/approval and before/after commit protocol before editing code.
- Treat push and PR creation/update as external mutations; perform them only when the user's request authorizes shipping.

## Output

- Findings summary (critical, warning, note)
- Tests run and outcomes
- PR URL
