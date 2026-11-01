"""Bring a native Core back from the safe mode the watchdog put it in.

Safe mode lasts until Core next restarts, and a Core in safe mode answers like
a healthy one, so nothing would ever end it: custom integrations stay off after
a problem that has long gone. Core's notifications are kept in memory, so each
one lasts only as long as the Core it was sent to.

Each step is saved before it is taken: Core outlives a Supervisor restart, and
it can come up long after the watchdog stopped waiting for it. Core's pid and
what it reports about itself tell what actually happened.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass
from enum import StrEnum
import logging
from pathlib import Path
import time
from typing import TYPE_CHECKING

from ..coresys import CoreSysAttributes
from ..exceptions import HomeAssistantAPIError, JsonFileError
from ..homeassistant.const import SAFE_MODE_FILENAME
from ..utils.json import read_json_file, write_json_file

if TYPE_CHECKING:
    from ..coresys import CoreSys

_LOGGER: logging.Logger = logging.getLogger(__name__)

FIRST_WAIT = 30 * 60
MAX_WAIT = 24 * 60 * 60
STATE_FILE = "watchdog-safe-mode.json"
NOTIFICATION_ID = "supervisor_watchdog_safe_mode"
NOTIFICATION_TITLE = "Home Assistant Core safe mode"


class Phase(StrEnum):
    """Where the watchdog is with Core's safe mode."""

    ENTERING = "entering"
    IN_SAFE_MODE = "in_safe_mode"
    RETURNING = "returning"


class Action(StrEnum):
    """What the watchdog should do about it."""

    NORMAL_START = "normal_start"
    BACK_TO_SAFE_MODE = "back_to_safe_mode"


@dataclass
class _Run:
    """A safe mode run, as saved on disk."""

    phase: Phase
    pid: int | None  # ENTERING: the Core before it. Otherwise the safe mode Core.
    since: float | None  # When it first reached safe mode.
    retry_at: float
    wait: int


def _when(timestamp: float) -> str:
    return time.strftime("%a %H:%M", time.localtime(timestamp))


class NativeSafeMode(CoreSysAttributes):
    """The safe mode run the watchdog started, if any."""

    def __init__(self, coresys: CoreSys) -> None:
        """Initialize."""
        self.coresys: CoreSys = coresys
        self._run: _Run | None = None

    @property
    def _path(self) -> Path:
        return self.sys_config.path_supervisor / "core" / STATE_FILE

    @property
    def active(self) -> bool:
        """Return True if there is a run to look after."""
        return self._run is not None

    @property
    def returning(self) -> bool:
        """Return True if a normal start is under way: if Core is not up, it failed."""
        return self._run is not None and self._run.phase == Phase.RETURNING

    async def load(self) -> None:
        """Pick up a run from before a Supervisor restart."""
        if not await self.sys_run_in_executor(self._path.exists):
            return
        try:
            state = await self.sys_run_in_executor(read_json_file, self._path)
            self._run = _Run(
                phase=Phase(state["phase"]),
                pid=state["pid"],
                since=state["since"],
                retry_at=float(state["retry_at"]),
                wait=int(state["wait"]),
            )
        except (JsonFileError, KeyError, TypeError, ValueError) as err:
            _LOGGER.error("Dropping unreadable %s: %s", self._path, err)
            await self._clear()

    async def entering(self) -> None:
        """Save the intent, just before asking for safe mode."""
        run = self._run
        if run is None:
            wait = FIRST_WAIT
        elif run.phase == Phase.ENTERING:
            wait = run.wait
        else:
            wait = min(run.wait * 2, MAX_WAIT)
        self._run = _Run(
            Phase.ENTERING, await self._pid(), run.since if run else None, 0, wait
        )
        await self._save()

    async def remove_marker(self) -> None:
        """Remove a marker Core did not read, or a later restart would obey it."""
        marker = self.sys_config.path_homeassistant / SAFE_MODE_FILENAME
        try:
            await self.sys_run_in_executor(marker.unlink, True)
        except OSError as err:
            _LOGGER.error("Can't remove the safe mode marker: %s", err)

    async def check(self) -> Action | None:
        """Compare the run with the Core that is answering, on each healthy check."""
        assert self._run
        run = self._run
        pid = await self._pid()
        if pid is None:
            return None
        if (
            run.phase == Phase.IN_SAFE_MODE
            and pid == run.pid
            and time.time() < run.retry_at
        ):
            return None
        try:
            config = await self.sys_homeassistant.api.get_config()
        except HomeAssistantAPIError as err:
            _LOGGER.debug("Can't ask Core about safe mode, trying later: %s", err)
            return None
        safe_mode = bool(config.get("safe_mode"))
        recovery_mode = bool(config.get("recovery_mode"))

        if run.phase == Phase.ENTERING:
            if pid == run.pid:
                # A stop that failed: this Core never read the marker.
                await self.remove_marker()
            if safe_mode:
                await self._reached_safe_mode(pid, recovery_mode)
            elif recovery_mode and run.since is not None:
                # The way back from recovery mode failed; it isn't over.
                return Action.BACK_TO_SAFE_MODE
            else:
                _LOGGER.error("Home Assistant Core did not go into safe mode")
                await self.remove_marker()
                await self._ended(recovery_mode)
            return None

        if pid == run.pid:
            if safe_mode:
                # Due, or a return that never got as far as restarting Core.
                run.phase = Phase.IN_SAFE_MODE
                return Action.NORMAL_START
            await self._ended(recovery_mode)
            return None

        if run.phase == Phase.RETURNING:
            if recovery_mode:
                return Action.BACK_TO_SAFE_MODE
            if safe_mode:
                # A marker left behind by something else.
                run.wait = min(run.wait * 2, MAX_WAIT)
                await self._reached_safe_mode(pid, recovery_mode)
            else:
                await self._ended(recovery_mode)
            return None

        if safe_mode:
            _LOGGER.info("Core was restarted into safe mode by someone else")
            await self._clear()
        else:
            await self._ended(recovery_mode)
        return None

    async def same_core(self) -> bool:
        """Return True if Core is still the process this run is about."""
        assert self._run
        return await self._pid() == self._run.pid

    async def return_started(self) -> None:
        """Save that a normal start is about to be tried."""
        assert self._run
        self._run.phase = Phase.RETURNING
        await self._save()

    async def return_rejected(self) -> None:
        """Take back a normal start that never began, to try again later."""
        assert self._run
        self._run.phase = Phase.IN_SAFE_MODE
        await self._save()

    async def return_failed(self) -> None:
        """Handle a failed normal start where the safe mode Core is still there."""
        assert self._run
        if not await self.same_core():
            # Core went down. The next attempt goes back into safe mode.
            return
        self._run.wait = min(self._run.wait * 2, MAX_WAIT)
        await self.postpone(
            "Home Assistant Core stayed in safe mode, because it could not be "
            "restarted.",
            self._run.wait,
        )

    async def postpone(self, reason: str, wait: int = FIRST_WAIT) -> None:
        """Stay in safe mode for now, and say why."""
        assert self._run
        self._run.phase = Phase.IN_SAFE_MODE
        self._run.retry_at = time.time() + wait
        await self._save()
        await self._notify(
            f"{reason} Custom integrations and custom frontend pieces are off until "
            f"it restarts normally. A normal start will be tried at "
            f"{_when(self._run.retry_at)}."
        )

    async def _reached_safe_mode(self, pid: int, recovery_mode: bool) -> None:
        assert self._run
        run = self._run
        now = time.time()
        again = run.since is not None
        run.phase = Phase.IN_SAFE_MODE
        run.pid = pid
        run.since = run.since or now
        run.retry_at = now + run.wait
        await self._save()

        cause = (
            "A normal start was tried again and failed."
            if again
            else "Normal starts failed several times in a row."
        )
        state = (
            "is in recovery mode, so almost nothing is loaded, including Matter and "
            "the locks"
            if recovery_mode
            else "is running in safe mode, with custom integrations and custom "
            "frontend pieces off"
        )
        await self._notify(
            f"Home Assistant Core {state}. {cause} A normal start will be tried at "
            f"{_when(run.retry_at)}. The Supervisor and Core logs may show why it "
            "failed. To stay in safe mode for longer, restart Core into safe mode "
            "yourself."
        )

    async def _ended(self, recovery_mode: bool) -> None:
        """Forget the run, and say where Core is if the user was told about it."""
        assert self._run
        since = self._run.since
        await self._clear()
        if recovery_mode:
            await self._notify(
                "Home Assistant Core is in recovery mode, so almost nothing is "
                "loaded, including Matter and the locks. The watchdog will not "
                "restart it. Check its configuration and log, then restart it."
            )
        elif since is not None:
            await self._notify(
                "Home Assistant Core is running normally again, with custom "
                f"integrations back on. It had been in safe mode since {_when(since)} "
                "because normal starts kept failing."
            )

    async def _pid(self) -> int | None:
        agent = self.sys_homeassistant.core.instance.agent  # type: ignore[attr-defined]
        return (await agent.status()).pid

    async def _save(self) -> None:
        assert self._run
        try:
            await self.sys_run_in_executor(
                write_json_file, self._path, asdict(self._run)
            )
        except JsonFileError as err:
            # The run carries on from memory, until a Supervisor restart forgets it.
            _LOGGER.error("Can't save the safe mode state: %s", err)

    async def _clear(self) -> None:
        self._run = None
        try:
            await self.sys_run_in_executor(self._path.unlink, True)
        except OSError as err:
            _LOGGER.error("Can't remove %s: %s", self._path, err)

    async def _notify(self, message: str) -> None:
        """Show a message in Core. Failing to must never get in the way."""
        try:
            async with self.sys_homeassistant.api.make_request(
                "post",
                "api/services/persistent_notification/create",
                json={
                    "notification_id": NOTIFICATION_ID,
                    "title": NOTIFICATION_TITLE,
                    "message": message,
                },
            ) as resp:
                if resp.status != 200:
                    _LOGGER.warning("Core refused a notification: %s", resp.status)
        except HomeAssistantAPIError as err:
            _LOGGER.warning("Can't send Core a notification: %s", err)
