"""Temporal executor: sessions as entity workflows, durable agent runs, events over Workflow Streams.

- One agent per pod: the FastAPI server and the worker for task queue `ladderframe-<agent>` run in
  the same process (`ladderframe serve`), or the worker alone (`ladderframe worker`).
- Session workflow `session:<agent>:<id>` (session_workflow.py): update `submit`, update `wait`,
  query `turn`, signal `close`. Idle sessions complete after `session_idle_timeout`; the next
  message restarts them from the object-storage snapshot.
- Agent runs use pydantic-ai's `TemporalDurability`; the main agent publishes its events to the
  session's `AgentEventStream` (topic `agent-events`), which clients read with `stream_agent_events`.
- Sub-agents from the `Agent` tool run as child workflows (subagent_workflow.py).
- Snapshots go to `sessions/`, and payloads above 256 KiB to `payloads/` through Temporal
  External Storage, in the same bucket (client.py, storage/).
"""
