"""The Supervisor's view of itself on a native (macOS) host.

In a container install the Supervisor inspects its own container for its
image, IP address and mounts. Natively it is a launchd service on the host.
"""

from __future__ import annotations

from ipaddress import IPv4Address
import os
from typing import TYPE_CHECKING, Any

from awesomeversion import AwesomeVersion

from ..const import SUPERVISOR_VERSION, CpuArch
from ..docker.const import ContainerState
from ..docker.stats import DockerStats
from ..exceptions import DockerError
from .docker import LOCALHOST
from .stats import process_stats

if TYPE_CHECKING:
    from ..coresys import CoreSys


class NativeSupervisorInstance:
    """Replacement for DockerSupervisor on a native host."""

    name = "supervisor"
    image: str | None = None
    arch = CpuArch.MACOS_AARCH64.value
    attached = True
    in_progress = False
    privileged = True
    host_mounts_available = False
    meta_config: dict[str, Any] = {}
    meta_host: dict[str, Any] = {}
    restart_policy = None

    def __init__(self, coresys: CoreSys) -> None:
        """Initialize."""
        self.coresys = coresys

    @property
    def version(self) -> AwesomeVersion:
        """Return the running Supervisor version."""
        return AwesomeVersion(SUPERVISOR_VERSION)

    @property
    def ip_address(self) -> IPv4Address:
        """The Supervisor listens on localhost."""
        return LOCALHOST

    async def attach(self, *args: Any, **kwargs: Any) -> None:
        """Nothing to attach to."""

    async def cleanup(self, *args: Any, **kwargs: Any) -> None:
        """No images to clean up."""

    async def exists(self) -> bool:
        """Return True: the running Supervisor exists."""
        return True

    async def is_running(self) -> bool:
        """Return True: the Supervisor is running."""
        return True

    async def current_state(self) -> ContainerState:
        """Return the running state."""
        return ContainerState.RUNNING

    async def logs(self) -> list[str]:
        """Return no logs; they are in the host log, not a container."""
        return []

    async def stats(self, *, one_shot: bool = False) -> DockerStats:
        """Return the Supervisor's own CPU and memory use."""
        if (result := await process_stats(os.getpid(), one_shot=one_shot)) is None:
            raise DockerError("Can't read the Supervisor's own process")
        return result

    async def install(self, *args: Any, **kwargs: Any) -> None:
        """Self-update is not implemented yet on a native host."""
        raise DockerError("Supervisor update is not available on a native host yet")

    async def update_start_tag(self, *args: Any, **kwargs: Any) -> None:
        """No start tag on a native host."""

    async def retag(self, *args: Any, **kwargs: Any) -> None:
        """No image tags on a native host."""
