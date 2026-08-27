---
name: new-branch-and-pr
description: Create a fresh branch, complete work, and open a pull request
---

# New branch and PR

## Trigger

Starting work that should be shipped through a clean branch and pull request workflow.

## Workflow

1. Ensure the working tree is clean or explicitly handled.
2. Discover the repository's default branch and create a descriptive branch from its latest authorized local or remote state.
3. Complete implementation and tests.
4. Commit focused changes and push.
5. Create a concise PR with summary and test notes.

## Guardrails

- Keep branch scope focused on one change set.
- Include verification notes before requesting review.
- Follow the repository `AGENTS.md` proposal/approval and before/after commit protocol.
- Verify `gh --version` and `gh auth status` before creating the PR. Do not install, authenticate, push, or open a PR unless the user's request authorizes those external changes.

## Output

- New branch name
- PR summary and test notes
- PR URL
