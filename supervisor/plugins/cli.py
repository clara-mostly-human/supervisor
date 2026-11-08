"""Home Assistant cli plugin.

Code: https://github.com/home-assistant/plugin-cli
"""

import asyncio
from collections.abc import Awaitable
from contextlib import suppress
import logging
import secrets

from awesomeversion import AwesomeVersion

from ..const import ATTR_ACCESS_TOKEN
from ..coresys import CoreSys
from ..docker.cli import DockerCli
from ..docker.const import ContainerState
from ..docker.stats import DockerStats
from ..exceptions import (
    CliError,
    CliJobError,
    CliNotRunningError,
    CliStatsTimeoutError,
    CliUnknownError,
    CliUpdateError,
    DockerContainerNotFoundError,
    DockerContainerNotRunningError,
    DockerError,
    DockerStatsTimeoutError,
    PluginError,
)
from ..jobs.const import JobThrottle
from ..jobs.decorator import Job
from ..native import IS_MACOS
from ..native.cli import NativeCli
from ..utils.sentry import async_capture_exception
from .base import PluginBase
from .const import (
    FILE_HASSIO_CLI,
    PLUGIN_UPDATE_CONDITIONS,
    WATCHDOG_THROTTLE_MAX_CALLS,
    WATCHDOG_THROTTLE_PERIOD,
)
from .validate import SCHEMA_CLI_CONFIG

_LOGGER: logging.Logger = logging.getLogger(__name__)

NATIVE_BUILD_ATTEMPTS = 3
NATIVE_BUILD_RETRY_SECONDS = 120


class PluginCli(PluginBase):
    """HA cli interface inside supervisor."""

    def __init__(self, coresys: CoreSys):
        """Initialize cli handler."""
        super().__init__(FILE_HASSIO_CLI, SCHEMA_CLI_CONFIG)
        self.slug = "cli"
        self.coresys: CoreSys = coresys
        self.instance: DockerCli = (
            NativeCli(coresys)  # type: ignore[assignment]
            if IS_MACOS
            else DockerCli(coresys)
        )

    @property
    def default_image(self) -> str:
        """Return default image for cli plugin."""
        if self.sys_updater.image_cli:
            return self.sys_updater.image_cli
        return super().default_image

    @property
    def latest_version(self) -> AwesomeVersion | None:
        """Return version of latest cli."""
        return self.sys_updater.version_cli

    @property
    def supervisor_token(self) -> str | None:
        """Return an access token for the Supervisor API."""
        return self._data.get(ATTR_ACCESS_TOKEN)

    @Job(
        name="plugin_cli_update",
        conditions=PLUGIN_UPDATE_CONDITIONS,
        on_condition=CliJobError,
    )
    async def update(self, version: AwesomeVersion | None = None) -> None:
        """Update local HA cli."""
        try:
            await super().update(version)
        except (DockerError, PluginError) as err:
            raise CliUpdateError("CLI update failed", _LOGGER.error) from err

    async def start(self) -> None:
        """Run cli."""
        # Create new API token
        self._data[ATTR_ACCESS_TOKEN] = secrets.token_hex(56)
        await self.save_data()

        # Start Instance
        _LOGGER.info("Starting CLI plugin")
        try:
            await self.instance.run()
        except DockerError as err:
            raise CliError("Can't start cli plugin", _LOGGER.error) from err

    async def stop(self) -> None:
        """Stop cli."""
        _LOGGER.info("Stopping cli plugin")
        try:
            await self.instance.stop()
        except DockerError as err:
            raise CliError("Can't stop cli plugin", _LOGGER.error) from err

    async def stats(self, *, one_shot: bool = False) -> DockerStats:
        """Return stats of cli."""
        try:
            return await self.instance.stats(one_shot=one_shot)
        except (DockerContainerNotFoundError, DockerContainerNotRunningError) as err:
            raise CliNotRunningError(_LOGGER.warning) from err
        except DockerStatsTimeoutError as err:
            raise CliStatsTimeoutError(_LOGGER.error) from err
        except DockerError as err:
            _LOGGER.error("Could not get stats of container for HA cli: %s", err)
            raise CliUnknownError from err

    async def load(self) -> None:
        """Load the CLI; natively, build it in the background the first time."""
        if IS_MACOS and self.enabled and not await self.instance.exists():
            with suppress(DockerError):
                version = self.version or await self.instance.get_latest_version()
                await self.instance.attach(version=version)
        if IS_MACOS and self.enabled and not await self.instance.exists():
            # A Go build takes a minute and needs the network: never hold up
            # the Supervisor's startup for a command-line tool
            self.sys_create_task(self._build_natively())
            return
        await super().load()

    async def _build_natively(self) -> None:
        """Build and link the native ha command, with a few retries."""
        for attempt in range(NATIVE_BUILD_ATTEMPTS):
            if attempt:
                await asyncio.sleep(NATIVE_BUILD_RETRY_SECONDS)
            try:
                # Like update: never side by side with `ha cli update`
                async with self._lifecycle_lock:
                    if not await self.instance.exists():
                        await self._install()
                    await self.start()
            except Exception as err:  # pylint: disable=broad-except
                # A background task: log every failure, never lose it silently
                _LOGGER.warning("Can't set up the ha command: %s", err)
            else:
                return
        _LOGGER.error(
            "The ha command isn't set up; it is tried again when the Supervisor starts"
        )

    def is_running(self) -> Awaitable[bool]:
        """Return True if Docker container is running.

        Return a coroutine.
        """
        return self.instance.is_running()

    async def repair(self) -> None:
        """Repair cli container."""
        if await self.instance.exists():
            return

        _LOGGER.info("Repairing HA cli %s", self.version)
        try:
            await self.instance.install(self.version)
        except DockerError as err:
            _LOGGER.error("Repair of HA cli failed")
            await async_capture_exception(err)

    @Job(
        name="plugin_cli_restart_after_problem",
        throttle_period=WATCHDOG_THROTTLE_PERIOD,
        throttle_max_calls=WATCHDOG_THROTTLE_MAX_CALLS,
        on_condition=CliJobError,
        throttle=JobThrottle.RATE_LIMIT,
    )
    async def _restart_after_problem(
        self, state: ContainerState, exit_code: int | None = None
    ):
        """Restart unhealthy or failed plugin."""
        return await super()._restart_after_problem(state, exit_code)
