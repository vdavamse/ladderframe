"""Temporal executor (phase 2).

Design (agreed):
- One agent per pod: the FastAPI server and the Temporal worker for task queue
  `ladderframe-<agent>` run in the same process, with supercronic alongside.
- Each session is an entity workflow `session:<agent>:<session_id>`:
    Update  -> send a message and wait for the answer
    Signal  -> start an async run, returns a run id
    Query   -> history / status
- Agent runs use pydantic-ai's `TemporalDurability` capability (not the deprecated `TemporalAgent`).
- History snapshots go to MinIO `sessions/<agent>/<session_id>/` after each turn; on
  continue-as-new the workflow carries only a reference. Payloads above 256 KiB are offloaded to
  MinIO `payloads/` via Temporal External Storage (claim-check codec as fallback).
- Live events go through an in-process event bus (same pod) to SSE, with sequence numbers so
  duplicates from activity retries can be dropped.
- Sub-agents started by the `Agent` tool run as child workflows.
"""
