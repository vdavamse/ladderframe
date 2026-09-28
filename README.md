# ladderframe

A file-driven agent harness on [pydantic-ai](https://ai.pydantic.dev). An agent is a folder:
personality in `AGENTS.md`, sub-agents and skills in Claude Code's format, MCP servers in
`mcp.yaml`, custom tools as plain Python files, and one YAML config. The engine (`packages/harness`)
is kept apart from the agents (`agents/`), so one engine runs many agents.

## Layout

```
ladderframe/
├── packages/harness/          # the engine (Python package `ladderframe`)
│   └── src/ladderframe/
│       ├── core/              # agent-root discovery, frontmatter, permissions, skill rendering, check
│       ├── config/            # etc/ladderframe.yaml schema, loader, defaults.yaml
│       ├── tools/             # Read Bash Glob Grep WebFetch WebSearch Agent Skill (+ @tool decorator)
│       ├── mcp/               # mcp.yaml -> pydantic-ai MCPToolset
│       ├── capabilities/      # YAML names -> pydantic-ai capabilities (prompt_injection_defender, ...)
│       ├── runtime/           # Runtime + inline executor; temporal/ (phase 2)
│       ├── server/            # FastAPI (phase 2)
│       ├── storage/           # MinIO/S3 (phase 2)
│       └── system/            # init.d runner, cron.d -> supercronic
├── agents/
│   └── coder/                 # an agent root
│       ├── AGENTS.md          # personality -> system prompt
│       ├── mcp.yaml           # MCP servers
│       ├── agents/            # sub-agents (*.md, Claude Code frontmatter)
│       ├── skills/            # <name>/SKILL.md (+ scripts/, reference files)
│       ├── tools/             # custom @tool files
│       ├── evals/             # pydantic-evals datasets
│       ├── etc/
│       │   ├── ladderframe.yaml        # model, tools, permissions, capabilities, runtime
│       │   ├── ladderframe.prod.yaml   # profile overlay (LADDERFRAME_PROFILE=prod)
│       │   ├── cron.d/                 # supercronic crontabs
│       │   └── init.d/                 # bootstrap scripts, run in name order
│       ├── var/               # runtime state (gitignored)
│       └── tmp/               # scratch (gitignored)
├── share/                     # same layout as an agent root; searched after the root
└── deploy/                    # Dockerfile, entrypoint, docker-compose (Temporal + MinIO), MinIO lifecycle
```

**Lookup order** for sub-agents, skills, tools and MCP servers (first match by name wins):
the agent root → `share/` → (with `claude_compat: true`) `<root>/.claude/` and `~/.claude/`.

## Quick start

```bash
uv sync
export ANTHROPIC_API_KEY=...
uv run ladderframe --root agents/coder check          # validate the agent root
uv run ladderframe --root agents/coder run "What does this repo do?"
uv run ladderframe --root agents/coder repl           # interactive; /fix-issue 42 runs a skill
uv run ladderframe --root agents/coder boot           # run etc/init.d (installs jq, mq, rg, gh, supercronic)
uv run ladderframe --root agents/coder cron           # supercronic with the merged etc/cron.d
```

The root is `--root`, else `$LADDERFRAME_ROOT`, else the current directory. Sessions from `run --session <id>`
and `repl` are stored in `<root>/var/lib/sessions/` by the inline executor.

## Configuration — `etc/ladderframe.yaml`

Merged over [`defaults.yaml`](packages/harness/src/ladderframe/config/defaults.yaml); the profile
file `etc/ladderframe.<profile>.yaml` is merged on top (mappings merge, lists replace).
`${VAR}` and `${VAR:-default}` expand from the environment.

```yaml
model: sonnet                    # alias | claude-opus-5-5 | provider:model | {id, effort, settings}
tools: [Read, Glob, Grep, Bash, Agent(code-reviewer, explore), Skill, WebFetch, WebSearch, Hello]
tool_settings:
  Bash: {timeout: 120}
  WebSearch: {backend: tavily}   # duckduckgo (default, extra `search`) | tavily
permissions:
  default: allow                 # deny in production
  allow: [Bash(git *)]
  deny: [Bash(rm -rf *), github_*]
capabilities:
  - prompt_injection_defender: {block_high_risk: true}
limits: {request_limit: 60, max_subagent_depth: 2}
```

`Agent(a, b)` in `tools` limits which sub-agents can be started. `Task` is accepted everywhere as an
alias of `Agent`.

### Permission rules

Claude Code syntax: `Tool`, `Tool(glob)`, `Agent(name, name)`, `WebFetch(domain:example.com)`, and
MCP tool globs such as `github_*`. A matching deny rule wins, then an allow rule (or a skill's
`allowed-tools` grant for the current run), then `default`. Denied calls are reported back to the
model; fully blocked tools are hidden from it.

## Sub-agents — `agents/*.md`

[Claude Code sub-agent format](https://code.claude.com/docs/en/sub-agents): the body is the system prompt.

| Field | Applied |
|---|---|
| `name`, `description` | required |
| `tools`, `disallowedTools` | yes (inherits the main tool list when `tools` is omitted) |
| `model` | yes (`inherit`, alias, or id) |
| `maxTurns` | yes (request limit) |
| `skills` | yes (preloaded into the prompt) |
| `mcpServers` | yes (names from `mcp.yaml`, or inline definitions); none by default |
| `omitClaudeMd` | yes (skips `AGENTS.md`) |
| `permissionMode`, `effort`, `hooks`, `memory`, `isolation`, `background`, `color`, … | accepted, not yet applied (`check` lists them) |

## Skills — `skills/<name>/SKILL.md`

[Claude Code skill format](https://code.claude.com/docs/en/skills). Descriptions are listed to the model
in the `Skill` tool; the body loads when the skill is invoked.

Supported: `name` (defaults to the directory), `description`, `when_to_use`, `arguments`,
`argument-hint`, `disable-model-invocation`, `user-invocable`, `allowed-tools`, `disallowed-tools`,
`context: fork` + `agent`, `model`; substitutions `$ARGUMENTS`, `$ARGUMENTS[N]`, `$N`, `$name`,
`${CLAUDE_SKILL_DIR}`, `${CLAUDE_PROJECT_DIR}`, `${CLAUDE_SESSION_ID}`, `${AGENT_ROOT}`; shell
injection `` !`cmd` `` and ```` ```! ```` blocks (checked against `Bash` permission rules; disable with
`disable_skill_shell_execution: true`).

## MCP servers — `mcp.yaml`

pydantic-ai's [`load_mcp_toolsets()`](https://pydantic.dev/docs/ai/mcp/client/) format as YAML
(`command`/`args`/`env`/`cwd` → stdio, `url`/`headers` → streamable HTTP, SSE for `/sse`), built into
`MCPToolset`s and prefixed with the server name (`github_create_issue`). Extra keys:

```yaml
mcpServers:
  linear:
    url: https://mcp.linear.app/mcp
    options: {auth: oauth, include_instructions: true, read_timeout: 60}   # MCPToolset(**options)
    prefix: linear          # default: the server name
    tools: [get_*, list_*]  # allowlist of the server's own tool names
    enabled: true
```

Note: YAML reads the bare keys `on`, `off`, `yes`, `no` as booleans; don't use them as server names.

## Custom tools

Any `@tool` function in `tools/*.py` (root or share) is discovered; list its name under `tools:`.

```python
from pydantic import BaseModel
from pydantic_ai import RunContext
from ladderframe import HarnessDeps, tool


class HelloSettings(BaseModel):
    greeting: str = "Hello"


@tool(settings=HelloSettings)
def Hello(ctx: RunContext[HarnessDeps], name: str) -> str:
    """Greet someone.

    Args:
        name: Who to greet.
    """
    return f"{ctx.deps.settings('Hello', HelloSettings).greeting}, {name}!"
```

Tools can also ship in a package through the `ladderframe.tools` entry-point group, enabled per agent
with `tool_packages: [my-package]`. Capabilities use the `ladderframe.capabilities` group or a
`module:Class` path.

## Runtime and deployment

- **One agent per pod.** The image contains the harness, one agent root and `share/`
  (`docker build -f deploy/Dockerfile --build-arg AGENT=coder .`).
- **Entrypoint:** `ladderframe boot`, then `ladderframe cron` in the background, then
  `ladderframe serve` (FastAPI + Temporal worker in one process).
- **Temporal (self-hosted)** holds sessions (one entity workflow per session), durable execution and
  async runs. **MinIO** stores session snapshots (`sessions/`, kept) and offloaded Temporal payloads
  (`payloads/`, expired by `deploy/minio/lifecycle.json` after 45 days; keep this longer than the
  namespace retention, 30 days in the compose file).

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Config + profiles, discovery, frontmatter, permissions, all 8 tools, skills, sub-agents, MCP, capabilities, inline runtime, CLI (`run`, `repl`, `check`, `boot`, `cron`), init.d/cron.d | done |
| 2 | FastAPI server (Vercel AI / AG-UI / web), auth, Temporal session workflow + worker, MinIO storage, OpenTelemetry | stubs + design notes in `runtime/temporal/__init__.py` |
| 3 | Bash sandboxing, pydantic-evals in CI, Helm chart | planned |

Until phase 2 lands, `ladderframe serve` and `ladderframe worker` exit with a notice, so the Docker
entrypoint is not usable yet.

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pyright
```
