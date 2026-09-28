"""Claude Code frontmatter formats for sub-agents (`agents/*.md`) and skills (`skills/<name>/SKILL.md`).

Field names follow https://code.claude.com/docs/en/sub-agents and https://code.claude.com/docs/en/skills.
Fields Claude Code supports but ladderframe does not act on yet are accepted and kept in `extra`, so a
Claude Code file always loads.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import AliasChoices, BaseModel, ConfigDict, Field, field_validator

_FRONTMATTER = re.compile(r"\A---\s*\n(.*?)\n---\s*(?:\n|\Z)(.*)\Z", re.DOTALL)


class FrontmatterError(ValueError):
    pass


def parse_frontmatter(text: str, source: Path | str = "<string>") -> tuple[dict[str, Any], str]:
    match = _FRONTMATTER.match(text)
    if not match:
        return {}, text.strip()
    try:
        meta = yaml.safe_load(match.group(1)) or {}
    except yaml.YAMLError as exc:
        raise FrontmatterError(f"{source}: invalid frontmatter YAML: {exc}") from exc
    if not isinstance(meta, dict):
        raise FrontmatterError(f"{source}: frontmatter must be a mapping")
    return meta, match.group(2).strip()


def split_tool_list(value: str | list[str] | None) -> list[str] | None:
    """Parse `Read, Grep, Agent(worker, researcher)` or `Read Grep Bash(git *)` or a YAML list.

    Commas and spaces inside parentheses belong to the rule specifier and are not separators.
    """
    if value is None:
        return None
    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()]
    items: list[str] = []
    current, depth = "", 0
    for char in value:
        if char == "(":
            depth += 1
        elif char == ")":
            depth = max(depth - 1, 0)
        if depth == 0 and (char == "," or char.isspace()):
            if current.strip():
                items.append(current.strip())
            current = ""
            continue
        current += char
    if current.strip():
        items.append(current.strip())
    return items


class _Spec(BaseModel):
    model_config = ConfigDict(extra="allow", populate_by_name=True)

    @property
    def extra(self) -> dict[str, Any]:
        return dict(self.__pydantic_extra__ or {})


class SubagentSpec(_Spec):
    """A sub-agent definition: frontmatter + markdown body as its system prompt."""

    name: str
    description: str
    tools: list[str] | None = None
    disallowed_tools: list[str] | None = Field(
        default=None, validation_alias=AliasChoices("disallowedTools", "disallowed_tools")
    )
    model: str | None = None
    permission_mode: str | None = Field(
        default=None, validation_alias=AliasChoices("permissionMode", "permission_mode")
    )
    max_turns: int | None = Field(default=None, validation_alias=AliasChoices("maxTurns", "max_turns"))
    skills: list[str] = Field(default_factory=list)
    mcp_servers: list[str | dict[str, Any]] = Field(
        default_factory=list, validation_alias=AliasChoices("mcpServers", "mcp_servers")
    )
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    prompt: str = ""
    source: Path | None = None

    @field_validator("name")
    @classmethod
    def _valid_name(cls, value: str) -> str:
        if ":" in value or value.startswith("-"):
            raise ValueError("agent name must not contain ':' or start with '-'")
        return value

    @field_validator("tools", "disallowed_tools", mode="before")
    @classmethod
    def _split(cls, value: Any) -> Any:
        return split_tool_list(value)

    @field_validator("skills", mode="before")
    @classmethod
    def _split_skills(cls, value: Any) -> Any:
        return split_tool_list(value) or []

    @classmethod
    def from_file(cls, path: Path) -> SubagentSpec:
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"), path)
        return cls.model_validate({**meta, "prompt": body, "source": path})


class SkillSpec(_Spec):
    """A skill: `SKILL.md` frontmatter + body, plus its directory for bundled files."""

    name: str
    description: str = ""
    when_to_use: str | None = None
    argument_hint: str | None = Field(default=None, validation_alias=AliasChoices("argument-hint", "argument_hint"))
    arguments: list[str] = Field(default_factory=list)
    disable_model_invocation: bool = Field(
        default=False, validation_alias=AliasChoices("disable-model-invocation", "disable_model_invocation")
    )
    user_invocable: bool = Field(default=True, validation_alias=AliasChoices("user-invocable", "user_invocable"))
    allowed_tools: list[str] = Field(
        default_factory=list, validation_alias=AliasChoices("allowed-tools", "allowed_tools")
    )
    disallowed_tools: list[str] = Field(
        default_factory=list, validation_alias=AliasChoices("disallowed-tools", "disallowed_tools")
    )
    context: Literal["fork"] | None = None
    agent: str | None = None
    model: str | None = None
    effort: Literal["low", "medium", "high", "xhigh", "max"] | None = None
    shell: Literal["bash", "powershell"] = "bash"
    paths: list[str] = Field(default_factory=list)
    body: str = ""
    directory: Path | None = None

    @field_validator("allowed_tools", "disallowed_tools", "arguments", "paths", mode="before")
    @classmethod
    def _split(cls, value: Any) -> Any:
        return split_tool_list(value) or []

    @property
    def listing(self) -> str:
        """Text shown to the model in the skill list, truncated like Claude Code (1,536 chars)."""
        text = self.description + (f" {self.when_to_use}" if self.when_to_use else "")
        return text[:1536]

    @classmethod
    def from_dir(cls, directory: Path) -> SkillSpec:
        path = directory / "SKILL.md"
        meta, body = parse_frontmatter(path.read_text(encoding="utf-8"), path)
        meta.setdefault("name", directory.name)
        return cls.model_validate({**meta, "body": body, "directory": directory})
