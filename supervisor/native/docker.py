"""Stand-in for the Docker API on a native (macOS) host.

There is no Docker engine on a native host. Home Assistant Core, the
Supervisor and apps run as launchd services, so this object only provides the
pieces of DockerAPI that the rest of Supervisor reads: engine info, the
"network" addresses (everything is on localhost) and the registry config.
Image and container operations are not available and raise DockerError.
"""

from __future__ import annotations

from dataclasses import dataclass
from ipaddress import IPv4Address
import logging
from typing import TYPE_CHECKING, Any, Self

from awesomeversion import AwesomeVersion

from ..docker.manager import DockerConfig, DockerInfo
from ..exceptions import DockerError

if TYPE_CHECKING:
    from ..coresys import CoreSys

_LOGGER: logging.Logger = logging.getLogger(__name__)

LOCALHOST = IPv4Address("127.0.0.1")
NATIVE = "native"


@dataclass(slots=True, frozen=True)
class NativeInfo(DockerInfo):
    """Engine info of a native host."""

    @property
    def supported_version(self) -> bool:
        """There is no engine version to check."""
        return True


class NativeNetwork:
    """All services of a native host share the host network."""

    supervisor = LOCALHOST
    gateway = LOCALHOST
    dns = LOCALHOST
    audio = LOCALHOST
    cli = LOCALHOST
    observer = LOCALHOST

    @property
    def containers(self) -> list[str]:
        """No containers are attached."""
        return []

    async def attach_container(self, *args: Any, **kwargs: Any) -> None:
        """Nothing to attach on a native host."""


class _Unavailable:
    """Image or container API, which a native host does not have."""

    def __init__(self, what: str) -> None:
        self._what = what

    def __getattr__(self, name: str) -> Any:
        raise DockerError(
            f"Docker {self._what}.{name} is not available on a native host",
            _LOGGER.debug,
        )


class NativeMonitor:
    """No container events to watch."""

    async def load(self) -> None:
        """Nothing to start."""

    async def unload(self) -> None:
        """Nothing to stop."""


class NativeDockerAPI:
    """DockerAPI replacement for a native host."""

    def __init__(self, coresys: CoreSys) -> None:
        """Initialize."""
        self.coresys = coresys
        self.config: DockerConfig = DockerConfig()
        self.network = NativeNetwork()
        self.info = NativeInfo(
            version=AwesomeVersion(NATIVE),
            storage=NATIVE,
            logging=NATIVE,
            cgroup=NATIVE,
            support_cpu_realtime=False,
        )
        self.monitor = NativeMonitor()
        self.images = _Unavailable("images")
        self.containers = _Unavailable("containers")

    async def post_init(self) -> Self:
        """Read the registry config."""
        await self.config.read_data()
        return self

    async def load(self) -> None:
        """Nothing to start."""

    async def unload(self) -> None:
        """Nothing to stop."""

    def __getattr__(self, name: str) -> Any:
        """Any other Docker operation is unavailable."""
        raise DockerError(
            f"Docker operation {name} is not available on a native host",
            _LOGGER.debug,
        )
