"""Manage per-user launchd services (LaunchAgents).

Home Assistant Core runs as a LaunchAgent in the logged-in user's session,
not as a system daemon: macOS only grants Bluetooth access (TCC) to processes
in a user session.
"""

from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import plistlib
import re

_LOGGER: logging.Logger = logging.getLogger(__name__)

LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"

_RE_STATE = re.compile(r"^\s*state = (\S+)", re.MULTILINE)
_RE_PID = re.compile(r"^\s*pid = (\d+)", re.MULTILINE)
_RE_EXIT = re.compile(r"^\s*last exit code = (-?\d+|\(never exited\))", re.MULTILINE)


class LaunchdError(Exception):
    """A launchctl command failed."""


@dataclass(slots=True, frozen=True)
class ServiceStatus:
    """State of a launchd service."""

    loaded: bool
    running: bool
    pid: int | None
    last_exit_code: int | None


def domain() -> str:
    """Return the launchd domain of the current user's GUI session."""
    return f"gui/{os.getuid()}"


async def _launchctl(*args: str, check: bool = True) -> tuple[int, str]:
    proc = await asyncio.create_subprocess_exec(
        "/bin/launchctl",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    text = out.decode(errors="replace")
    if check and proc.returncode != 0:
        raise LaunchdError(f"launchctl {' '.join(args)}: {proc.returncode} {text}")
    return proc.returncode or 0, text


class LaunchAgent:
    """A LaunchAgent in the current user's session."""

    def __init__(self, label: str) -> None:
        """Initialize."""
        self.label = label
        self.plist_path = LAUNCH_AGENTS / f"{label}.plist"

    @property
    def target(self) -> str:
        """Return the service target for launchctl."""
        return f"{domain()}/{self.label}"

    def read_plist(self) -> dict | None:
        """Return the installed plist, or None."""
        try:
            with self.plist_path.open("rb") as file:
                return plistlib.load(file)
        except FileNotFoundError:
            return None

    def write_plist(
        self,
        program: list[str],
        *,
        environment: dict[str, str],
        working_directory: Path,
        stdout: Path,
        stderr: Path,
    ) -> None:
        """Write the service definition. The Supervisor restarts it, not launchd."""
        LAUNCH_AGENTS.mkdir(parents=True, exist_ok=True)
        data = {
            "Label": self.label,
            "ProgramArguments": program,
            "EnvironmentVariables": environment,
            "WorkingDirectory": str(working_directory),
            "StandardOutPath": str(stdout),
            "StandardErrorPath": str(stderr),
            "RunAtLoad": True,
            "KeepAlive": False,
            "ProcessType": "Interactive",
        }
        tmp = self.plist_path.with_suffix(".plist.tmp")
        with tmp.open("wb") as file:
            plistlib.dump(data, file)
        tmp.chmod(0o600)
        tmp.replace(self.plist_path)

    def remove_plist(self) -> None:
        """Delete the service definition."""
        self.plist_path.unlink(missing_ok=True)

    async def status(self) -> ServiceStatus:
        """Return the service state."""
        code, text = await _launchctl("print", self.target, check=False)
        if code != 0:
            return ServiceStatus(False, False, None, None)
        state = m.group(1) if (m := _RE_STATE.search(text)) else ""
        pid = int(m.group(1)) if (m := _RE_PID.search(text)) else None
        exit_code = None
        if (m := _RE_EXIT.search(text)) and m.group(1) != "(never exited)":
            exit_code = int(m.group(1))
        return ServiceStatus(True, state == "running", pid, exit_code)

    async def bootstrap(self) -> None:
        """Load the service, which starts it."""
        await _launchctl("bootstrap", domain(), str(self.plist_path))

    async def kickstart(self) -> None:
        """Start the loaded service if it is not running."""
        await _launchctl("kickstart", self.target)

    async def bootout(self, timeout: float) -> None:
        """Stop and unload the service, waiting up to timeout seconds."""
        status = await self.status()
        if not status.loaded:
            return
        # bootout sends SIGTERM and waits for the process (launchd's
        # ExitTimeOut, 20 s by default, then SIGKILL). Core may need longer
        # to shut down cleanly, so send SIGTERM ourselves and wait first.
        if status.pid:
            await _launchctl("kill", "SIGTERM", self.target, check=False)
            loop = asyncio.get_running_loop()
            deadline = loop.time() + timeout
            while loop.time() < deadline:
                await asyncio.sleep(1)
                if not (await self.status()).running:
                    break
            else:
                _LOGGER.warning(
                    "%s did not stop within %ss, killing it", self.label, timeout
                )
        await _launchctl("bootout", self.target, check=False)
