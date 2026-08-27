---
name: fix-ci
description: Find failing PR checks, inspect logs or external check links, and apply focused fixes
---

# Fix CI

## Trigger

Branch or PR CI is failing and needs a fast, iterative path to green checks.

## Workflow

1. Verify `gh --version` and `gh auth status`; if unavailable, report the prerequisite and stop without installing or authenticating silently.
2. Resolve the active PR and inspect `gh pr checks --json name,bucket,state,workflow,link`.
3. Inspect failed jobs and extract the first actionable error. Use GitHub Actions logs when available; otherwise use the check link to identify the failing command or service.
4. Follow the repository `AGENTS.md` proposal/approval and commit protocol, then apply the smallest safe fix.
5. When the user's request authorizes remote mutation, push, re-check the PR check set, and repeat until green.

## Guardrails

- Fix one actionable failure at a time.
- Prefer minimal, low-risk changes before broader refactors.
- Keep `gh pr checks` as the source of truth for overall PR CI state.

## Output

- Primary failing job and root error
- Fixes applied in iteration order
- Current CI status and next action
