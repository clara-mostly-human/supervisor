"""Client for the root helper (macos/helper/ha_macos_helper.py).

Apps that need root are installed into a root-owned folder and run as
LaunchDaemons by the helper; the Supervisor itself stays unprivileged.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
from typing import Any

SOCKET = os.environ.get("SUPERVISOR_HELPER_SOCKET", "/var/run/ha-macos-helper.sock")
TIMEOUT = 900


class HelperError(Exception):
    """The helper is missing or refused a request."""


async def call(command: str, **params: Any) -> dict[str, Any]:
    """Send one request to the helper and return its result."""
    try:
        reader, writer = await asyncio.open_unix_connection(SOCKET)
    except OSError as err:
        raise HelperError(
            f"The root helper is not running ({SOCKET}): install it with "
            "macos/helper/install.sh"
        ) from err
    try:
        writer.write((json.dumps({"command": command, **params}) + "\n").encode())
        await writer.drain()
        line = await asyncio.wait_for(reader.readline(), TIMEOUT)
    finally:
        writer.close()
    return _result(command, line)


def call_sync(command: str, **params: Any) -> dict[str, Any]:
    """Send one request to the helper from a worker thread (blocking)."""
    request = (json.dumps({"command": command, **params}) + "\n").encode()
    try:
        with socket.socket(socket.AF_UNIX, socket.SOCK_STREAM) as sock:
            sock.settimeout(TIMEOUT)
            sock.connect(SOCKET)
            sock.sendall(request)
            line = b""
            while not line.endswith(b"\n"):
                if not (chunk := sock.recv(65536)):
                    break
                line += chunk
    except OSError as err:
        raise HelperError(
            f"The root helper is not reachable ({SOCKET}): {err}"
        ) from err
    return _result(command, line)


def _result(command: str, line: bytes) -> dict[str, Any]:
    try:
        reply = json.loads(line)
    except ValueError as err:
        raise HelperError(f"Bad reply from the root helper: {line!r}") from err
    if not reply.get("ok"):
        raise HelperError(f"Root helper refused {command}: {reply.get('error')}")
    return reply.get("result") or {}


async def available() -> bool:
    """Return True if the helper answers."""
    try:
        await call("ping")
    except HelperError:
        return False
    return True
