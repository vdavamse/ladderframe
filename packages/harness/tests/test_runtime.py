from conftest import streaming
from pydantic_ai.messages import ModelMessage, ModelResponse, RetryPromptPart, TextPart, ToolCallPart, ToolReturnPart
from pydantic_ai.models.function import AgentInfo, FunctionModel

from ladderframe import Runtime
from ladderframe.core.check import check_runtime
from ladderframe.runtime.inline import InlineExecutor
from ladderframe.storage import MemoryObjectStore


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

    return streaming(respond)


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
    assert "- helper: Helps with things." in tools["Agent"].description
    assert "Available agent types and the tools they have access to:" in tools["Agent"].description
    assert set(tools["Read"].parameters_json_schema["properties"]) == {"filePath", "offset", "limit"}
    instructions = seen["info"].instructions or ""
    assert "<available_skills>\n  <skill>\n    <name>greet</name>\n    <description>Greet someone.</description>" in (
        instructions
    )


async def test_permission_denied_is_reported_to_model(runtime: Runtime) -> None:
    with runtime.agent.override(model=scripted(("Bash", {"command": "rm -rf /tmp/x"}))):
        result = await runtime.run("delete")
    assert "Permission denied: Bash" in result.output


async def test_skill_loads_content(runtime: Runtime) -> None:
    with runtime.agent.override(model=scripted(("Skill", {"name": "greet"}))):
        result = await runtime.run("greet Ada")
    skill_dir = runtime.skills["greet"].directory
    assert result.output == (
        '<skill_content name="greet">\n# Skill: greet\n\n'
        f"Say hello to  from {skill_dir}.\n\n"
        f"Base directory for this skill: {skill_dir}\n"
        "Relative paths in this skill (e.g., scripts/, reference/) are relative to this base directory.\n"
        "Note: file list is sampled.\n\n<skill_files>\n\n</skill_files>\n</skill_content>"
    )


async def test_agent_tool_runs_subagent(runtime: Runtime) -> None:
    helper, _ = runtime.subagent(runtime.subagents["helper"])
    main = scripted(("Agent", {"description": "echo", "prompt": "say x", "subagent_type": "helper"}))
    with runtime.agent.override(model=main), helper.override(model=scripted(("Echo", {"text": "from helper"}))):
        result = await runtime.run("delegate")
    assert result.output.startswith('<task id="task_')
    assert result.output.endswith('" state="completed">\n<task_result>\necho: from helper\n</task_result>\n</task>')


async def test_agent_tool_resumes_task(runtime: Runtime) -> None:
    runtime.object_store = MemoryObjectStore()
    helper, _ = runtime.subagent(runtime.subagents["helper"])
    seen: list[int] = []

    def respond(messages: list[ModelMessage], info: AgentInfo) -> ModelResponse:
        seen.append(len(messages))
        return ModelResponse(parts=[TextPart("ok")])

    call = {"description": "d", "prompt": "p", "subagent_type": "helper"}
    with runtime.agent.override(model=scripted(("Agent", call))), helper.override(model=FunctionModel(respond)):
        first = await runtime.run("go")
    task_id = first.output.split('"')[1]
    resume = {**call, "task_id": task_id}
    with runtime.agent.override(model=scripted(("Agent", resume))), helper.override(model=FunctionModel(respond)):
        second = await runtime.run("again")
    assert f'<task id="{task_id}"' in second.output
    assert seen == [1, 3]  # the resumed run saw the first prompt and answer


async def test_agent_tool_rejects_disallowed_type(runtime: Runtime) -> None:
    calls = [("Agent", {"description": "x", "prompt": "x", "subagent_type": "general-purpose"})]
    with runtime.agent.override(model=scripted(*calls)):
        result = await runtime.run("delegate")
    assert "Unknown agent type: general-purpose is not a valid agent type" in result.output


async def test_inline_executor_keeps_history(runtime: Runtime) -> None:
    runtime.object_store = MemoryObjectStore()
    executor = InlineExecutor(runtime)
    with runtime.agent.override(model=scripted()):
        first = await executor.send("s1", "first")
        second = await executor.send("s1", "second")
    assert first.status == second.status == "done"
    assert len(await executor.history("s1")) == 4
    [meta] = await executor.sessions()
    assert meta.turns == 2
