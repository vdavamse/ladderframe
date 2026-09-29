"""Claude Code style permission rules.

    Bash                 every Bash call
    Bash(git *)          Bash calls whose command matches the glob
    Read(/etc/**)        Read calls on matching absolute paths (as written, and with symlinks resolved)
    Agent(reviewer)      spawning the `reviewer` sub-agent (`Task(...)` is an alias)
    WebFetch(domain:example.com)
    github_*             every tool of the `github` MCP server

Evaluation: a matching deny rule wins, then a matching allow rule (or a skill's temporary
`allowed-tools` grant), then the configured default.
"""

from __future__ import annotations

import fnmatch
import re
from dataclasses import dataclass, field

TOOL_ALIASES = {"Task": "Agent"}

_RULE = re.compile(r"^\s*([^()\s]+)\s*(?:\((.*)\))?\s*$")


def canonical_tool_name(name: str) -> str:
    return TOOL_ALIASES.get(name, name)


@dataclass(frozen=True)
class Rule:
    tool: str
    specifier: str | None = None

    @classmethod
    def parse(cls, text: str) -> Rule:
        match = _RULE.match(text)
        if not match:
            raise ValueError(f"invalid permission rule: {text!r}")
        tool, spec = match.group(1), match.group(2)
        return cls(canonical_tool_name(tool), spec.strip() if spec is not None else None)

    def matches_tool(self, tool: str) -> bool:
        return fnmatch.fnmatchcase(tool, self.tool)

    def matches(self, tool: str, subject: str | None) -> bool:
        if not self.matches_tool(tool):
            return False
        if self.specifier is None:
            return True
        if subject is None:
            return False
        return any(fnmatch.fnmatchcase(subject, pattern.strip()) for pattern in self._patterns())

    def _patterns(self) -> list[str]:
        # Agent(worker, researcher) lists several names; other specifiers are a single glob.
        if self.tool == "Agent":
            return [p for p in self.specifier.split(",")] if self.specifier else []
        return [self.specifier] if self.specifier else []

    def __str__(self) -> str:
        return self.tool if self.specifier is None else f"{self.tool}({self.specifier})"


class PermissionDenied(Exception):
    pass


@dataclass
class Permissions:
    default: str = "allow"
    allow: list[Rule] = field(default_factory=list)
    deny: list[Rule] = field(default_factory=list)
    grants: list[Rule] = field(default_factory=list)
    """Temporary allow rules from a skill's `allowed-tools`, cleared at the end of the run."""
    restrictions: list[Rule] = field(default_factory=list)
    """Temporary deny rules from a skill's `disallowed-tools`, cleared at the end of the run."""

    @classmethod
    def from_config(cls, default: str, allow: list[str], deny: list[str]) -> Permissions:
        return cls(default, [Rule.parse(r) for r in allow], [Rule.parse(r) for r in deny])

    def is_allowed(self, tool: str, subject: str | None = None) -> bool:
        tool = canonical_tool_name(tool)
        if any(rule.matches(tool, subject) for rule in (*self.deny, *self.restrictions)):
            return False
        if any(rule.matches(tool, subject) for rule in (*self.allow, *self.grants)):
            return True
        return self.default == "allow"

    def check(self, tool: str, subject: str | None = None) -> None:
        if not self.is_allowed(tool, subject):
            target = f" for {subject!r}" if subject else ""
            raise PermissionDenied(f"Permission denied: {tool}{target} is not allowed by the agent's permission rules.")

    def tool_blocked(self, tool: str) -> bool:
        """True if the tool is unusable for every input (an unqualified deny rule, or default deny with no allow)."""
        tool = canonical_tool_name(tool)
        if any(rule.specifier is None and rule.matches_tool(tool) for rule in (*self.deny, *self.restrictions)):
            return True
        if self.default == "deny":
            return not any(rule.matches_tool(tool) for rule in (*self.allow, *self.grants))
        return False

    def grant(self, rules: list[str]) -> None:
        self.grants.extend(Rule.parse(r) for r in rules)

    def restrict(self, rules: list[str]) -> None:
        self.restrictions.extend(Rule.parse(r) for r in rules)

    def clear_temporary(self) -> None:
        self.grants.clear()
        self.restrictions.clear()

    def child(self) -> Permissions:
        """Sub-agents inherit the parent's rules but not its temporary grants or restrictions."""
        return Permissions(self.default, list(self.allow), list(self.deny))
