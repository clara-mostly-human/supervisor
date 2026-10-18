"""Run Home Assistant Core natively on macOS.

Each Core version is installed into its own virtual environment under
``<data>/core/versions/<version>``, and runs as a per-user launchd service.
Installing a new version never touches the running one, so a failed update
rolls back by starting the previous version again, like switching images.

Two optional files under ``<data>/core`` shape every install:

* ``extra-requirements.txt``: packages installed next to Core, e.g. an
  editable development checkout of a library a custom integration uses.
* ``patches/*.patch``: unified diffs applied to site-packages after install,
  for fixes not yet released upstream. An install fails if a patch does not
  apply, so a patch never silently goes missing on update.
"""

from __future__ import annotations

import asyncio
from collections import deque
from ipaddress import IPv4Address
import logging
import os
from pathlib import Path
import shutil
import time
from typing import TYPE_CHECKING, Any

from awesomeversion import AwesomeVersion

from ..const import SOCKET_CORE, BusEvent
from ..docker.const import (
    ENV_CORE_API_SOCKET,
    ENV_DUPLICATE_LOG_FILE,
    ENV_TIME,
    ENV_TOKEN,
    ENV_TOKEN_OLD,
    ContainerState,
)
from ..docker.manager import CommandReturn
from ..docker.monitor import DockerContainerStateEvent
from ..exceptions import DockerError
from ..homeassistant.const import LANDINGPAGE
from ..jobs.job_group import JobGroup
from .docker import LOCALHOST
from .launchd import LaunchAgent, LaunchdError

if TYPE_CHECKING:
    from ..coresys import CoreSys

_LOGGER: logging.Logger = logging.getLogger(__name__)

HASS_NAME = "homeassistant"
ENV_RESTORE_JOB_ID = "SUPERVISOR_RESTORE_JOB_ID"
STOP_TIMEOUT = 240
POLL_SECONDS = 5
LOG_TAIL_LINES = 1000


def _python() -> str:
    """Return the Python used to create Core virtual environments."""
    return os.environ.get("SUPERVISOR_CORE_PYTHON", "/opt/homebrew/bin/python3.14")


def _launchd_prefix() -> str:
    return os.environ.get("SUPERVISOR_LAUNCHD_PREFIX", "ha-macos")


def api_address() -> str:
    """Return host:port of the Supervisor API as Core must reach it."""
    port = os.environ.get("SUPERVISOR_API_PORT", "80")
    return f"{LOCALHOST}:{port}" if port != "80" else str(LOCALHOST)


async def _run(
    *args: str, cwd: Path | None = None, env: dict[str, str] | None = None
) -> CommandReturn:
    """Run a command and collect its output."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    out, _ = await proc.communicate()
    return CommandReturn(
        proc.returncode or 0, out.decode(errors="replace").splitlines(True)
    )


class InstallError(Exception):
    """Installing a Core version failed."""


INSTALLED_MARKER = ".installed"


def version_path(path_core: Path, version: AwesomeVersion | str) -> Path:
    """Return the virtual environment of a Core version."""
    return path_core / "versions" / str(version)


def is_installed(path_core: Path, version: AwesomeVersion | str) -> bool:
    """Return True if a Core version finished installing."""
    return (version_path(path_core, version) / INSTALLED_MARKER).exists()


async def install_core(path_core: Path, version: AwesomeVersion | str) -> None:
    """Install a Core version, its extra requirements and carried patches.

    The venv is built in place (a venv is not relocatable) and only counts as
    installed once the marker file is written, so a half-finished install is
    never used and is rebuilt on the next attempt.
    """
    venv = version_path(path_core, version)
    loop = asyncio.get_running_loop()
    if venv.exists():
        await loop.run_in_executor(None, shutil.rmtree, venv)

    _LOGGER.info("Installing Home Assistant Core %s into %s", version, venv)
    pip = str(venv / "bin" / "pip")
    steps: list[tuple[str, ...]] = [
        (_python(), "-m", "venv", str(venv)),
        (pip, "install", "--upgrade", "pip"),
        (pip, "install", f"homeassistant=={version}"),
    ]
    extra = path_core / "extra-requirements.txt"
    if extra.exists():
        steps.append((pip, "install", "-r", str(extra)))

    for step in steps:
        result = await _run(*step)
        if result.exit_code != 0:
            _LOGGER.error(
                "Installing Core %s failed at %s:\n%s",
                version,
                " ".join(step[:4]),
                "".join(result.log[-40:]),
            )
            raise InstallError(f"Can't install Home Assistant Core {version}")

    patches = path_core / "patches"
    if patches.is_dir():
        site = next((venv / "lib").glob("python3*/site-packages"))
        for patch in sorted(patches.glob("*.patch")):
            result = await _run(
                "/usr/bin/patch",
                "-p1",
                "--forward",
                "--batch",
                "-i",
                str(patch),
                cwd=site,
            )
            if result.exit_code != 0:
                _LOGGER.error(
                    "Patch %s does not apply to Core %s:\n%s",
                    patch.name,
                    version,
                    "".join(result.log),
                )
                raise InstallError(f"Patch {patch.name} does not apply to {version}")
            _LOGGER.info("Applied %s to Core %s", patch.name, version)

    (venv / INSTALLED_MARKER).touch()
    _LOGGER.info("Home Assistant Core %s installed", version)


class NativeHomeAssistant(JobGroup):
    """Replacement for DockerHomeAssistant on a native host."""

    def __init__(self, coresys: CoreSys) -> None:
        """Initialize."""
        super().__init__(coresys, "native_home_assistant", HASS_NAME)
        self._version: AwesomeVersion | None = None
        self._environment: dict[str, str] = {}
        self._watch_task: asyncio.Task | None = None
        self._last_state: ContainerState | None = None
        self.agent = LaunchAgent(f"{_launchd_prefix()}.core")

    # Paths

    @property
    def path_core(self) -> Path:
        """Return the root of the native Core installs."""
        return self.sys_config.path_supervisor / "core"

    def path_version(self, version: AwesomeVersion) -> Path:
        """Return the virtual environment of a Core version."""
        return version_path(self.path_core, version)

    @property
    def path_logs(self) -> Path:
        """Return the folder for Core's stdout/stderr."""
        return self.path_core / "logs"

    # DockerInterface compatible properties

    @property
    def name(self) -> str:
        """Return the service name used in state events."""
        return HASS_NAME

    @property
    def timeout(self) -> int:
        """Return the stop timeout."""
        return STOP_TIMEOUT

    @property
    def attached(self) -> bool:
        """Return True once a version is attached."""
        return self._version is not None

    @property
    def meta_config(self) -> dict[str, Any]:
        """Return the service environment in Docker's metadata shape."""
        return {"Env": [f"{key}={value}" for key, value in self._environment.items()]}

    @property
    def version(self) -> AwesomeVersion | None:
        """Return the attached Core version."""
        return self._version

    @property
    def image(self) -> str | None:
        """Natively the 'image' is the Core venv; keep the configured name."""
        return self.sys_homeassistant.default_image

    @property
    def machine(self) -> str | None:
        """Return the machine type."""
        return self.sys_machine

    @property
    def arch(self) -> str | None:
        """Return the native architecture."""
        return self.sys_arch.default

    @property
    def ip_address(self) -> IPv4Address:
        """Core listens on the host."""
        return LOCALHOST

    @property
    def in_progress(self) -> bool:
        """Return True if a job of this runtime is running."""
        return self.active_job is not None

    @property
    def restart_policy(self) -> None:
        """The Supervisor's watchdog restarts Core, not launchd."""
        return None

    # Install

    def _installed(self, version: AwesomeVersion) -> bool:
        return is_installed(self.path_core, version)

    async def _install(self, version: AwesomeVersion) -> None:
        """Install a Core version into its own virtual environment."""
        if version == LANDINGPAGE:
            raise DockerError("There is no native landing page", _LOGGER.debug)
        try:
            await install_core(self.path_core, version)
        except InstallError as err:
            raise DockerError(str(err), _LOGGER.error) from err

    async def install(
        self,
        version: AwesomeVersion,
        image: str | None = None,
        latest: bool = False,
        **kwargs: Any,
    ) -> None:
        """Install a Core version unless it is already installed."""
        if not await self.sys_run_in_executor(self._installed, version):
            await self._install(version)
        self._version = version

    async def update(
        self, version: AwesomeVersion, image: str | None = None, latest: bool = False
    ) -> None:
        """Install the new version, then stop the running one."""
        await self.install(version)
        await self.stop()

    async def check_image(self, *args: Any, **kwargs: Any) -> None:
        """Nothing to check natively."""

    async def get_latest_version(self) -> AwesomeVersion:
        """Return the latest Core release known to the updater."""
        if version := self.sys_updater.version_homeassistant:
            return version
        raise DockerError("No Home Assistant version information", _LOGGER.warning)

    async def cleanup(
        self,
        old_image: str | None = None,
        image: str | None = None,
        version: AwesomeVersion | None = None,
    ) -> None:
        """Keep the current version and the newest older one (for rollback)."""
        current = str(version or self._version or "")
        versions = self.path_core / "versions"

        def _cleanup() -> None:
            if not versions.is_dir():
                return
            older = sorted(
                (p for p in versions.iterdir() if p.is_dir() and p.name != current),
                key=lambda p: p.stat().st_mtime,
            )
            for path in older[:-1]:
                _LOGGER.info("Removing old Home Assistant Core %s", path.name)
                shutil.rmtree(path, ignore_errors=True)

        await self.sys_run_in_executor(_cleanup)

    # Lifecycle

    async def attach(
        self, version: AwesomeVersion, *, skip_state_event_if_down: bool = False
    ) -> None:
        """Attach to an installed version and the service if it runs."""
        if not await self.sys_run_in_executor(self._installed, version):
            raise DockerError(f"Home Assistant Core {version} is not installed")
        self._version = version
        if plist := await self.sys_run_in_executor(self.agent.read_plist):
            self._environment = dict(plist.get("EnvironmentVariables", {}))
        self._start_watch()
        if not skip_state_event_if_down or await self.is_running():
            self._fire(await self.current_state())

    def _program(self) -> list[str]:
        assert self._version
        return [
            str(self.path_version(self._version) / "bin" / "hass"),
            "-c",
            str(self.sys_config.path_homeassistant),
        ]

    async def is_initialize(self) -> bool:
        """Return True if the service is defined for the current version."""
        plist = await self.sys_run_in_executor(self.agent.read_plist)
        if not plist or not self._version:
            return False
        return plist.get("ProgramArguments") == self._program()

    async def exists(self) -> bool:
        """Return True if the attached version is installed."""
        return self._version is not None and await self.sys_run_in_executor(
            self._installed, self._version
        )

    async def run(self, *, restore_job_id: str | None = None) -> None:
        """Define the service for the current version and start it."""
        if not self._version:
            raise DockerError("No Home Assistant Core version to run")
        environment = {
            "SUPERVISOR": api_address(),
            "HASSIO": api_address(),
            ENV_TIME: self.sys_timezone,
            ENV_TOKEN: self.sys_homeassistant.supervisor_token or "",
            ENV_TOKEN_OLD: self.sys_homeassistant.supervisor_token or "",
            # Natively there is no container log to read, so keep Core's
            # usual home-assistant.log in the config folder as well.
            ENV_DUPLICATE_LOG_FILE: "1",
            "HOME": str(Path.home()),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
        }
        if restore_job_id:
            environment[ENV_RESTORE_JOB_ID] = restore_job_id
        if self.sys_homeassistant.api.supports_unix_socket:
            await self.sys_run_in_executor(
                lambda: SOCKET_CORE.parent.mkdir(parents=True, exist_ok=True)
            )
            environment[ENV_CORE_API_SOCKET] = str(SOCKET_CORE)

        await self.stop()

        def _write() -> None:
            self.path_logs.mkdir(parents=True, exist_ok=True)
            self.agent.write_plist(
                self._program(),
                environment=environment,
                working_directory=self.sys_config.path_homeassistant,
                stdout=self.path_logs / "stdout.log",
                stderr=self.path_logs / "stderr.log",
            )

        await self.sys_run_in_executor(_write)
        self._environment = environment
        await self.start()

    async def start(self) -> None:
        """Start the defined service."""
        try:
            if (await self.agent.status()).loaded:
                await self.agent.kickstart()
            else:
                await self.agent.bootstrap()
        except LaunchdError as err:
            raise DockerError(f"Can't start Home Assistant Core: {err}") from err
        self._start_watch()

    async def stop(self, remove_container: bool = True) -> None:
        """Stop the service; remove its definition if asked."""
        self._expected_down = True
        try:
            await self.agent.bootout(self.timeout)
        except LaunchdError as err:
            raise DockerError(f"Can't stop Home Assistant Core: {err}") from err
        finally:
            self._expected_down = False
        if remove_container:
            await self.sys_run_in_executor(self.agent.remove_plist)

    async def restart(self) -> None:
        """Restart the service."""
        await self.stop(remove_container=False)
        await self.start()

    async def remove(self, *, remove_image: bool = True) -> None:
        """Remove the service and, if asked, the installed version."""
        await self.stop()
        if remove_image and self._version:
            await self.sys_run_in_executor(
                shutil.rmtree, self.path_version(self._version), True
            )

    async def is_running(self) -> bool:
        """Return True if Core is running."""
        return (await self.agent.status()).running

    async def current_state(self) -> ContainerState:
        """Map the launchd state to a container state."""
        status = await self.agent.status()
        if status.running:
            return ContainerState.RUNNING
        if status.loaded and status.last_exit_code not in (None, 0):
            return ContainerState.FAILED
        return ContainerState.STOPPED

    async def is_failed(self) -> bool:
        """Return True if Core exited with an error."""
        return await self.current_state() == ContainerState.FAILED

    # Watchdog support: launchd has no event stream we can subscribe to, so
    # poll the service and fire the same events the Docker monitor fires.

    _expected_down: bool = False

    def _fire(self, state: ContainerState) -> None:
        self._last_state = state
        status_exit = None
        self.sys_bus.fire_event(
            BusEvent.DOCKER_CONTAINER_STATE_CHANGE,
            DockerContainerStateEvent(
                name=self.name,
                state=state,
                id=self.agent.label,
                time=int(time.time()),
                exit_code=status_exit,
            ),
        )

    def _start_watch(self) -> None:
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = self.sys_create_task(self._watch())

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(POLL_SECONDS)
            if self._expected_down:
                continue
            state = await self.current_state()
            if state != self._last_state:
                self._fire(state)

    # Commands and logs

    async def execute_command(self, command: list[str]) -> CommandReturn:
        """Run a Core command (only 'python3 -m homeassistant ...' is used)."""
        if not self._version:
            raise DockerError("No Home Assistant Core installed")
        python = str(self.path_version(self._version) / "bin" / "python3")
        args = [python if arg == "python3" else arg for arg in command]
        args = [
            str(self.sys_config.path_homeassistant) if arg == "/config" else arg
            for arg in args
        ]
        return await _run(*args, env={**os.environ, ENV_TIME: self.sys_timezone})

    async def logs(self) -> list[str]:
        """Return the tail of Core's output."""

        def _tail() -> list[str]:
            lines: deque[str] = deque(maxlen=LOG_TAIL_LINES)
            for name in ("stdout.log", "stderr.log"):
                path = self.path_logs / name
                if path.exists():
                    with path.open(errors="replace") as file:
                        lines.extend(file)
            return list(lines)

        return await self.sys_run_in_executor(_tail)

    async def stats(self, *, one_shot: bool = False) -> Any:
        """Not implemented yet natively."""
        raise DockerError("Core stats are not available on a native host yet")

    def __getattr__(self, name: str) -> Any:
        """Fail loudly on DockerInterface members not provided natively."""
        raise AttributeError(f"NativeHomeAssistant has no attribute {name!r}")
