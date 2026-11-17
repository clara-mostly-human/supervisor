"""Manage the launchd services of Core and apps.

Two layouts, chosen by how the Supervisor itself was started
(SUPERVISOR_LAUNCHD_LAYOUT):

* session (default): LaunchAgents in the logged-in user's GUI session. Core
  can use the Mac's Bluetooth (TCC grants it only to processes in a user
  session), but everything stops when the user logs out.
* system: LaunchDaemons that run as the Supervisor's user (launchd UserName),
  defined and loaded by the root helper. They run with nobody logged in, and
  launchd daemons get local network access without a prompt (Apple TN3179),
  but not the Mac's Bluetooth: this layout is for a Bluetooth dongle.
"""

from __future__ import annotations

import asyncio
import contextlib
from dataclasses import dataclass
import logging
import os
from pathlib import Path
import plistlib
import re
import shlex
import signal

from . import helper

_LOGGER: logging.Logger = logging.getLogger(__name__)

LAUNCH_AGENTS = Path.home() / "Library" / "LaunchAgents"
LAUNCH_DAEMONS = Path("/Library/LaunchDaemons")
ENV_LAYOUT = "SUPERVISOR_LAUNCHD_LAYOUT"
LAYOUT_SESSION = "session"
LAYOUT_SYSTEM = "system"
BOOTOUT_TIMEOUT = 30
ABORT_TIMEOUT = 30
SAMPLE_SECONDS = 5
SAMPLE_TIMEOUT = 60

_RE_STATE = re.compile(r"^\s*state = (\S+)", re.MULTILINE)
_RE_PID = re.compile(r"^\s*pid = (\d+)", re.MULTILINE)
_RE_EXIT = re.compile(r"^\s*last exit code = (-?\d+|\(never exited\))", re.MULTILINE)
_RE_SIGNAL = re.compile(r"^\s*last terminating signal = ", re.MULTILINE)
_RE_ENV_KEY = re.compile(r"[A-Z_][A-Z0-9_]{0,63}")


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


def layout() -> str:
    """Return where Core and apps run: the user's session or the system domain."""
    value = os.environ.get(ENV_LAYOUT, LAYOUT_SESSION)
    if value not in (LAYOUT_SESSION, LAYOUT_SYSTEM):
        raise LaunchdError(f"{ENV_LAYOUT}={value!r}: use session or system")
    return value


def service(label: str) -> LaunchAgent:
    """Return the launchd service for label in the current layout."""
    if layout() == LAYOUT_SYSTEM:
        return UserDaemon(label)
    return LaunchAgent(label)


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


async def launchctl(*args: str, check: bool = True) -> tuple[int, str]:
    """Run launchctl; return its exit code and output."""
    return await _launchctl(*args, check=check)


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
        # A process killed by a signal (a crash) reports the signal instead.
        if state != "running" and not exit_code and _RE_SIGNAL.search(text):
            exit_code = -1
        return ServiceStatus(True, state == "running", pid, exit_code)

    async def bootstrap(self) -> None:
        """Load the service, which starts it."""
        await _launchctl("bootstrap", domain(), str(self.plist_path))

    async def kickstart(self) -> None:
        """Start the loaded service if it is not running."""
        await _launchctl("kickstart", self.target)

    async def sample(self, path: Path) -> bool:
        """Write the native stacks of the running service to path."""
        status = await self.status()
        if not status.pid:
            return False
        proc = await asyncio.create_subprocess_exec(
            "/usr/bin/sample",
            str(status.pid),
            str(SAMPLE_SECONDS),
            "-file",
            str(path),
            stdout=asyncio.subprocess.DEVNULL,
            stderr=asyncio.subprocess.DEVNULL,
        )
        try:
            return await asyncio.wait_for(proc.wait(), SAMPLE_TIMEOUT) == 0
        except TimeoutError:
            proc.kill()
            await proc.wait()
            return False

    async def _signal(self, sig: signal.Signals) -> None:
        """Send sig to the running service."""
        await _launchctl("kill", sig.name, self.target, check=False)

    async def unload(self) -> None:
        """Unload the service; launchd stops it if it still runs."""
        await _launchctl("bootout", self.target, check=False)

    async def _wait_stopped(self, timeout: float) -> bool:
        """Return True once the service is no longer running."""
        loop = asyncio.get_running_loop()
        deadline = loop.time() + timeout
        while loop.time() < deadline:
            await asyncio.sleep(1)
            if not (await self.status()).running:
                return True
        return False

    async def bootout(self, timeout: float, *, abort_first: bool = False) -> None:
        """Stop and unload the service, waiting up to timeout seconds.

        With abort_first a service that ignores SIGTERM gets SIGABRT before
        SIGKILL, so that its fault handler can record where it was stuck.
        """
        status = await self.status()
        if not status.loaded:
            return
        # bootout sends SIGTERM and waits for the process (launchd's
        # ExitTimeOut, 20 s by default, then SIGKILL). Core may need longer
        # to shut down cleanly, so send SIGTERM ourselves and wait first.
        if status.pid:
            await self._signal(signal.SIGTERM)
            if not await self._wait_stopped(timeout):
                _LOGGER.warning(
                    "%s did not stop within %ss, killing it", self.label, timeout
                )
                if abort_first:
                    await self._signal(signal.SIGABRT)
                if not abort_first or not await self._wait_stopped(ABORT_TIMEOUT):
                    await self._signal(signal.SIGKILL)
        await self.unload()
        # bootout returns at once while launchd still tears the service down,
        # and a kickstart in that window succeeds without starting anything:
        # the service then disappears. Only return once it is really gone.
        loop = asyncio.get_running_loop()
        deadline = loop.time() + BOOTOUT_TIMEOUT
        while (await self.status()).loaded:
            if loop.time() >= deadline:
                raise LaunchdError(f"{self.label} is still loaded after bootout")
            await asyncio.sleep(0.5)


class UserDaemon(LaunchAgent):
    """A LaunchDaemon that runs as the Supervisor's user, outside any session.

    Writing, loading and unloading need root, so they go through the root
    helper. The helper's plist starts /bin/sh as the user, which reads the
    environment from a file only the user can read (a plist, and launchctl
    print, show the environment to every account, and it holds the API token)
    and appends the output to the logs. Reading the definition and the state
    needs no privilege, and signals go straight to the process.
    """

    def __init__(self, label: str, state_dir: Path | None = None) -> None:
        """Initialize."""
        super().__init__(label)
        self.plist_path = LAUNCH_DAEMONS / f"{label}.plist"
        self.env_path = (state_dir or launchd_state_dir()) / f"{label}.env"
        prefix, _, name = label.partition(".")
        self._params = {"prefix": prefix, "name": name}

    @property
    def target(self) -> str:
        """Return the service target for launchctl."""
        return f"system/{self.label}"

    def read_plist(self) -> dict | None:
        """Return the definition as written: program, environment and logs."""
        data = super().read_plist()
        if data is None:
            return None
        args = data.get("ProgramArguments", [])
        if args[:2] != ["/bin/sh", "-c"] or len(args) < 8:
            return data
        env_file, stdout, stderr = args[4:7]
        try:
            environment = read_env_file(Path(env_file))
        except OSError:
            environment = {}
        return data | {
            "ProgramArguments": args[7:],
            "EnvironmentVariables": environment,
            "StandardOutPath": stdout,
            "StandardErrorPath": stderr,
        }

    def write_plist(
        self,
        program: list[str],
        *,
        environment: dict[str, str],
        working_directory: Path,
        stdout: Path,
        stderr: Path,
        keep_alive: bool = False,
        throttle_interval: int | None = None,
    ) -> None:
        """Write the environment file and have the helper define the service."""
        write_env_file(self.env_path, environment)
        params: dict[str, object] = {
            "program": program,
            "env_file": str(self.env_path),
            "working_directory": str(working_directory),
            "stdout": str(stdout),
            "stderr": str(stderr),
            "run_at_load": True,
            "keep_alive": keep_alive,
        }
        if throttle_interval is not None:
            params["throttle_interval"] = throttle_interval
        self._call_sync("user_service", **params)

    def remove_plist(self) -> None:
        """Have the helper unload and delete the definition (blocking)."""
        self._call_sync("user_unservice")
        self.env_path.unlink(missing_ok=True)

    async def bootstrap(self) -> None:
        """Load the service, which starts it."""
        await self._call("user_bootstrap")

    async def kickstart(self) -> None:
        """Start the loaded service if it is not running."""
        await self._call("user_kickstart")

    async def _signal(self, sig: signal.Signals) -> None:
        if (pid := (await self.status()).pid) is None:
            return
        with contextlib.suppress(ProcessLookupError):  # it just exited
            os.kill(pid, sig)

    async def unload(self) -> None:
        """Have the helper unload the service."""
        await self._call("user_bootout")

    async def _call(self, command: str) -> None:
        try:
            await helper.call(command, **self._params)
        except helper.HelperError as err:
            raise LaunchdError(str(err)) from err

    def _call_sync(self, command: str, **params: object) -> None:
        try:
            helper.call_sync(command, **self._params, **params)
        except helper.HelperError as err:
            raise LaunchdError(str(err)) from err


def launchd_state_dir() -> Path:
    """Return the folder for the user services' environment files."""
    if data := os.environ.get("SUPERVISOR_DATA"):
        return Path(data) / "launchd"
    return Path.home() / "Library" / "Application Support" / "ha-macos" / "launchd"


def write_env_file(path: Path, environment: dict[str, str]) -> None:
    """Write environment as sh assignments, readable by the user only."""
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    path.parent.chmod(0o700)
    text = "".join(
        f"{key}={shlex.quote(value)}\n" for key, value in sorted(environment.items())
    )
    for key in environment:
        if not _RE_ENV_KEY.fullmatch(key):
            raise LaunchdError(f"bad environment variable name {key!r}")
    tmp = path.with_suffix(".env.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC | os.O_NOFOLLOW, 0o600)
    with os.fdopen(fd, "w") as file:
        os.fchmod(file.fileno(), 0o600)
        file.write(text)
    tmp.replace(path)


def read_env_file(path: Path) -> dict[str, str]:
    """Read a file written by write_env_file."""
    environment = {}
    for token in shlex.split(path.read_text()):
        key, _, value = token.partition("=")
        environment[key] = value
    return environment
