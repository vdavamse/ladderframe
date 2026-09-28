"""Runtime: turns an agent root into pydantic-ai agents and runs them.

The CLI, cron jobs, the FastAPI server and the Temporal worker all go through this class, so an
agent behaves the same however it is started.
"""

from __future__ import annotations

import platform
import uuid
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any, cast

from pydantic_ai import Agent, AgentRunResult
from pydantic_ai.agent.abstract import EventStreamHandler
from pydantic_ai.messages import ModelMessage
from pydantic_ai.settings import ModelSettings
from pydantic_ai.toolsets import AbstractToolset, FunctionToolset
from pydantic_ai.usage import UsageLimits

from ..capabilities import build_capabilities
from ..config import HarnessConfig, ModelConfig, load_config
from ..core.deps import HarnessDeps
from ..core.discovery import Discovered, discover, entry_point_tools
from ..core.frontmatter import SkillSpec, SubagentSpec
from ..core.permissions import Permissions, Rule, canonical_tool_name
from ..core.rootfs import AgentRoot, Layer, claude_layers
from ..core.substitution import RenderContext, inject_shell_output, run_shell, substitute
from ..mcp import MCPServerConfig, build_toolsets, load_mcp_config
from ..tools import BUILTIN_TOOLS
from ..tools.base import to_pydantic_tool
from ..tools.guard import PermissionGuard

HarnessAgent = Agent[HarnessDeps, str]

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
        self._agents: dict[str, HarnessAgent] = {}

    # ------------------------------------------------------------------ loading

    @classmethod
    def load(cls, root: str | Path | None = None, profile: str | None = None) -> Runtime:
        agent_root = AgentRoot.resolve(root)
        missing: set[str] = set()
        config = load_config(agent_root.etc, profile, missing)
        share = agent_root.find_share(config.share)
        layers: list[Layer] = [agent_root, *([share] if share else [])]
        if config.claude_compat:
            layers += claude_layers(agent_root)
        discovered = discover(layers)
        mcp_servers = load_mcp_config([layer.mcp_yaml for layer in layers], missing)
        return cls(agent_root, config, share, discovered, mcp_servers, missing)

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

    def _build(
        self,
        name: str,
        instructions: str,
        tools: dict[str, Callable[..., Any]],
        mcp_toolsets: list[AbstractToolset[Any]],
        model: str | None,
    ) -> HarnessAgent:
        function_toolset = FunctionToolset[HarnessDeps]([to_pydantic_tool(fn) for fn in tools.values()])
        toolsets: list[AbstractToolset[HarnessDeps]] = [PermissionGuard(function_toolset, functions=tools)]
        toolsets += [PermissionGuard(ts) for ts in mcp_toolsets]
        return Agent(
            self.config.resolve_model(model),
            name=name,
            instructions=instructions,
            deps_type=HarnessDeps,
            output_type=str,
            toolsets=toolsets,
            capabilities=build_capabilities(self.config.capabilities),
            model_settings=self._model_settings(),
            defer_model_check=True,
        )

    @property
    def agent(self) -> HarnessAgent:
        """The main agent: AGENTS.md personality, configured tools, every enabled MCP server."""
        if "__main__" not in self._agents:
            selection = self.select_tools(self.config.tools)
            if selection.unknown:
                raise RuntimeConfigError(f"unknown tools in etc/ladderframe.yaml: {', '.join(selection.unknown)}")
            instructions = f"{self.personality()}\n\n{self.environment_block()}"
            self._agents["__main__"] = self._build(
                self.name, instructions, selection.functions, build_toolsets(self.mcp_servers), None
            )
        return self._agents["__main__"]

    def subagent(self, spec: SubagentSpec, parent_tools: list[str]) -> tuple[HarnessAgent, ToolSelection]:
        selection = self.select_tools(spec.tools if spec.tools is not None else parent_tools, spec.disallowed_tools)
        key = f"sub:{spec.name}"
        if key not in self._agents:
            parts = [] if spec.extra.get("omitClaudeMd") else [self.personality()]
            parts.append(spec.prompt)
            for skill_name in spec.skills:
                if skill := self.skills.get(skill_name):
                    parts.append(f'<skill name="{skill.name}">\n{skill.body}\n</skill>')
            parts.append(self.environment_block())
            model = None if spec.model in (None, "inherit") else spec.model
            self._agents[key] = self._build(
                spec.name, "\n\n".join(p for p in parts if p), selection.functions, self._subagent_mcp(spec), model
            )
        return self._agents[key], selection

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
        permissions = self.config.permissions
        return HarnessDeps(
            runtime=self,
            root=self.root,
            config=self.config,
            permissions=Permissions.from_config(permissions.default, permissions.allow, permissions.deny),
            workdir=self.workdir,
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
        agent, selection = self.subagent(spec, self.config.tools)
        deps = HarnessDeps(
            runtime=self,
            root=self.root,
            config=self.config,
            permissions=parent.permissions.child(),
            workdir=parent.workdir,
            session_id=parent.session_id,
            depth=parent.depth + 1,
            allowed_subagents=selection.allowed_subagents,
        )
        result = await agent.run(prompt, deps=deps, usage_limits=self.usage_limits(spec.max_turns))
        return result.output

    async def render_skill(self, spec: SkillSpec, arguments: str, deps: HarnessDeps) -> str:
        ctx = RenderContext(
            skill_dir=str(spec.directory or ""),
            project_dir=str(deps.workdir),
            agent_root=str(self.root.path),
            session_id=deps.session_id,
        )
        text = substitute(spec.body, arguments, spec.arguments, ctx)

        async def runner(command: str) -> str:
            deps.permissions.check("Bash", command)
            return await run_shell(command, cwd=str(deps.workdir))

        return await inject_shell_output(text, None if self.config.disable_skill_shell_execution else runner)

    async def run_forked_skill(self, spec: SkillSpec, rendered: str, deps: HarnessDeps) -> str:
        agent_spec = self.subagents.get(spec.agent or "general-purpose", _GENERAL_PURPOSE)
        if spec.model:
            agent_spec = agent_spec.model_copy(update={"model": spec.model, "name": f"{agent_spec.name}@{spec.name}"})
        return await self.run_subagent(agent_spec, rendered, deps)
