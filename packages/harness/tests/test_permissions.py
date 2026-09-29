from ladderframe.core.permissions import Permissions, Rule


def test_rule_parsing_and_task_alias() -> None:
    assert Rule.parse("Bash(git *)") == Rule("Bash", "git *")
    assert Rule.parse("Task(reviewer)") == Rule("Agent", "reviewer")
    assert Rule.parse("github_*") == Rule("github_*", None)


def test_deny_wins_over_allow() -> None:
    perms = Permissions.from_config("deny", ["Bash(git *)"], ["Bash(git push *)"])
    assert perms.is_allowed("Bash", "git status")
    assert not perms.is_allowed("Bash", "git push origin main")
    assert not perms.is_allowed("Bash", "ls")


def test_agent_rule_lists_names() -> None:
    perms = Permissions.from_config("deny", ["Agent(worker, researcher)"], [])
    assert perms.is_allowed("Task", "researcher")
    assert not perms.is_allowed("Agent", "other")


def test_tool_blocked() -> None:
    perms = Permissions.from_config("deny", ["Read"], [])
    assert perms.tool_blocked("Bash")
    assert not perms.tool_blocked("Read")
    assert Permissions.from_config("allow", [], ["github_*"]).tool_blocked("github_create_issue")


def test_skill_grants_are_temporary_and_not_inherited() -> None:
    perms = Permissions.from_config("deny", [], [])
    perms.grant(["Bash(echo *)"])
    assert perms.is_allowed("Bash", "echo hi")
    assert not perms.child().is_allowed("Bash", "echo hi")
    perms.clear_temporary()
    assert not perms.is_allowed("Bash", "echo hi")
