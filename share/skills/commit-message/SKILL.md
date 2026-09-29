---
name: commit-message
description: Write a conventional commit message for the staged changes. Use when asked to commit or to draft a commit message.
allowed-tools: Bash(git diff *) Bash(git log *)
---
Draft a commit message for the staged changes.

Staged diff summary:
!`git diff --cached --stat 2>/dev/null || echo "no git repository"`

Rules:
- Subject: `<type>(<scope>): <summary>`, imperative mood, max 72 characters.
  Types: feat, fix, docs, refactor, test, chore, perf, ci.
- Body: why the change was made, wrapped at 72 characters.
- Match the style of recent history (`git log --oneline -10`).
