from pathlib import Path

from ladderframe.core.frontmatter import SkillSpec, SubagentSpec, parse_frontmatter, split_tool_list

ROOT = Path(__file__).parent / "fixtures" / "rootfs"


def test_split_tool_list_keeps_specifiers_together() -> None:
    assert split_tool_list("Read, Grep, Agent(worker, researcher)") == ["Read", "Grep", "Agent(worker, researcher)"]
    assert split_tool_list("Read Grep Bash(git add *)") == ["Read", "Grep", "Bash(git add *)"]
    assert split_tool_list(["Read", "Bash"]) == ["Read", "Bash"]
    assert split_tool_list(None) is None


def test_subagent_claude_code_fields() -> None:
    spec = SubagentSpec.from_file(ROOT / "agents" / "helper.md")
    assert spec.name == "helper"
    assert spec.tools == ["Read", "Echo"]
    assert spec.max_turns == 5
    assert spec.prompt == "You are the helper."
    assert spec.extra == {"color": "blue"}  # accepted, not acted on


def test_skill_defaults_to_directory_name() -> None:
    skill = SkillSpec.from_dir(ROOT / "skills" / "greet")
    assert skill.name == "greet"
    assert skill.arguments == ["who"]
    assert skill.allowed_tools == ["Bash(echo *)"]
    assert skill.user_invocable is True


def test_skill_boolean_spellings() -> None:
    skill = SkillSpec.model_validate({"name": "x", "disable-model-invocation": "yes", "user-invocable": "off"})
    assert skill.disable_model_invocation is True
    assert skill.user_invocable is False


def test_no_frontmatter() -> None:
    assert parse_frontmatter("just text") == ({}, "just text")
