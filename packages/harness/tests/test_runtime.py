from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ladderframe import Runtime
from ladderframe.core.check import check_runtime
from ladderframe.runtime.inline import InlineExecutor


def scripted(*calls: tuple[str, dict]) -> FunctionModel:
    """A model that makes the given tool calls in order, then answers with the last tool result (or retry prompt)."""
    steps = list(calls)

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        if steps:
            name, args = steps.pop(0)
            return ModelResponse(parts=[ToolCallPart(name, args)])
        results = (ToolReturnPart, RetryPromptPart)
        last = [p for m in messages for p in getattr(m, "parts", []) if isinstance(p, results)]
        return ModelResponse(parts=[TextPart(str(last[-1].content) if last else "done")])

    return FunctionModel(respond)


def test_discovery(runtime: Runtime) -> None:
    assert set(runtime.subagents) == {"general-purpose", "helper"}
    assert set(runtime.skills) == {"greet"}
    assert "Echo" in runtime.available_tools
    selection = runtime.select_tools(runtime.config.tools)
    assert selection.allowed_subagents == ["helper"]
    assert not selection.unknown


def test_check_is_clean(runtime: Runtime) -> None:
    report = check_runtime(runtime)
    assert report.ok, report.errors


async def test_custom_tool_and_instructions(runtime: Runtime) -> None:
    seen: dict[str, AgentInfo] = {}

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen["info"] = info
        return ModelResponse(parts=[TextPart("hi")])

    with runtime.agent.override(model=FunctionModel(respond)):
        result = await runtime.run("hello")
    assert result.output == "hi"
    tools = {t.name: t for t in seen["info"].function_tools}
    assert {"Read", "Echo", "Agent", "Skill"} <= set(tools)
    assert "helper: Helps with things." in tools["Agent"].description
    assert "greet: Greet someone." in tools["Skill"].description


async def test_permission_denied_is_reported_to_model(runtime: Runtime) -> None:
    with runtime.agent.override(model=scripted(("Bash", {"command": "rm -rf /tmp/x"}))):
        result = await runtime.run("delete")
    assert "Permission denied: Bash" in result.output


async def test_skill_renders_inline(runtime: Runtime) -> None:
    with runtime.agent.override(model=scripted(("Skill", {"skill": "greet", "args": "Ada"}))):
        result = await runtime.run("greet Ada")
    assert "Say hello to Ada from" in result.output
    assert "skills/greet." in result.output


async def test_agent_tool_runs_subagent(runtime: Runtime) -> None:
    helper, _ = runtime.subagent(runtime.subagents["helper"], runtime.config.tools)
    main = scripted(("Agent", {"description": "echo", "prompt": "say x", "subagent_type": "helper"}))
    with runtime.agent.override(model=main), helper.override(model=scripted(("Echo", {"text": "from helper"}))):
        result = await runtime.run("delegate")
    assert result.output == "echo: from helper"


async def test_agent_tool_rejects_disallowed_type(runtime: Runtime) -> None:
    calls = [("Agent", {"description": "x", "prompt": "x", "subagent_type": "general-purpose"})]
    with runtime.agent.override(model=scripted(*calls)):
        result = await runtime.run("delegate")
    assert "disallowed agent type" in result.output


async def test_inline_sessions_persist(runtime: Runtime, tmp_path) -> None:
    executor = InlineExecutor(runtime)
    executor.sessions.directory = tmp_path
    with runtime.agent.override(model=scripted()):
        session_id, _ = await executor.send("first")
        await executor.send("second", session_id)
    assert len(executor.sessions.load(session_id)) == 4
