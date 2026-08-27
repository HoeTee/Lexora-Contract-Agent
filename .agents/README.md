# Cursor Team Kit for Codex

This directory contains a one-time Codex migration of Cursor Team Kit 1.2.0.

- Upstream: <https://github.com/cursor/plugins/tree/main/cursor-team-kit>
- Upstream revision: `799151d91b6e12ee7dbd09f708eec108d7de9b3b`
- Snapshot date: 2026-08-27
- License: MIT; see `LICENSE.cursor-team-kit`

## Installed capabilities

- 18 project-scoped skills under `.agents/skills/`
- 2 project-scoped custom agents under `.codex/agents/`
- 2 converted rules in the repository `AGENTS.md`
- PR review canvas assets bundled with its skill
- Original plugin avatar preserved under `.agents/assets/`
- Original Cursor manifest preserved as inert source metadata in `.agents/upstream.cursor-plugin.json`; Codex does not load it as configuration.

## Runtime prerequisites

The migration does not install machine-level tools or project dependencies.

- GitHub PR and CI workflows require GitHub CLI (`gh`) and an authenticated account.
- UI automation and smoke workflows reuse an existing browser or test harness. They must not add Playwright merely to run a probe unless the user approves it.
- Examples from upstream that use `/tmp`, `python3`, Bash, or `tmux` must be translated to the current operating system. On Windows, use the process temp directory, `python`, PowerShell, and Codex's interactive command session where appropriate.

## Project authority

The repository `AGENTS.md` remains authoritative. A migrated skill never bypasses the required pre-change proposal, approval, checkpoint commit, final commit, or restrictions on external mutations.
