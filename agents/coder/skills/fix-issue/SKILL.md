---
name: fix-issue
description: Fix a GitHub issue end to end — reproduce, fix, test, and summarize. Use when asked to fix or resolve an issue by number.
argument-hint: "[issue-number]"
arguments: [issue]
allowed-tools: Bash(gh issue view *) Bash(git *)
---
Fix GitHub issue #$issue.

Current branch and status:
!`git -C "${CLAUDE_PROJECT_DIR}" status --short --branch 2>/dev/null || echo "not a git repository"`

Steps:
1. Read the issue: `gh issue view $issue`.
2. Reproduce the problem, ideally with a failing test.
3. Make the smallest fix; run the tests.
4. Ask the `code-reviewer` sub-agent to review the change.
5. Summarize the root cause and the fix. Helper: `${CLAUDE_SKILL_DIR}/scripts/summary.sh`.
