"""Temporal worker for one agent: session + sub-agent workflows, snapshot activities, and the
activities pydantic-ai's `TemporalDurability` registers for every agent's model and toolsets."""

from __future__ import annotations

from pydantic_ai.durable_exec.temporal import AgentPlugin
from temporalio.client import Client
from temporalio.worker import Worker
from temporalio.worker.workflow_sandbox import SandboxedWorkflowRunner, SandboxRestrictions

from ..runtime import Runtime, RuntimeConfigError
from .activities import ACTIVITIES
from .client import task_queue
from .session_workflow import SessionWorkflow
from .subagent_workflow import SubagentWorkflow


def build_worker(client: Client, runtime: Runtime) -> Worker:
    if not runtime.durable:
        raise RuntimeConfigError("the Temporal worker needs a durable runtime (runtime.executor: temporal)")
    agents = runtime.all_agents()  # build every agent now; workflows must not construct agents
    return Worker(
        client,
        task_queue=task_queue(runtime),
        workflows=[SessionWorkflow, SubagentWorkflow],
        activities=ACTIVITIES,
        plugins=[AgentPlugin(agent) for agent in agents],
        # Workflow code reaches the already-loaded runtime (config, agents, skills) through the
        # ladderframe registry, so the package is shared with the sandbox instead of re-imported.
        workflow_runner=SandboxedWorkflowRunner(
            restrictions=SandboxRestrictions.default.with_passthrough_modules("ladderframe", "annotated_types")
        ),
    )
