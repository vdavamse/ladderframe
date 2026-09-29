"""`ladderframe check`: validate an agent root before running or deploying it."""

from __future__ import annotations

from dataclasses import dataclass, field

from ..capabilities import build_capabilities
from ..runtime.runtime import Runtime


@dataclass
class Report:
    errors: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors


def check_runtime(runtime: Runtime) -> Report:
    report = Report()
    report.errors.extend(runtime.discovered.errors)

    main = runtime.select_tools(runtime.config.tools)
    for entry in main.unknown:
        report.errors.append(f"etc/ladderframe.yaml: unknown tool {entry!r}")

    if {"Glob", "Grep", "Skill"} & set(main.functions):
        from ..tools.ripgrep import rg_binary

        if rg_binary() is None:
            report.errors.append("ripgrep (rg) is required by Glob, Grep and Skill but was not found")

    subagents = runtime.subagents
    for name in main.allowed_subagents or []:
        if name not in subagents:
            report.errors.append(f"etc/ladderframe.yaml: Agent({name}) references an unknown sub-agent")

    for spec in runtime.discovered.subagents.values():
        selection = runtime.select_tools(spec.tools or [], spec.disallowed_tools)
        for entry in selection.unknown:
            report.errors.append(f"{spec.source}: unknown tool {entry!r}")
        for skill in spec.skills:
            if skill not in runtime.skills:
                report.errors.append(f"{spec.source}: preloads unknown skill {skill!r}")
        for server in spec.mcp_servers:
            if isinstance(server, str) and server not in runtime.mcp_servers:
                report.errors.append(f"{spec.source}: unknown MCP server {server!r}")
        ignored = sorted(set(spec.extra) - {"color", "omitClaudeMd"})
        if ignored:
            report.warnings.append(f"{spec.source}: fields accepted but not yet applied: {', '.join(ignored)}")

    for skill in runtime.skills.values():
        if skill.context == "fork" and skill.agent and skill.agent not in subagents:
            report.errors.append(f"{skill.directory}/SKILL.md: agent {skill.agent!r} does not exist")
        if not skill.description:
            report.warnings.append(f"{skill.directory}/SKILL.md: no description; the model cannot tell when to use it")

    for entry in runtime.config.capabilities:
        name = entry if isinstance(entry, str) else next(iter(entry))
        try:
            build_capabilities([entry])
        except Exception as exc:  # noqa: BLE001
            report.errors.append(f"capability {name!r}: {exc}")

    config = runtime.config
    if config.runtime.executor == "temporal":
        if not config.storage.endpoint:
            report.warnings.append(
                "runtime.executor is temporal but storage.endpoint is unset: sessions and large payloads "
                "go to local files, which other pods and restarts will not see"
            )
        try:
            import temporalio  # noqa: F401
        except ImportError:
            report.errors.append("runtime.executor is temporal but `ladderframe[temporal]` is not installed")
    if config.server.auth == "api-key" and not any(config.server.api_keys):
        report.errors.append("server.auth is api-key but server.api_keys is empty")
    if config.server.auth == "jwt":
        jwt_config = config.server.jwt
        sources = [s for s in (jwt_config.jwks_url, jwt_config.public_key, jwt_config.secret) if s]
        if len(sources) != 1:
            report.errors.append("server.auth is jwt: set exactly one of server.jwt.jwks_url, public_key, secret")
        if not jwt_config.audience:
            report.warnings.append("server.jwt.audience is unset: tokens issued for other services are accepted")

    if runtime.missing_env:
        report.warnings.append(f"undefined environment variables: {', '.join(sorted(runtime.missing_env))}")

    if not runtime.root.agents_md.exists():
        report.warnings.append("no AGENTS.md in the agent root; a generic personality will be used")

    report.info += [
        f"agent: {runtime.name}  model: {runtime.config.resolve_model()}",
        f"share: {runtime.share.path if runtime.share else 'disabled'}",
        f"tools: {', '.join(main.functions)}",
        f"sub-agents: {', '.join(sorted(subagents))}",
        f"skills: {', '.join(sorted(runtime.skills)) or '-'}",
        "mcp servers: "
        + (", ".join(f"{n}{'' if s.enabled else ' (disabled)'}" for n, s in runtime.mcp_servers.items()) or "-"),
    ]
    return report
