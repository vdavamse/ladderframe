from ladderframe.core.substitution import RenderContext, inject_shell_output, substitute

CTX = RenderContext(skill_dir="/skills/x", project_dir="/proj", agent_root="/root", session_id="s1")


def test_arguments() -> None:
    body = "all=$ARGUMENTS first=$0 second=$ARGUMENTS[1] named=$issue dir=${CLAUDE_SKILL_DIR} cost=\\$1"
    out = substitute(body, '42 "two words"', ["issue"], CTX)
    assert out == 'all=42 "two words" first=42 second=two words named=42 dir=/skills/x cost=$1'


def test_arguments_appended_when_unreferenced() -> None:
    assert substitute("Do it.", "abc", [], CTX) == "Do it.\n\nARGUMENTS: abc"
    assert substitute("Do it.", "", [], CTX) == "Do it."


async def test_shell_injection() -> None:
    async def fake(command: str) -> str:
        return f"<{command}>\n"

    text = "Status: !`git status`\n```!\nls\n```\nend"
    assert await inject_shell_output(text, fake) == "Status: <git status>\n<ls>\nend"
    assert "disabled by policy" in await inject_shell_output("x !`ls`", None)
