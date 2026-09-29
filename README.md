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
│       ├── runtime/           # Runtime, executors (inline, temporal/: session + sub-agent workflows)
│       ├── server/            # FastAPI: sessions API, Vercel AI, AG-UI, dev web UI, API-key auth
│       ├── storage/           # object store (MinIO/S3, files, memory), session archive
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
uv run ladderframe --root agents/coder serve          # HTTP API on :8080 (+ Temporal worker if configured)
uv run ladderframe --root agents/coder worker         # Temporal worker only
uv run ladderframe --root agents/coder eval           # run the evals/ datasets
```

The root is `--root`, else `$LADDERFRAME_ROOT`, else the current directory. `run`, `repl` and `serve` use the
executor from `runtime.executor`. With `inline`, sessions are stored under `<root>/var/lib/objects/sessions/`
(or in S3/MinIO when `storage.endpoint` is set).

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

## HTTP API — `ladderframe serve`

| Method & path | |
|---|---|
| `GET /health`, `GET /ready` | liveness; readiness checks object storage and Temporal |
| `POST /v1/sessions` | new session id (the session starts with its first message) |
| `GET /v1/sessions` | sessions, filtered by `X-User-Id` |
| `POST /v1/sessions/{id}/messages` | `{"prompt": "...", "mode": "wait" \| "async" \| "stream"}`: wait for the answer, get `202` + turn id, or server-sent events |
| `GET /v1/sessions/{id}/messages` | history as pydantic-ai messages |
| `GET /v1/sessions/{id}/turns/{turn}` and `.../events` | turn status; turn events as SSE (ends with `event: result`) |
| `DELETE /v1/sessions/{id}` | close the session |
| `POST /chat` | Vercel AI SDK data stream (`useChat({api: "/chat"})`); the chat `id` is the session id |
| `POST /ag-ui` | AG-UI; the thread id is the session id |
| `GET /` | pydantic-ai chat UI — development only: it runs the agent directly, not through the executor |

`server.protocols` selects `vercel-ai`, `ag-ui` and `web`. The UI protocols send the whole conversation, but
only the latest user message is submitted: the session keeps its own history.

**Authentication** (`server.auth`, `/health`, `/ready` and `/metrics` stay open):

| Mode | Credentials | End user |
|---|---|---|
| `none` | — (development) | `X-User-Id` |
| `api-key` | `Authorization: Bearer <key>` or `X-API-Key`, checked against `server.api_keys` | `X-User-Id`, set by the calling service |
| `jwt` | `Authorization: Bearer <jwt>` (extra `auth`), verified with `server.jwt`: one of `jwks_url` (OIDC), `public_key` or `secret`, plus `issuer`, `audience`, `algorithms`; `exp` is required | the `user_claim` claim (default `sub`) |

## Observability

```yaml
observability:
  tracing: otel              # none | otel (OTEL_EXPORTER_OTLP_* env) | logfire (LOGFIRE_TOKEN)
  include_content: false     # keep prompts, responses and tool arguments out of spans
  metrics: true              # Prometheus /metrics on the server (metrics_port for `ladderframe worker`)
  temporal_metrics_port: 9464
```

- **Traces:** pydantic-ai agent runs, model requests and tool calls; Temporal workflows and activities
  (Temporal's `TracingInterceptor`, or pydantic-ai's `LogfirePlugin` with Logfire); FastAPI requests with Logfire.
- **Metrics:** `ladderframe_turns_total{agent,status}`, `ladderframe_turn_duration_seconds`,
  `ladderframe_tokens_total{kind=input|output}`, `ladderframe_model_requests_total`, `ladderframe_tool_calls_total`,
  `ladderframe_http_requests_total{method,route,status}`, `ladderframe_http_request_duration_seconds`. Under
  Temporal, the session workflow reports turns through a `record_turn` activity. `temporal_metrics_port`
  additionally exposes the Temporal SDK's metrics.

## Evals — `evals/*.yaml`

Each file in an agent root's `evals/` is a [pydantic-evals](https://ai.pydantic.dev/evals/) dataset: inputs
are prompts, outputs are the agent's final answers.

```bash
uv run ladderframe --root agents/coder eval                      # every dataset; fails below 100% by default
uv run ladderframe --root agents/coder eval basics --min-pass-rate 0.8
```

Every case is a fresh in-process turn (no session history, no Temporal). A case passes when all its
assertion evaluators (`Contains`, `LLMJudge`, `MaxDuration`, ...) pass.

## CI — `.github/workflows/ci.yml`

`lint` (ruff, pyright) · `test` (pytest, including a local Temporal dev server and an S3 API) ·
`check-agents` (`ladderframe check` for every agent and profile) · `evals` (only when the
`ANTHROPIC_API_KEY` secret is set; `EVAL_MIN_PASS_RATE`, default 0.8) · `helm` (lint + template) ·
`docker` (build `deploy/Dockerfile` for `coder`).

## Runtime and deployment

- **One agent per pod.** `ladderframe serve` runs the HTTP API and the Temporal worker for task queue
  `ladderframe-<agent>` in one process; supercronic runs alongside (`deploy/entrypoint.sh`). Cron jobs call
  `ladderframe run`, which goes through the same executor, so scheduled runs are durable too.
- **Sessions are Temporal entity workflows** (`session:<agent>:<id>`): update `submit` queues a turn, update
  `wait` returns its result, query `turn` reports status, signal `close` ends it. Each turn runs the main agent
  with pydantic-ai's `TemporalDurability`, so model requests and tool calls are activities.
- **Events** go through the session's Temporal Workflow Stream (`AgentEventStream`), read with
  `stream_agent_events`; each turn truncates the previous turn's events so workflow state stays small.
- **Sub-agents** started by the `Agent` tool run as child workflows (`ladderframe.Subagent`). Forked skills
  (`context: fork`) run inside the Skill tool's activity and are not durable step by step.
- **Object storage** (one bucket): `sessions/<agent>/<id>/` gets a history snapshot after every turn
  (kept), and Temporal payloads over `storage.payload_threshold_bytes` (256 KiB) go to `payloads/` through
  Temporal External Storage. Continue-as-new carries only the snapshot key. Idle sessions complete after
  `session_idle_timeout` (default 7d); the next message restarts the workflow from the snapshot.
- **Payload expiry:** `deploy/minio/lifecycle.json` expires `payloads/` after 45 days — keep it longer than
  the namespace retention (30 days in the compose file), or old workflows can no longer replay.
- **MinIO images:** MinIO no longer publishes free container images; set `MINIO_IMAGE` / `MINIO_MC_IMAGE`
  for `deploy/docker-compose.yaml` to a build or registry you have access to.
- **Kubernetes:** `deploy/helm/ladderframe` deploys one agent per release: a single replica with the
  `Recreate` strategy (cron jobs never run twice during a rollout), `/health` and `/ready` probes, secrets
  from `existingSecret`, an optional PVC for `var/`, and an optional Prometheus Operator `ServiceMonitor`.

  ```bash
  kubectl create secret generic coder-ladderframe --from-literal=ANTHROPIC_API_KEY=... \
    --from-literal=LADDERFRAME_API_KEY=... --from-literal=S3_ACCESS_KEY=... --from-literal=S3_SECRET_KEY=...
  helm install coder deploy/helm/ladderframe --set agent=coder \
    --set image.repository=<registry>/ladderframe-coder --set existingSecret=coder-ladderframe
  ```
- **Bash is not sandboxed by ladderframe.** It runs as the pod's user with the pod's environment (including
  secrets). Isolate it at the container level, or limit it with `permissions` rules.

Durability notes:

- Tool dependencies (`HarnessDeps`) are plain data so they can cross into activities; tools reach the
  runtime (config, agents, skills) through a per-process registry.
- A skill's `allowed-tools` / `disallowed-tools` are applied by the permission guard in workflow code,
  so they hold for the rest of the turn under Temporal as well.
- Keep agent names, sub-agent names, tool names and MCP server names stable while sessions are open:
  Temporal activity names are derived from them.

## Status

| Phase | Scope | State |
|---|---|---|
| 1 | Config + profiles, discovery, frontmatter, permissions, all 8 tools, skills, sub-agents, MCP, capabilities, inline runtime, CLI, init.d/cron.d | done |
| 2 | Temporal sessions/sub-agents/events, object storage + payload offloading, FastAPI (sessions API, Vercel AI, AG-UI, web), API-key auth, `serve`/`worker` | done |
| 3 | JWT/OIDC auth, OpenTelemetry/Logfire tracing + Prometheus metrics, `ladderframe eval` + CI, Helm chart | done |
| — | Bash sandboxing | out of scope for now (deployment-level) |

## Development

```bash
uv sync
uv run pytest
uv run ruff check . && uv run ruff format --check .
uv run pyright
```
