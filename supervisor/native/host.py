"""Host information on macOS, which has no systemd-hostnamed or timedated.

Read-only: changing the hostname or time zone needs root, so the matching
host features stay off and those settings stay in System Settings.
"""

from __future__ import annotations

from datetime import tzinfo
import logging
from pathlib import Path
import platform
import subprocess
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from ..host.info import InfoCenter
from .hardware import boot_time

_LOGGER: logging.Logger = logging.getLogger(__name__)

_LOCALTIME = Path("/etc/localtime")
_ZONEINFO_MARKER = "zoneinfo/"


def _run(*args: str) -> str | None:
    try:
        return subprocess.run(
            args, capture_output=True, text=True, check=True, timeout=10
        ).stdout.strip()
    except OSError, subprocess.SubprocessError:
        return None


def read_host_facts() -> dict[str, str | int | None]:
    """Gather what the host page shows, from tools any user may run."""
    timezone = None
    try:
        target = str(_LOCALTIME.readlink())
        if _ZONEINFO_MARKER in target:
            timezone = target.split(_ZONEINFO_MARKER, 1)[1]
    except OSError:
        pass
    version = _run("/usr/bin/sw_vers", "-productVersion")
    battery = _run("/usr/bin/pmset", "-g", "batt") or ""
    boot = boot_time()
    return {
        "hostname": _run("/usr/sbin/scutil", "--get", "LocalHostName"),
        "operating_system": f"macOS {version}" if version else None,
        "kernel": platform.release() or None,
        "chassis": "laptop" if "InternalBattery" in battery else "desktop",
        "timezone": timezone,
        # systemd reports this in microseconds since the epoch
        "boot_timestamp": boot * 1_000_000 if boot is not None else None,
    }


def _zone(name: str | None) -> tzinfo | None:
    if not name:
        return None
    try:
        return ZoneInfo(name)
    except ZoneInfoNotFoundError, ValueError:
        return None


class NativeInfoCenter(InfoCenter):
    """InfoCenter fed from macOS instead of D-Bus."""

    def __init__(self, coresys) -> None:
        """Initialize."""
        super().__init__(coresys)
        self._facts: dict[str, str | int | None] = {}
        self._tzinfo: tzinfo | None = None

    async def update(self) -> None:
        """Read the host facts again."""
        _LOGGER.info("Updating local host information")
        self._facts = await self.sys_run_in_executor(read_host_facts)
        # Loading a zone reads a file, so not in the event loop
        self._tzinfo = await self.sys_run_in_executor(_zone, self.timezone)

    def _fact(self, key: str):
        return self._facts.get(key)

    @property
    def hostname(self) -> str | None:
        """Return the local (Bonjour) hostname."""
        return self._fact("hostname")

    @property
    def llmnr_hostname(self) -> str | None:
        """MacOS doesn't answer LLMNR."""
        return None

    @property
    def broadcast_llmnr(self) -> bool | None:
        """MacOS doesn't answer LLMNR."""
        return False

    @property
    def broadcast_mdns(self) -> bool | None:
        """MDNSResponder always advertises the host."""
        return True

    @property
    def chassis(self) -> str | None:
        """Return desktop or laptop."""
        return self._fact("chassis")

    @property
    def deployment(self) -> str | None:
        """No deployment type on macOS."""
        return None

    @property
    def kernel(self) -> str | None:
        """Return the Darwin kernel version."""
        return self._fact("kernel")

    @property
    def operating_system(self) -> str | None:
        """Return the macOS version."""
        return self._fact("operating_system")

    @property
    def cpe(self) -> str | None:
        """No CPE on macOS."""
        return None

    @property
    def timezone(self) -> str | None:
        """Return the system time zone."""
        return self._fact("timezone")

    @property
    def timezone_tzinfo(self) -> tzinfo | None:
        """Return the system time zone as tzinfo."""
        return self._tzinfo

    @property
    def use_rtc(self) -> bool | None:
        """Not known without root."""
        return None

    @property
    def use_ntp(self) -> bool | None:
        """Not readable without root on macOS."""
        return None

    @property
    def dt_synchronized(self) -> bool | None:
        """Not readable without root on macOS."""
        return None

    @property
    def startup_time(self) -> float | None:
        """MacOS doesn't report how long booting took."""
        return None

    @property
    def boot_timestamp(self) -> int | None:
        """Return when the host booted, in microseconds since the epoch."""
        return self._fact("boot_timestamp")

    @property
    def virtualization(self) -> str | None:
        """A native install runs on the Mac itself."""
        return None
