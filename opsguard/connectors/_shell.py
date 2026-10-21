"""Running real commands for the `local` backends.

Commands are always an argument list passed straight to the executable -
never a shell string - so an LLM-chosen argument like `nginx; rm -rf /`
is just a (nonexistent) container name, not a second command. Arguments
that name things (containers, services, nodes) are additionally checked
against a conservative character set before they get here.
"""

from __future__ import annotations

import asyncio
import re
import shutil

from opsguard.tools import ToolError

_SAFE_NAME = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.@:-]{0,127}$")


def safe_name(value: str, what: str) -> str:
    if not _SAFE_NAME.match(value):
        raise ToolError(f"invalid {what} name {value!r}")
    return value


def require_binary(name: str) -> str:
    path = shutil.which(name)
    if path is None:
        raise ToolError(f"'{name}' is not available on this machine")
    return path


async def run(argv: list[str], timeout: float = 20.0, merge_stderr: bool = False) -> str:
    argv = [require_binary(argv[0]), *argv[1:]]
    proc = await asyncio.create_subprocess_exec(
        *argv,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT if merge_stderr else asyncio.subprocess.PIPE,
    )
    try:
        out, err = await asyncio.wait_for(proc.communicate(), timeout)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise ToolError(f"'{argv[0]}' timed out after {timeout:.0f}s")
    if proc.returncode != 0:
        message = (err or b"").decode(errors="replace").strip() or out.decode(errors="replace").strip()
        raise ToolError(f"command failed ({proc.returncode}): {message[:500]}")
    return out.decode(errors="replace")


def tail(text: str, lines: int) -> list[str]:
    return text.splitlines()[-lines:]
