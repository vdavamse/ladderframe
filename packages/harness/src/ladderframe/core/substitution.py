"""Skill body rendering, following Claude Code's skill string substitutions.

$ARGUMENTS            all arguments as typed
$ARGUMENTS[N] / $N    positional argument (0-based)
$name                 named argument from the `arguments` frontmatter field
${CLAUDE_SKILL_DIR}   the skill's directory       (alias: ${SKILL_DIR})
${CLAUDE_PROJECT_DIR} the agent's working dir     (alias: ${PROJECT_DIR})
${CLAUDE_SESSION_ID}  the session id              (alias: ${SESSION_ID})
${AGENT_ROOT}         the agent root
!`cmd`  and  ```! ... ```   shell output injected before the model sees the skill
\\$                    literal dollar sign
"""

from __future__ import annotations

import asyncio
import re
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass

_INLINE_CMD = re.compile(r"(?:(?<=\s)|^)!`([^`\n]+)`", re.MULTILINE)
_BLOCK_CMD = re.compile(r"^```!\s*\n(.*?)\n```\s*$", re.MULTILINE | re.DOTALL)
_ESCAPED_DOLLAR = "\x00DOLLAR\x00"


class SkillRenderError(RuntimeError):
    pass


@dataclass
class RenderContext:
    skill_dir: str
    project_dir: str
    agent_root: str
    session_id: str = ""
    effort: str = ""


ShellRunner = Callable[[str], Awaitable[str]]


def substitute(body: str, arguments: str, names: list[str], ctx: RenderContext) -> str:
    """Expand argument and path placeholders. Appends `ARGUMENTS: ...` if the body never references them."""
    try:
        args = shlex.split(arguments) if arguments else []
    except ValueError:
        args = arguments.split()

    text = body.replace("\\$", _ESCAPED_DOLLAR)
    arg_patterns = [r"\$ARGUMENTS", r"\$\d"] + [rf"\${re.escape(n)}\b" for n in names]
    references_args = bool(re.search("|".join(arg_patterns), text))

    variables = {
        "CLAUDE_SKILL_DIR": ctx.skill_dir,
        "SKILL_DIR": ctx.skill_dir,
        "CLAUDE_PROJECT_DIR": ctx.project_dir,
        "PROJECT_DIR": ctx.project_dir,
        "CLAUDE_SESSION_ID": ctx.session_id,
        "SESSION_ID": ctx.session_id,
        "CLAUDE_EFFORT": ctx.effort,
        "AGENT_ROOT": ctx.agent_root,
    }
    text = re.sub(r"\$\{([A-Z_]+)\}", lambda m: variables.get(m.group(1), m.group(0)), text)

    def positional(index: int) -> str:
        return args[index] if index < len(args) else ""

    text = re.sub(r"\$ARGUMENTS\[(\d+)\]", lambda m: positional(int(m.group(1))), text)
    text = text.replace("$ARGUMENTS", arguments)
    text = re.sub(r"\$(\d+)\b", lambda m: positional(int(m.group(1))), text)
    # Longest names first so `$issue_id` is not clobbered by `$issue`.
    for index, name in sorted(enumerate(names), key=lambda item: -len(item[1])):
        text = re.sub(rf"\${re.escape(name)}\b", positional(index).replace("\\", "\\\\"), text)

    text = text.replace(_ESCAPED_DOLLAR, "$")
    if arguments and not references_args:
        text += f"\n\nARGUMENTS: {arguments}"
    return text


async def inject_shell_output(text: str, run: ShellRunner | None) -> str:
    """Replace `` !`cmd` `` and ```` ```! ```` blocks with command output. `run=None` means disabled by policy."""
    commands = [(m.group(0), m.group(1)) for m in _BLOCK_CMD.finditer(text)]
    commands += [(m.group(0), m.group(1)) for m in _INLINE_CMD.finditer(text)]
    if not commands:
        return text
    outputs = []
    for _, command in commands:
        outputs.append("[shell command execution disabled by policy]" if run is None else await run(command))
    for (placeholder, _), output in zip(commands, outputs, strict=True):
        text = text.replace(placeholder, output.rstrip("\n"), 1)
    return text


async def run_shell(command: str, cwd: str, timeout: float = 30) -> str:
    """Run a skill injection command; a failure aborts the whole skill, like Claude Code."""
    process = await asyncio.create_subprocess_exec(
        "bash", "-c", command, cwd=cwd, stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout)
    except TimeoutError as exc:
        process.kill()
        raise SkillRenderError(f"skill command timed out: {command}") from exc
    output = stdout.decode(errors="replace")
    if process.returncode not in (0, 1) or (process.returncode == 1 and not _is_search_command(command)):
        raise SkillRenderError(f"skill command failed (exit {process.returncode}): {command}\n{output}")
    return output


def _is_search_command(command: str) -> bool:
    # Exit 1 means "no match" for search/comparison commands; Claude Code tolerates it for these.
    first = command.strip().split()[0] if command.strip() else ""
    return first in {"grep", "rg", "diff", "test", "[", "cmp", "egrep", "fgrep"}
