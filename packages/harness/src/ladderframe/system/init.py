"""`ladderframe boot`: run `etc/init.d/*` scripts in name order, like `run-parts`.

Scripts from `share/etc/init.d/` and the root's `etc/init.d/` are combined; a root script with the
same file name replaces the shared one. Execution stops at the first failing script.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path

from ..core.rootfs import AgentRoot, Layer


def collect_scripts(root: AgentRoot, share: Layer | None) -> list[Path]:
    scripts: dict[str, Path] = {}
    for layer in (share, root):  # root last so it overrides share
        if layer and layer.init_d.is_dir():
            for path in layer.init_d.iterdir():
                if path.is_file() and not path.name.startswith(".") and not path.name.endswith("~"):
                    scripts[path.name] = path
    return [scripts[name] for name in sorted(scripts)]


def run_init(root: AgentRoot, share: Layer | None, dry_run: bool = False) -> int:
    root.ensure_runtime_dirs()
    env = {**os.environ, "AGENT_ROOT": str(root.path), "AGENT_NAME": root.name}
    for script in collect_scripts(root, share):
        print(f"[boot] {script}")
        if dry_run:
            continue
        command = [str(script)] if os.access(script, os.X_OK) else ["bash", str(script)]
        result = subprocess.run(command, cwd=root.path, env=env, check=False)
        if result.returncode != 0:
            print(f"[boot] {script.name} failed with exit code {result.returncode}")
            return result.returncode
    return 0
