"""Sub-agents started by the `Agent` tool inside a session workflow run as child workflows, so their
model requests and tool calls are durable activities too."""

from __future__ import annotations

from dataclasses import dataclass

from temporalio import workflow

from ...core.deps import HarnessDeps
from ..registry import get_runtime


@dataclass
class SubagentInput:
    root_path: str
    subagent: str
    prompt: str
    deps: HarnessDeps


@workflow.defn(name="ladderframe.Subagent")
class SubagentWorkflow:
    @workflow.run
    async def run(self, params: SubagentInput) -> str:
        runtime = get_runtime(params.root_path)
        spec = runtime.subagents[params.subagent]
        agent, _ = runtime.subagent(spec)
        result = await agent.run(params.prompt, deps=params.deps, usage_limits=runtime.usage_limits(spec.max_turns))
        return result.output


async def run_subagent_child(subagent: str, prompt: str, parent: HarnessDeps) -> str:
    """Called by the `Agent` tool from workflow code."""
    runtime = parent.runtime
    _, selection = runtime.subagent(runtime.subagents[subagent])
    return await workflow.execute_child_workflow(
        SubagentWorkflow.run,
        SubagentInput(parent.root_path, subagent, prompt, parent.child(selection.allowed_subagents)),
        id=f"{workflow.info().workflow_id}:agent:{subagent}:{workflow.uuid4().hex[:12]}",
    )
