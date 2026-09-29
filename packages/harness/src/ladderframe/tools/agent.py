"""The `Agent` tool (alias `Task`): run a sub-agent from `agents/*.md`. Prompt, parameters and output follow
opencode's task tool, including `task_id` to continue an earlier sub-agent conversation."""

from __future__ import annotations

from dataclasses import replace

from pydantic_ai import ModelRetry, RunContext
from pydantic_ai.tools import ToolDefinition

from ..core.deps import HarnessDeps
from ..storage.sessions import validate_session_id
from .base import tool

DESCRIPTION = """Launch a new agent to handle complex, multistep tasks autonomously.

When using the Agent tool, you must specify a subagent_type parameter to select which agent type to use.

When NOT to use the Agent tool:
- If you want to read a specific file path, use the Read or Glob tool instead of the Agent tool, to find the match more quickly
- If you are searching for a specific class definition like "class Foo", use the Grep tool instead, to find the match more quickly
- If you are searching for code within a specific file or set of 2-3 files, use the Read tool instead of the Agent tool, to find the match more quickly
- If no available agent is a good fit for the task, use other tools directly


Usage notes:
1. Launch multiple agents concurrently whenever possible, to maximize performance; to do that, use a single message with multiple tool uses
2. Once you have delegated work to an agent, do not duplicate that work yourself. Continue with non-overlapping tasks, or wait for the result.
3. When the agent is done, it will return a single message back to you. The result returned by the agent is not visible to the user. To show the user the result, you should send a text message back to the user with a concise summary of the result. The output includes a task_id you can reuse later to continue the same subagent session.
4. Each agent invocation starts with a fresh context unless you provide task_id to resume the same subagent session (which continues with its previous messages and tool outputs). When starting fresh, your prompt should contain a highly detailed task description for the agent to perform autonomously and you should specify exactly what information the agent should return back to you in its final and only message to you.
5. The agent's outputs should generally be trusted
6. Clearly tell the agent whether you expect it to write code or just to do research (search, file reads, web fetches, etc.), since it is not aware of the user's intent. Tell it how to verify its work if possible (e.g., relevant test commands).
7. If the agent description mentions that it should be used proactively, then you should try your best to use it without the user having to ask for it first. Use your judgement."""

NO_DESCRIPTION = "This subagent should only be called manually by the user."


def _available(deps: HarnessDeps) -> list[str]:
    names = sorted(deps.runtime.subagents)
    if deps.allowed_subagents is not None:
        names = [n for n in names if n in deps.allowed_subagents]
    return [n for n in names if deps.permissions.is_allowed("Agent", n)]


async def _describe(ctx: RunContext[HarnessDeps], tool_def: ToolDefinition) -> ToolDefinition | None:
    deps = ctx.deps
    if deps.depth >= deps.config.limits.max_subagent_depth:
        return None
    names = _available(deps)
    if not names:
        return None
    listing = "\n".join(f"- {n}: {deps.runtime.subagents[n].description or NO_DESCRIPTION}" for n in names)
    description = f"{DESCRIPTION}\nAvailable agent types and the tools they have access to:\n{listing}"
    return replace(tool_def, description=description)


def render_task(task_id: str, state: str, text: str, summary: str | None = None) -> str:
    tag = "task_error" if state == "error" else "task_result"
    lines = [f'<task id="{task_id}" state="{state}">']
    if summary:
        lines.append(f"<summary>{summary}</summary>")
    lines += [f"<{tag}>", text, f"</{tag}>", "</task>"]
    return "\n".join(lines)


# `temporal: False` keeps this tool out of activities: inside a workflow it starts the sub-agent as a
# child workflow, so the sub-agent's own model and tool calls are durable too.
@tool(subject="subagent_type", prepare=_describe, aliases=("Task",), metadata={"temporal": False})
async def Agent(
    ctx: RunContext[HarnessDeps],
    description: str,
    prompt: str,
    subagent_type: str,
    task_id: str | None = None,
    command: str | None = None,
) -> str:
    """Run a sub-agent.

    Args:
        description: A short (3-5 words) description of the task
        prompt: The task for the agent to perform
        subagent_type: The type of specialized agent to use for this task
        task_id: This should only be set if you mean to resume a previous task (you can pass a prior task_id and the task will continue the same subagent session as before instead of creating a fresh one)
        command: The command that triggered this task
    """
    deps = ctx.deps
    if deps.depth >= deps.config.limits.max_subagent_depth:
        limit = deps.config.limits.max_subagent_depth
        raise ModelRetry(
            f'Subagent depth limit reached ({limit}). Increase "limits.max_subagent_depth" to allow nested subagents.'
        )
    if subagent_type not in _available(deps):
        raise ModelRetry(f"Unknown agent type: {subagent_type} is not a valid agent type")
    spec = deps.runtime.subagents[subagent_type]
    task_id = _valid(task_id)

    if _in_workflow():
        from temporalio.exceptions import ChildWorkflowError
        from temporalio.workflow import uuid4

        from ..runtime.temporal.subagent_workflow import run_subagent_child

        resume = task_id is not None
        task_id = task_id or f"task_{uuid4().hex}"
        try:
            output = await run_subagent_child(spec.name, prompt, deps, task_id, resume, description)
        except ChildWorkflowError as exc:
            raise ModelRetry(f"Subagent failed (task_id: {task_id}): {_cause(exc)}") from exc
    else:
        import uuid

        resume = task_id is not None
        task_id = task_id or f"task_{uuid.uuid4().hex}"
        try:
            output = await deps.runtime.run_task(spec, prompt, deps, task_id, resume, description)
        except ModelRetry:
            raise
        except Exception as exc:  # noqa: BLE001 - reported to the model like opencode's task tool does
            raise ModelRetry(f"Subagent failed (task_id: {task_id}): {exc}") from exc
    return render_task(task_id, "completed", output)


def _valid(task_id: str | None) -> str | None:
    if not task_id:
        return None
    try:
        return validate_session_id(task_id)
    except ValueError:
        return None


def _cause(exc: BaseException) -> str:
    while exc.__cause__ is not None:
        exc = exc.__cause__
    return str(exc)


def _in_workflow() -> bool:
    try:
        from temporalio import workflow
    except ImportError:
        return False
    return workflow.in_workflow()
