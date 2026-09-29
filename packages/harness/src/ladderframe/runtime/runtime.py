"""Runtime: turns an agent root into pydantic-ai agents and runs them.

The CLI, cron jobs, the FastAPI server and the Temporal worker all go through this class, so an
agent behaves the same however it is started.
"""

from __future__ import annotations

import html
import platform
import re
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, date, datetime, timedelta
from pathlib import Path
from typing import Any, cast

from pydantic_ai import Agent, AgentRunResult
from pydantic_ai.agent.abstract import EventStreamHandler
from pydantic_ai.capabilities import AbstractCapability
from pydantic_ai.messages import ModelMessage
from pydantic_ai.models import Model
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
from pydantic_ai.usage import UsageLimits

from ..capabilities import build_capabilities
from ..config import HarnessConfig, ModelConfig, load_config
from ..core.deps import HarnessDeps
from ..core.discovery import Discovered, discover, entry_point_tools
from ..core.frontmatter import SkillSpec, SubagentSpec
from ..core.permissions import Rule, canonical_tool_name
from ..core.rootfs import AgentRoot, Layer, claude_layers
from ..core.substitution import RenderContext, inject_shell_output, run_shell, substitute
from ..mcp import MCPServerConfig, build_toolsets, load_mcp_config
from ..storage.object_store import ObjectStore, open_object_store
from ..storage.sessions import SessionArchive, SessionMeta, count_user_turns
from ..tools import BUILTIN_TOOLS
from ..tools.base import to_pydantic_tool
from ..tools.guard import PermissionGuard
from .registry import register_runtime

HarnessAgent = Agent[HarnessDeps, str]

EVENT_TOPIC = "agent-events"
"""Workflow Stream topic the main agent publishes its events to when running under Temporal."""

_GENERAL_PURPOSE = SubagentSpec(
    name="general-purpose",
    description="General-purpose agent for multi-step research and tasks.",
    prompt="You are a capable sub-agent. Complete the task you are given and reply with a concise final report.",
)


class RuntimeConfigError(RuntimeError):
    pass


@dataclass
class ToolSelection:
    functions: dict[str, Callable[..., Any]]
    allowed_subagents: list[str] | None = None
    unknown: list[str] = field(default_factory=list)


class Runtime:
    def __init__(
        self,
        root: AgentRoot,
        config: HarnessConfig,
        share: Layer | None,
        discovered: Discovered,
        mcp_servers: dict[str, MCPServerConfig],
        missing_env: set[str],
        *,
        durable: bool | None = None,
        model: Model | str | None = None,
    ) -> None:
        self.root = root
        self.config = config
        self.share = share
        self.discovered = discovered
        self.mcp_servers = mcp_servers
        self.missing_env = missing_env
        self.name = config.name or root.name
        self.workdir = (Path.cwd() / Path(config.workdir).expanduser()).resolve()
        self.available_tools: dict[str, Callable[..., Any]] = self._tool_pool()
        self.durable = config.runtime.executor == "temporal" if durable is None else durable
        """Attach pydantic-ai `TemporalDurability` to every agent (required by the Temporal worker)."""
        self.model_override = model
        """Use this model for every agent instead of the configured ones (tests, local experiments)."""
        self._agents: dict[str, HarnessAgent] = {}
        self._object_store: ObjectStore | None = None
        register_runtime(self)

    # ------------------------------------------------------------------ loading

    @classmethod
    def load(
        cls,
        root: str | Path | None = None,
        profile: str | None = None,
        *,
        durable: bool | None = None,
        model: Model | str | None = None,
    ) -> Runtime:
        agent_root = AgentRoot.resolve(root)
        missing: set[str] = set()
        config = load_config(agent_root.etc, profile, missing)
        share = agent_root.find_share(config.share)
        layers: list[Layer] = [agent_root, *([share] if share else [])]
        if config.claude_compat:
            layers += claude_layers(agent_root)
        discovered = discover(layers)
        mcp_servers = load_mcp_config([layer.mcp_yaml for layer in layers], missing)
        return cls(agent_root, config, share, discovered, mcp_servers, missing, durable=durable, model=model)

    @property
    def object_store(self) -> ObjectStore:
        """MinIO/S3 when `storage.endpoint` is set, else `<root>/var/lib/objects`. Assignable (tests)."""
        if self._object_store is None:
            self._object_store = open_object_store(self.config.storage, self.root.var_lib / "objects")
        return self._object_store

    @object_store.setter
    def object_store(self, store: ObjectStore) -> None:
        self._object_store = store

    def session_archive(self) -> SessionArchive:
        return SessionArchive(self.object_store, self.agent_name(), self.config.storage.session_prefix)

    def task_archive(self) -> SessionArchive:
        """Sub-agent conversations started by the Agent tool, resumable by `task_id`."""
        return SessionArchive(self.object_store, f"{self.agent_name()}.tasks", self.config.storage.session_prefix)

    @property
    def subagents(self) -> dict[str, SubagentSpec]:
        return {"general-purpose": _GENERAL_PURPOSE, **self.discovered.subagents}

    @property
    def skills(self) -> dict[str, SkillSpec]:
        return self.discovered.skills

    def _tool_pool(self) -> dict[str, Callable[..., Any]]:
        """Custom tools from the root/share override entry-point tools, which override built-ins."""
        packaged, errors = entry_point_tools(self.config.tool_packages)
        self.discovered.errors.extend(errors)
        return {**BUILTIN_TOOLS, **packaged, **self.discovered.tools}

    def select_tools(self, entries: list[str], disallowed: list[str] | None = None) -> ToolSelection:
        """Resolve a tools list like `[Read, Bash, Agent(reviewer, tester)]` against the tool pool."""
        selection = ToolSelection(functions={})
        for entry in entries:
            rule = Rule.parse(entry)
            name = canonical_tool_name(rule.tool)
            if name == "Agent" and rule.specifier:
                selection.allowed_subagents = [s.strip() for s in rule.specifier.split(",") if s.strip()]
            if name not in self.available_tools:
                selection.unknown.append(entry)
                continue
            selection.functions[name] = self.available_tools[name]
        for entry in disallowed or []:
            selection.functions.pop(canonical_tool_name(Rule.parse(entry).tool), None)
        return selection

    # ------------------------------------------------------------------ agents

    def environment_block(self) -> str:
        return (
            "<environment>\n"
            f"agent: {self.name}\n"
            f"agent root: {self.root.path}\n"
            f"working directory: {self.workdir}\n"
            f"platform: {platform.system().lower()}\n"
            f"date: {date.today().isoformat()}\n"
            "</environment>"
        )

    def skills_prompt(self) -> str:
        """The `<available_skills>` system prompt section (opencode's format), for agents with the Skill tool."""
        skills = sorted(
            (s for s in self.skills.values() if s.description and not s.disable_model_invocation),
            key=lambda s: s.name,
        )
        if not skills:
            return ""
        entries = [
            "  <skill>\n"
            f"    <name>{s.name}</name>\n"
            f"    <description>{html.escape(s.listing, quote=False)}</description>\n"
            f"    <location>{html.escape(str((s.directory or Path('.')) / 'SKILL.md'))}</location>\n"
            "  </skill>"
            for s in skills
        ]
        return "\n".join(
            [
                "Skills provide specialized instructions and workflows for specific tasks.",
                "Use the Skill tool to load a skill when a task matches its description.",
                "<available_skills>",
                *entries,
                "</available_skills>",
            ]
        )

    def personality(self) -> str:
        for layer in (self.root, self.share):
            if layer and layer.agents_md.exists():
                return layer.agents_md.read_text(encoding="utf-8").strip()
        return "You are a helpful agent."

    def _model_settings(self) -> ModelSettings | None:
        if not isinstance(self.config.model, ModelConfig) or not self.config.model.settings:
            return None
        return cast(ModelSettings, dict(self.config.model.settings))

    def usage_limits(self, request_limit: int | None = None) -> UsageLimits:
        limits = self.config.limits
        return UsageLimits(
            request_limit=request_limit or limits.request_limit,
            tool_calls_limit=limits.tool_calls_limit,
            total_tokens_limit=limits.total_tokens_limit,
        )

    def agent_name(self, subagent: str | None = None) -> str:
        """Stable, unique agent names; Temporal derives activity names from them."""
        base = re.sub(r"[^A-Za-z0-9_.-]", "-", self.name)
        return base if subagent is None else f"{base}--{re.sub(r'[^A-Za-z0-9_.-]', '-', subagent)}"

    def _capabilities(self, main: bool) -> list[AbstractCapability[HarnessDeps]]:
        capabilities: list[AbstractCapability[HarnessDeps]] = build_capabilities(self.config.capabilities)
        if self.durable:
            from pydantic_ai.durable_exec.temporal import TemporalDurability
            from temporalio.common import RetryPolicy

            from ..tools.bash import BashSettings, max_timeout_ms
            from .temporal.run_context import HarnessRunContext

            bash = BashSettings.model_validate(self.config.tool_settings.get("Bash", {}))
            capabilities.append(
                TemporalDurability(
                    # Only the main agent streams events: it runs in the session workflow, which hosts the stream.
                    event_stream_topic=EVENT_TOPIC if main else None,
                    run_context_type=HarnessRunContext,
                    # Tool calls may not be idempotent (`git push`), so they are never retried, and they
                    # get as long as the longest Bash command plus a margin.
                    activity_config={
                        "start_to_close_timeout": timedelta(milliseconds=max_timeout_ms(bash)) + timedelta(minutes=1),
                        "retry_policy": RetryPolicy(maximum_attempts=1),
                    },
                    model_activity_config={
                        "start_to_close_timeout": timedelta(minutes=5),
                        "retry_policy": RetryPolicy(maximum_attempts=3),
                    },
                )
            )
        return capabilities

    def _build(
        self,
        name: str,
        instructions: str,
        tools: dict[str, Callable[..., Any]],
        mcp_toolsets: list[AbstractToolset[Any]],
        model: str | None,
        main: bool,
    ) -> HarnessAgent:
        function_toolset = FunctionToolset[HarnessDeps]([to_pydantic_tool(fn) for fn in tools.values()], id="tools")
        delegate = "Agent" in tools
        toolsets: list[AbstractToolset[HarnessDeps]] = [
            PermissionGuard(function_toolset, functions=tools, delegate=delegate)
        ]
        toolsets += [PermissionGuard(ts, delegate=delegate) for ts in mcp_toolsets]
        return Agent(
            self.model_override or self.config.resolve_model(model),
            name=name,
            instructions=instructions,
            deps_type=HarnessDeps,
            output_type=str,
            toolsets=toolsets,
            capabilities=self._capabilities(main),
            model_settings=self._model_settings(),
            retries=self.config.limits.tool_retries,
            defer_model_check=True,
        )

    @property
    def agent(self) -> HarnessAgent:
        """The main agent: AGENTS.md personality, configured tools, every enabled MCP server."""
        if "__main__" not in self._agents:
            selection = self.select_tools(self.config.tools)
            if selection.unknown:
                raise RuntimeConfigError(f"unknown tools in etc/ladderframe.yaml: {', '.join(selection.unknown)}")
            skills = self.skills_prompt() if "Skill" in selection.functions else ""
            instructions = "\n\n".join(p for p in (self.personality(), skills, self.environment_block()) if p)
            self._agents["__main__"] = self._build(
                self.agent_name(), instructions, selection.functions, build_toolsets(self.mcp_servers), None, True
            )
        return self._agents["__main__"]

    def subagent(self, spec: SubagentSpec) -> tuple[HarnessAgent, ToolSelection]:
        """The sub-agent's pydantic-ai agent; without `tools` it inherits the main agent's tool list."""
        entries = spec.tools if spec.tools is not None else self.config.tools
        selection = self.select_tools(entries, spec.disallowed_tools)
        key = f"sub:{spec.name}"
        if key not in self._agents:
            parts = [] if spec.extra.get("omitClaudeMd") else [self.personality()]
            parts.append(spec.prompt)
            for skill_name in spec.skills:
                if skill := self.skills.get(skill_name):
                    parts.append(f'<skill name="{skill.name}">\n{skill.body}\n</skill>')
            if "Skill" in selection.functions:
                parts.append(self.skills_prompt())
            parts.append(self.environment_block())
            model = None if spec.model in (None, "inherit") else spec.model
            self._agents[key] = self._build(
                self.agent_name(spec.name),
                "\n\n".join(p for p in parts if p),
                selection.functions,
                self._subagent_mcp(spec),
                model,
                False,
            )
        return self._agents[key], selection

    def all_agents(self) -> list[HarnessAgent]:
        """The main agent and every sub-agent, built eagerly (the Temporal worker registers their activities)."""
        return [self.agent, *(self.subagent(spec)[0] for spec in self.subagents.values())]

    def _subagent_mcp(self, spec: SubagentSpec) -> list[AbstractToolset[Any]]:
        """Sub-agents get only the MCP servers they name (or define inline) in `mcpServers`."""
        servers: dict[str, MCPServerConfig] = {}
        for entry in spec.mcp_servers:
            if isinstance(entry, str):
                if entry in self.mcp_servers:
                    servers[entry] = self.mcp_servers[entry]
            else:
                for name, raw in entry.items():
                    servers[name] = MCPServerConfig.model_validate(raw)
        return build_toolsets(servers)

    # ------------------------------------------------------------------ running

    def new_deps(self, session_id: str | None = None) -> HarnessDeps:
        return HarnessDeps(
            root_path=str(self.root.path),
            workdir=str(self.workdir),
            session_id=session_id or uuid.uuid4().hex,
            allowed_subagents=self.select_tools(self.config.tools).allowed_subagents,
        )

    async def run(
        self,
        prompt: str,
        *,
        message_history: list[ModelMessage] | None = None,
        session_id: str | None = None,
        event_stream_handler: EventStreamHandler[HarnessDeps] | None = None,
    ) -> AgentRunResult[str]:
        return await self.agent.run(
            prompt,
            deps=self.new_deps(session_id),
            message_history=message_history,
            usage_limits=self.usage_limits(),
            event_stream_handler=event_stream_handler,
        )

    async def run_subagent(self, spec: SubagentSpec, prompt: str, parent: HarnessDeps) -> str:
        """Run a sub-agent in-process. Inside a Temporal workflow the Agent tool uses a child workflow instead."""
        agent, selection = self.subagent(spec)
        deps = parent.child(selection.allowed_subagents)
        result = await agent.run(prompt, deps=deps, usage_limits=self.usage_limits(spec.max_turns))
        return result.output

    async def run_task(
        self,
        spec: SubagentSpec,
        prompt: str,
        parent: HarnessDeps,
        task_id: str,
        resume: bool = False,
        description: str | None = None,
    ) -> str:
        """Run a sub-agent in-process for the Agent tool, continuing `task_id`'s conversation when `resume`."""
        agent, selection = self.subagent(spec)
        archive = self.task_archive()
        meta, history = await archive.load(task_id) if resume else (None, [])
        result = await agent.run(
            prompt,
            message_history=history,
            deps=parent.child(selection.allowed_subagents),
            usage_limits=self.usage_limits(spec.max_turns),
        )
        messages = result.all_messages()
        meta = meta or SessionMeta(session_id=task_id, agent=spec.name, title=description)
        meta.turns, meta.messages, meta.status = count_user_turns(messages), len(messages), "closed"
        meta.usage = meta.usage + result.usage
        meta.updated_at = datetime.now(UTC)
        await archive.save(meta, messages)
        return result.output

    async def render_skill(self, spec: SkillSpec, arguments: str, deps: HarnessDeps) -> str:
        ctx = RenderContext(
            skill_dir=str(spec.directory or ""),
            project_dir=deps.workdir,
            agent_root=str(self.root.path),
            session_id=deps.session_id,
        )
        text = substitute(spec.body, arguments, spec.arguments, ctx)

        async def runner(command: str) -> str:
            deps.permissions.check("Bash", command)
            return await run_shell(command, cwd=deps.workdir)

        return await inject_shell_output(text, None if self.config.disable_skill_shell_execution else runner)

    async def run_forked_skill(self, spec: SkillSpec, rendered: str, deps: HarnessDeps) -> str:
        agent_spec = self.subagents.get(spec.agent or "general-purpose", _GENERAL_PURPOSE)
        if spec.model:
            agent_spec = agent_spec.model_copy(update={"model": spec.model, "name": f"{agent_spec.name}@{spec.name}"})
        return await self.run_subagent(agent_spec, rendered, deps)
