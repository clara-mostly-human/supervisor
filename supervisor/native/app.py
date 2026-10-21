"""Run apps natively on macOS.

A native app is an app repository folder with the usual ``config.yaml`` (no
``image``) and a ``native.yaml`` recipe::

    requirements:            # pip requirements; {version} is the app version
      - python-matter-server[server]=={version}
    build: build.sh          # optional: script run with OUT/SRC_DIR/CACHE/VERSION
    run: run.py              # what to start: a .py runs with the app's Python,
                             # anything else is executed directly
    privileged: false        # true: runs as root through the root helper

Each version is installed under ``<data>/apps/native/<slug>/versions/<version>``
(a copy of the app folder in ``app/``, a venv if there are requirements, build
output in ``out/``). Unprivileged apps run as a per-user LaunchAgent;
privileged ones are copied by the root helper into a root-owned folder and run
as a LaunchDaemon. The app gets the environment a container app gets,
translated to the host:

* ``SUPERVISOR`` / ``SUPERVISOR_TOKEN``: the Supervisor API and the app's token
* ``APP_DATA``: the app's data folder (``/data`` in a container), with
  ``options.json``; ``APP_CONFIG``, ``APP_SHARE``, ``APP_MEDIA``, ``APP_SSL``
* ``PORT_<port>_<proto>``: the host port the user picked for each port in
  ``config.yaml`` (``ports``), e.g. ``PORT_5580_TCP``
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
from dataclasses import dataclass
from ipaddress import IPv4Address
import json
import logging
import os
from pathlib import Path
import shutil
import time
from typing import TYPE_CHECKING, Any

from awesomeversion import AwesomeVersion
import yaml

from ..const import AppBoot, BusEvent
from ..docker.const import ENV_TIME, ENV_TOKEN, ENV_TOKEN_OLD, ContainerState
from ..docker.monitor import DockerContainerStateEvent
from ..exceptions import DockerError
from ..jobs.job_group import JobGroup
from . import NATIVE_RECIPE as RECIPE, helper
from .docker import LOCALHOST
from .homeassistant import _launchd_prefix, _python, _run, api_address
from .launchd import LaunchAgent, LaunchdError, ServiceStatus

if TYPE_CHECKING:
    from ..apps.app import App

_LOGGER: logging.Logger = logging.getLogger(__name__)

INSTALLED_MARKER = ".installed"
STOP_TIMEOUT = 60
POLL_SECONDS = 5
LOG_TAIL_LINES = 1000
HOST_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"


@dataclass(slots=True, frozen=True)
class InstallInfo:
    """What an installed version runs."""

    run: str
    privileged: bool

    @staticmethod
    def read(marker: Path) -> InstallInfo:
        """Read an install marker."""
        text = marker.read_text().strip()
        try:
            data = json.loads(text)
        except ValueError:  # plain run path, written before privileged apps
            return InstallInfo(run=text, privileged=False)
        return InstallInfo(run=data["run"], privileged=bool(data.get("privileged")))


class AgentService:
    """An app run as a LaunchAgent in the user's session."""

    def __init__(self, label: str) -> None:
        """Initialize."""
        self.agent = LaunchAgent(label)
        self.label = label

    async def status(self) -> ServiceStatus:
        """Return the service state."""
        return await self.agent.status()

    async def define(
        self,
        version_path: Path,
        info: InstallInfo,
        environment: dict[str, str],
        working_directory: Path,
        log_file: Path,
        run_at_load: bool,
    ) -> None:
        """Write the LaunchAgent."""
        script = version_path / "app" / info.run
        program = (
            [str(version_path / "venv" / "bin" / "python"), str(script)]
            if info.run.endswith(".py")
            else [str(script)]
        )
        await asyncio.get_running_loop().run_in_executor(
            None,
            lambda: self.agent.write_plist(
                program,
                environment=environment,
                working_directory=working_directory,
                stdout=log_file,
                stderr=log_file,
            ),
        )

    async def start(self) -> None:
        """Start it."""
        if (await self.agent.status()).loaded:
            await self.agent.kickstart()
        else:
            await self.agent.bootstrap()

    async def stop(self, timeout: float, remove: bool) -> None:
        """Stop it; remove the definition if asked."""
        await self.agent.bootout(timeout)
        if remove:
            await asyncio.get_running_loop().run_in_executor(
                None, self.agent.remove_plist
            )


class DaemonService:
    """An app run as root, as a LaunchDaemon managed by the root helper."""

    def __init__(self, prefix: str, slug: str, version: str) -> None:
        """Initialize."""
        self.params = {"prefix": prefix, "slug": slug}
        self.version = version
        self.label = f"{prefix}.app.{slug}"

    async def status(self) -> ServiceStatus:
        """Return the service state."""
        result = await helper.call("status", **self.params)
        return ServiceStatus(
            result["loaded"], result["running"], result["pid"], result["exit"]
        )

    async def define(
        self,
        version_path: Path,
        info: InstallInfo,
        environment: dict[str, str],
        working_directory: Path,
        log_file: Path,
        run_at_load: bool,
    ) -> None:
        """Have the helper write the LaunchDaemon."""
        await helper.call(
            "service",
            **self.params,
            version=self.version,
            program=f"app/{info.run}",
            env=environment,
            working_directory=str(working_directory),
            log_file=str(log_file),
            run_at_load=run_at_load,
        )

    async def start(self) -> None:
        """Start it."""
        await helper.call("start", **self.params)

    async def stop(self, timeout: float, remove: bool) -> None:
        """Stop it; remove the definition if asked."""
        await helper.call(
            "unservice" if remove else "stop", **self.params, timeout=timeout
        )


class NativeApp(JobGroup):
    """Replacement for DockerApp on a native host."""

    def __init__(self, coresys, app: App) -> None:
        """Initialize."""
        self.app = app
        super().__init__(coresys, f"native_app_{app.slug}", app.slug)
        self._service: AgentService | DaemonService = AgentService(
            f"{_launchd_prefix()}.app.{app.slug}"
        )
        self._attached_version: AwesomeVersion | None = None
        self._watch_task: asyncio.Task | None = None
        self._last_state: ContainerState | None = None
        self._expected_down = False
        self._last_exit_code: int | None = None

    # Paths

    @property
    def path_root(self) -> Path:
        """Return the folder of this app's native installs."""
        return self.sys_config.path_supervisor / "apps" / "native" / self.app.slug

    def path_version(self, version: AwesomeVersion | str) -> Path:
        """Return the install folder of a version."""
        return self.path_root / "versions" / str(version)

    @property
    def path_logs(self) -> Path:
        """Return the folder for the app's stdout/stderr."""
        return self.path_root / "logs"

    # DockerInterface compatible properties

    @staticmethod
    def slug_to_name(slug: str) -> str:
        """Convert slug to the service name used in state events."""
        return f"app_{slug}"

    @property
    def name(self) -> str:
        """Return the service name used in state events."""
        return self.slug_to_name(self.app.slug)

    @property
    def image(self) -> str | None:
        """Natively there is no image; keep the app's (build) name."""
        return self.app.image

    @property
    def version(self) -> AwesomeVersion | None:
        """Return the attached version."""
        return self._attached_version

    @property
    def arch(self) -> str | None:
        """Return the native architecture."""
        return self.sys_arch.default

    @property
    def attached(self) -> bool:
        """Return True once an installed version is attached."""
        return self._attached_version is not None

    @property
    def in_progress(self) -> bool:
        """Return True if a job of this runtime is running."""
        return self.active_job is not None

    @property
    def healthcheck(self) -> None:
        """Native apps have no container healthcheck."""
        return None

    @property
    def ip_address(self) -> IPv4Address:
        """Native apps run on the host."""
        return LOCALHOST

    @property
    def meta_config(self) -> dict[str, Any]:
        """No container metadata."""
        return {}

    # Install

    def _installed(self, version: AwesomeVersion | str) -> bool:
        return (self.path_version(version) / INSTALLED_MARKER).exists()

    async def _install(self, version: AwesomeVersion) -> None:
        """Install a version from the app's store folder."""
        store = self.app.app_store
        if store is None:
            raise DockerError(f"App {self.app.slug} has no store source to install")
        source = store.path_location
        recipe_path = source / RECIPE
        if not await self.sys_run_in_executor(recipe_path.is_file):
            raise DockerError(
                f"App {self.app.slug} has no {RECIPE}; it cannot run natively",
                _LOGGER.error,
            )
        recipe = await self.sys_run_in_executor(
            lambda: yaml.safe_load(recipe_path.read_text()) or {}
        )
        target = self.path_version(version)
        if await self.sys_run_in_executor(target.exists):
            await self.sys_run_in_executor(shutil.rmtree, target)

        _LOGGER.info("Installing app %s %s natively", self.app.slug, version)
        await self.sys_run_in_executor(
            lambda: shutil.copytree(source, target / "app", symlinks=True)
        )
        info = InstallInfo(
            run=str(recipe.get("run", "run.py")),
            privileged=bool(recipe.get("privileged")),
        )
        requirements = [
            str(req).format(version=version) for req in recipe.get("requirements", [])
        ]
        steps: list[tuple[tuple[str, ...], dict[str, str] | None]] = []
        if requirements or info.run.endswith(".py"):
            venv = target / "venv"
            pip = str(venv / "bin" / "pip")
            python = str(recipe.get("python") or _python())
            steps += [
                ((python, "-m", "venv", str(venv)), None),
                ((pip, "install", "--upgrade", "pip"), None),
            ]
            if requirements:
                steps.append(((pip, "install", *requirements), None))
        if build := recipe.get("build"):
            cache = self.path_root / "cache"
            env = {
                **os.environ,
                "SRC_DIR": str(target / "app"),
                "OUT": str(target / "out"),
                "CACHE": str(cache),
                "VERSION": str(version),
                "PATH": HOST_PATH,
            }
            await self.sys_run_in_executor(
                lambda: cache.mkdir(parents=True, exist_ok=True)
            )
            steps.append((("/bin/bash", str(target / "app" / str(build))), env))

        for step, env in steps:
            result = await _run(*step, env=env)
            if result.exit_code != 0:
                _LOGGER.error(
                    "Installing app %s %s failed:\n%s",
                    self.app.slug,
                    version,
                    "".join(result.log[-40:]),
                )
                raise DockerError(f"Can't install app {self.app.slug} {version}")

        if info.privileged:
            # Root runs a root-owned copy the user account cannot change.
            try:
                await helper.call(
                    "install",
                    prefix=_launchd_prefix(),
                    slug=self.app.slug,
                    version=str(version),
                    source=str(target),
                )
            except helper.HelperError as err:
                raise DockerError(str(err), _LOGGER.error) from err

        await self.sys_run_in_executor(
            lambda: (target / INSTALLED_MARKER).write_text(
                json.dumps({"run": info.run, "privileged": info.privileged})
            )
        )
        _LOGGER.info("App %s %s installed", self.app.slug, version)

    async def install(
        self,
        version: AwesomeVersion,
        image: str | None = None,
        latest: bool = False,
        arch: Any = None,
        **kwargs: Any,
    ) -> None:
        """Install a version unless it is installed."""
        if not await self.sys_run_in_executor(self._installed, version):
            await self._install(version)
        await self._use_version(version)

    async def update(
        self, version: AwesomeVersion, image: str | None = None, **kwargs: Any
    ) -> None:
        """Install the new version; App.update stops and restarts the app."""
        await self.install(version)

    async def check_image(self, *args: Any, **kwargs: Any) -> None:
        """Nothing to check natively."""

    async def cleanup(
        self,
        old_image: str | None = None,
        image: str | None = None,
        version: AwesomeVersion | None = None,
    ) -> None:
        """Keep the current version and the newest older one."""
        current = str(version or self._attached_version or "")
        versions = self.path_root / "versions"

        def _cleanup() -> None:
            if not versions.is_dir():
                return
            older = sorted(
                (p for p in versions.iterdir() if p.is_dir() and p.name != current),
                key=lambda p: p.stat().st_mtime,
            )
            for path in older[:-1]:
                _LOGGER.info("Removing old app %s %s", self.app.slug, path.name)
                shutil.rmtree(path, ignore_errors=True)

        await self.sys_run_in_executor(_cleanup)

    async def remove(self, *, remove_image: bool = True) -> None:
        """Stop and remove the service and, if asked, all installs."""
        await self.stop()
        if remove_image:
            if isinstance(self._service, DaemonService):
                with suppress(helper.HelperError):
                    await helper.call(
                        "remove", prefix=_launchd_prefix(), slug=self.app.slug
                    )
            await self.sys_run_in_executor(
                shutil.rmtree, self.path_root / "versions", True
            )
            self._attached_version = None

    async def export_image(self, tar_file: Path) -> None:
        """Nothing to export: a restore reinstalls from the recipe."""

    async def import_image(self, tar_file: Path) -> None:
        """Nothing to import natively."""
        raise DockerError("Native apps have no image to import", _LOGGER.debug)

    # Lifecycle

    async def attach(
        self, version: AwesomeVersion, *, skip_state_event_if_down: bool = False
    ) -> None:
        """Attach to an installed version."""
        if not await self.sys_run_in_executor(self._installed, version):
            raise DockerError(f"App {self.app.slug} {version} is not installed")
        await self._use_version(version)
        self._start_watch()
        if not skip_state_event_if_down or await self.is_running():
            self._fire(await self.current_state())

    async def exists(self) -> bool:
        """Return True if the attached version is installed."""
        return self._attached_version is not None and await self.sys_run_in_executor(
            self._installed, self._attached_version
        )

    def _environment(self) -> dict[str, str]:
        config = self.sys_config
        environment = {
            "SUPERVISOR": api_address(),
            "HASSIO": api_address(),
            ENV_TOKEN: self.app.supervisor_token or "",
            ENV_TOKEN_OLD: self.app.supervisor_token or "",
            ENV_TIME: self.sys_timezone,
            "APP_SLUG": self.app.slug,
            "APP_DATA": str(self.app.path_data),
            "APP_CONFIG": str(self.app.path_config),
            "APP_SHARE": str(config.path_share),
            "APP_MEDIA": str(config.path_media),
            "APP_SSL": str(config.path_ssl),
            "HOME": str(Path.home()),
            "PATH": HOST_PATH,
        }
        for container_port, host_port in (self.app.ports or {}).items():
            if host_port:
                port, _, proto = container_port.partition("/")
                environment[f"PORT_{port}_{(proto or 'tcp').upper()}"] = str(host_port)
        return environment

    async def _use_version(self, version: AwesomeVersion) -> None:
        """Attach a version and pick the service backend it needs."""
        info = await self.sys_run_in_executor(
            InstallInfo.read, self.path_version(version) / INSTALLED_MARKER
        )
        self._attached_version = version
        label = f"{_launchd_prefix()}.app.{self.app.slug}"
        self._service = (
            DaemonService(_launchd_prefix(), self.app.slug, str(version))
            if info.privileged
            else AgentService(label)
        )

    async def run(self, **kwargs: Any) -> None:
        """Define the service for the attached version and start it."""
        if not self._attached_version:
            raise DockerError(f"App {self.app.slug} is not installed")
        target = self.path_version(self._attached_version)
        info = await self.sys_run_in_executor(
            InstallInfo.read, target / INSTALLED_MARKER
        )
        await self.stop()
        await self.sys_run_in_executor(
            lambda: self.path_logs.mkdir(parents=True, exist_ok=True)
        )
        try:
            await self._service.define(
                target,
                info,
                self._environment(),
                self.app.path_data,
                self.path_logs / "output.log",
                run_at_load=self.app.boot == AppBoot.AUTO,
            )
        except (helper.HelperError, LaunchdError) as err:
            raise DockerError(f"Can't define app {self.app.slug}: {err}") from err
        await self.start()

    async def start(self) -> None:
        """Start the defined service."""
        try:
            await self._service.start()
        except (helper.HelperError, LaunchdError) as err:
            raise DockerError(f"Can't start app {self.app.slug}: {err}") from err
        self._start_watch()

    async def stop(self, remove_container: bool = True) -> None:
        """Stop the service; remove its definition if asked."""
        self._expected_down = True
        try:
            await self._service.stop(STOP_TIMEOUT, remove_container)
        except (helper.HelperError, LaunchdError) as err:
            raise DockerError(f"Can't stop app {self.app.slug}: {err}") from err
        finally:
            self._expected_down = False
        self._fire(ContainerState.STOPPED)

    async def restart(self) -> None:
        """Restart the service."""
        await self.stop(remove_container=False)
        await self.start()

    async def is_running(self) -> bool:
        """Return True if the app runs."""
        try:
            return (await self._service.status()).running
        except helper.HelperError:
            return False

    async def current_state(self) -> ContainerState:
        """Map the launchd state to a container state."""
        try:
            status = await self._service.status()
        except helper.HelperError:
            return ContainerState.UNKNOWN
        if status.running:
            return ContainerState.RUNNING
        if status.loaded and status.last_exit_code not in (None, 0):
            self._last_exit_code = status.last_exit_code
            return ContainerState.FAILED
        if status.loaded:
            return ContainerState.STOPPED
        return ContainerState.UNKNOWN

    async def is_failed(self) -> bool:
        """Return True if the app exited with an error."""
        return await self.current_state() == ContainerState.FAILED

    def _fire(self, state: ContainerState) -> None:
        self._last_state = state
        self.sys_bus.fire_event(
            BusEvent.DOCKER_CONTAINER_STATE_CHANGE,
            DockerContainerStateEvent(
                name=self.name,
                state=state,
                id=self._service.label,
                time=int(time.time()),
                # The watchdog logs the exit code of a failure as a number.
                exit_code=(
                    self._last_exit_code or 1
                    if state == ContainerState.FAILED
                    else None
                ),
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

    # Logs and stats

    async def logs(self) -> list[str]:
        """Return the tail of the app's output."""

        def _tail() -> list[str]:
            lines: deque[str] = deque(maxlen=LOG_TAIL_LINES)
            for name in ("output.log",):
                path = self.path_logs / name
                if path.exists():
                    with path.open(errors="replace") as file:
                        lines.extend(file)
            return list(lines)

        return await self.sys_run_in_executor(_tail)

    async def stats(self, *, one_shot: bool = False) -> Any:
        """Not implemented yet natively."""
        raise DockerError("App stats are not available on a native host yet")

    async def write_stdin(self, data: bytes) -> None:
        """Native apps have no stdin channel."""
        raise DockerError("Native apps do not accept stdin")

    async def run_inside(self, command: str) -> Any:
        """Native apps have no container to run commands in."""
        raise DockerError("Native apps cannot run commands inside")

    def __getattr__(self, name: str) -> Any:
        """Fail loudly on DockerApp members not provided natively."""
        raise AttributeError(f"NativeApp has no attribute {name!r}")
