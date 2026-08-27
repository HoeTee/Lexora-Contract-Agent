---
name: run-smoke-tests
description: Discover and run the repository's existing smoke tests, debug failures, and verify fixes without silently adding a new browser-test dependency
---

# Run smoke tests

## Trigger

Need end-to-end smoke verification before or after changes.

## Workflow

1. Discover the repository's documented smoke/E2E harness and its prerequisites; do not assume npm or Playwright exists.
2. Build only the prerequisites already declared by the project.
3. Run the relevant smoke suite or a focused test file.
4. If failing, inspect traces/logs and isolate the root cause.
5. Follow the repository `AGENTS.md` before edits, apply a minimal approved fix, and rerun until stable.

## Example Commands (only when the repository defines these scripts)

```bash
# Run full smoke suite
npm run smoketest

# Run a specific smoke test file
npm run smoketest -- path/to/test.spec.ts

# Faster iteration when build artifacts are ready
npm run smoketest-no-compile -- path/to/test.spec.ts
```

## Guardrails

- Prefer deterministic waits and assertions over brittle timeouts.
- Re-run passing fixes to reduce flaky false positives.
- Quarantine tests only when explicitly requested and documented.
- If no smoke harness exists, report the gap. Do not install Playwright or introduce a new test stack without user approval.

## Output

- Test results summary
- Root cause and fix
- Remaining flake risk (if any)
