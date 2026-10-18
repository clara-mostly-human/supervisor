"""Logs on a native (macOS) host, where there is no systemd journal.

The log API (Settings > System > Logs, an app's Log tab) reads journald on
Home Assistant OS. Natively each service writes a log file, so the same
endpoints serve the tail of that file and can follow it.
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
import re
from typing import TYPE_CHECKING

from aiohttp import web

from ..const import SUPERVISOR_DOCKER_NAME

if TYPE_CHECKING:
    from ..coresys import CoreSys

_RE_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_BLOCK = 64 * 1024
_FOLLOW_INTERVAL = 1.0


def log_file(coresys: CoreSys, identifier: str | list[str] | None) -> Path | None:
    """Return the log file for a journald syslog identifier, if there is one."""
    identifiers = [identifier] if isinstance(identifier, str) else identifier or []
    for name in identifiers:
        if name == "homeassistant":
            return coresys.config.path_homeassistant / "home-assistant.log"
        if name == SUPERVISOR_DOCKER_NAME:
            if path := os.environ.get("SUPERVISOR_LOG_FILE"):
                return Path(path)
        for prefix in ("app_", "addon_"):
            if name.startswith(prefix):
                slug = name.removeprefix(prefix)
                return (
                    coresys.config.path_supervisor
                    / "apps"
                    / "native"
                    / slug
                    / "logs"
                    / "output.log"
                )
    return None


def tail(path: Path, lines: int) -> tuple[list[str], int]:
    """Return the last lines of a file and the file size, reading from the end."""
    with path.open("rb") as file:
        file.seek(0, os.SEEK_END)
        size = position = file.tell()
        data = b""
        while position > 0 and data.count(b"\n") <= lines:
            step = min(_BLOCK, position)
            position -= step
            file.seek(position)
            data = file.read(step) + data
    text = data.decode(errors="replace").splitlines()
    return text[-lines:], size


async def stream(
    coresys: CoreSys,
    request: web.Request,
    identifier: str | list[str] | None,
    *,
    lines: int,
    follow: bool,
    no_colors: bool,
) -> web.StreamResponse:
    """Stream the tail of a service's log file, then follow it if asked."""
    response = web.StreamResponse()
    response.content_type = "text/plain"
    response.headers["X-Accel-Buffering"] = "no"
    await response.prepare(request)

    def clean(line: str) -> bytes:
        return ((_RE_ANSI.sub("", line) if no_colors else line) + "\n").encode()

    path = log_file(coresys, identifier)
    if path is None or not await coresys.run_in_executor(path.is_file):
        await response.write(
            b"No log file for this service on this macOS host (yet).\n"
        )
        return response

    text, offset = await coresys.run_in_executor(tail, path, lines)
    for line in text:
        await response.write(clean(line))
    if not follow:
        return response

    pending = b""
    while True:
        await asyncio.sleep(_FOLLOW_INTERVAL)

        def _read(offset: int = offset) -> tuple[bytes, int]:
            size = path.stat().st_size
            if size < offset:  # rotated or truncated
                offset = 0
            with path.open("rb") as file:
                file.seek(offset)
                data = file.read()
            return data, offset + len(data)

        try:
            data, offset = await coresys.run_in_executor(_read)
        except FileNotFoundError:
            continue
        pending += data
        *complete, pending = pending.split(b"\n")
        for raw in complete:
            await response.write(clean(raw.decode(errors="replace")))
