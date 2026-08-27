---
name: get-pr-comments
description: Fetch and summarize review comments from the active pull request
---

# Get PR comments

## Trigger

Need a concise, actionable summary of feedback on the active pull request.

## Workflow

1. Verify `gh --version` and `gh auth status`; if unavailable, report the prerequisite and stop without installing or authenticating silently.
2. Resolve the active PR for the current branch.
3. Fetch review comments and discussion comments.
4. Group feedback by severity and actionability.
5. Return a concise action list.

## Output

- Grouped feedback summary
- Action list ordered by priority
- Open questions that still need clarification
