"""Security notes.

In place: permission rules (core/permissions.py), SSRF protection in WebFetch (pydantic-ai's safe
download), tool output truncation, usage limits, API-key / JWT auth on the server.

Not handled by ladderframe (deliberately left to the deployment): Bash sandboxing. `Bash` runs
commands as the pod's user with the pod's environment, which includes secrets such as model API keys;
isolate it at the container / pod level, or restrict it with `permissions` rules.
"""
