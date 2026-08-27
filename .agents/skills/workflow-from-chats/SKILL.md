---
name: workflow-from-chats
description: Extract durable working preferences from Codex conversation context or user-provided chat exports and convert them into skills, rules, or workflow docs. Use when asked to learn preferences, mine feedback, personalize workflows, or generate team/person-specific agent guidance.
---

# Workflow From Chats

Infer durable working preferences from recent chats. Do not summarize chats; extract reusable workflow guidance.

## Scope

- Default to the last 7 days unless the user asks for a different window.
- Use only conversation context exposed to the current Codex run, relevant returned subagent results, or transcripts explicitly provided by the user.
- If the requested historical window is not available in context, ask for an export or pasted transcript. Do not search hidden Cursor/Codex history or assume a local transcript store exists.
- Do not expose local transcript paths, secrets, customer data, private chat content, or credentials.

## Workflow

1. State the target workflow or preference surface in one paragraph.
2. Build an internal conversation inventory from available evidence: title/topic, approximate date, completion state, relevant returned subagent results, and why it may contain preference evidence.
3. Scan for explicit preferences, corrections, and workflow markers such as "I prefer", "always", "never", "not what I asked", "stop", "review", "PR", "CI", "logs", and "skill".
4. Extract preference atoms: trigger, workflow step, decision rule, quality bar, stop condition, evidence, and confidence.
5. Rate confidence as strong, medium, weak, or contradicted.
6. Cluster by workflow shape rather than transcript: shipping, review, simplification, debugging, capture, communication, delegation, or validation.
7. Choose the artifact: new skill, skill edit, rule, workflow doc, or no artifact.
8. Draft only the reusable guidance. Filter anecdotes that will not help future tasks.

## Confidence

- Strong: explicit user preference, workflow-changing correction, repeated user-visible conversation pattern, or direct request to encode behavior.
- Medium: accepted workflow, repeated tool/model/validation preference, or subagent consensus that the parent used successfully.
- Weak: agent-chosen behavior with no user feedback, one ambiguous transcript, or a likely task-specific correction.
- Contradicted: evidence points in incompatible directions; ask the user before writing files.

## Artifact Choice

- Skill: recurring multi-step workflow with clear triggers.
- Rule: general behavior that should apply broadly.
- Workflow doc: useful context that is not reliably triggerable.
- No artifact: situational, stale, or low-confidence observation.

## Output

Return a concise synthesis first:

- Target workflow.
- Evidence corpus with user-visible conversation references only.
- Preference profile.
- Adopt, consider, dismissed.
- Proposed artifacts.
- Open questions only if they block writing.
