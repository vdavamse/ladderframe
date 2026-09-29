"""Sub-agents started by the `Agent` tool inside a session workflow run as child workflows, so their
model requests and tool calls are durable activities too. Each one's conversation is saved under its
`task_id`, so a later `Agent` call can continue it."""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow

with workflow.unsafe.imports_passed_through():
    from pydantic_ai.usage import RunUsage

    from ...core.deps import HarnessDeps
    from ...storage.sessions import count_user_turns
    from ..registry import get_runtime
    from .activities import LoadedSession, LoadSessionInput, SaveSessionInput, load_session, save_session
    from .session_workflow import _ACTIVITY


@dataclass
class SubagentInput:
    root_path: str
    subagent: str
    prompt: str
    deps: HarnessDeps
    task_id: str = ""
    resume: bool = False
    description: str | None = None


@workflow.defn(name="ladderframe.Subagent")
class SubagentWorkflow:
    @workflow.run
    async def run(self, params: SubagentInput) -> str:
        runtime = get_runtime(params.root_path)
        spec = runtime.subagents[params.subagent]
        agent, _ = runtime.subagent(spec)
        loaded = LoadedSession([])
        if params.resume:
            loaded = await workflow.execute_activity(
                load_session, LoadSessionInput(params.root_path, params.task_id, tasks=True), **_ACTIVITY
            )
        result = await agent.run(
            params.prompt,
            message_history=loaded.messages,
            deps=params.deps,
            usage_limits=runtime.usage_limits(spec.max_turns),
        )
        if params.task_id:
            await workflow.execute_activity(
                save_session,
                SaveSessionInput(
                    root_path=params.root_path,
                    session_id=params.task_id,
                    messages=result.all_messages(),
                    title=params.description,
                    turns=count_user_turns(result.all_messages()),
                    status="closed",
                    usage=(loaded.meta.usage if loaded.meta else RunUsage()) + result.usage,
                    tasks=True,
                    agent=params.subagent,
                ),
                **_ACTIVITY,
            )
        return result.output


async def run_subagent_child(
    subagent: str,
    prompt: str,
    parent: HarnessDeps,
    task_id: str = "",
    resume: bool = False,
    description: str | None = None,
) -> str:
    """Called by the `Agent` tool from workflow code."""
    runtime = parent.runtime
    _, selection = runtime.subagent(runtime.subagents[subagent])
    return await workflow.execute_child_workflow(
        SubagentWorkflow.run,
        SubagentInput(
            parent.root_path, subagent, prompt, parent.child(selection.allowed_subagents), task_id, resume, description
        ),
        id=f"{workflow.info().workflow_id}:agent:{subagent}:{workflow.uuid4().hex[:12]}",
    )
