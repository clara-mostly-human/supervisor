"""Bring a native Core back from safe mode or recovery mode.

Safe mode lasts until Core next restarts, and a Core in safe mode answers like
a healthy one, so nothing would ever end it: custom integrations stay off after
a problem that has long gone. Recovery mode, which Core enters by itself, answers
the same way with almost nothing loaded. Core's notifications are kept in memory,
so each one lasts only as long as the Core it was sent to.

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
RECOVERY_WAIT = 15 * 60
STATE_FILE = "watchdog-safe-mode.json"
NOTIFICATION_ID = "supervisor_watchdog_safe_mode"
NOTIFICATION_TITLE = "Home Assistant Core watchdog"


class Phase(StrEnum):
    """Where the watchdog is with Core's safe mode or recovery mode."""

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
    since: float | None  # When it first stopped running normally.
    retry_at: float
    wait: int  # Before a return from safe mode. 0: the next one is the first.
    recovery: bool = False  # In recovery mode, whether or not in safe mode too.


def _when(timestamp: float) -> str:
    return time.strftime("%a %H:%M", time.localtime(timestamp))


def _longer(wait: int) -> int:
    return min(max(wait * 2, FIRST_WAIT), MAX_WAIT)


class NativeSafeMode(CoreSysAttributes):
    """The run the watchdog is looking after, if any."""

    def __init__(self, coresys: CoreSys) -> None:
        """Initialize."""
        self.coresys: CoreSys = coresys
        self._run: _Run | None = None
        self._seen_pid: int | None = None

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

    @property
    def in_recovery(self) -> bool:
        """Return True if the run's Core is in recovery mode: little to lose."""
        return self._run is not None and self._run.recovery

    @property
    def waiting(self) -> bool:
        """Return True if a safe start has settled, with Core up and known."""
        return self._run is not None and self._run.phase == Phase.IN_SAFE_MODE

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
                recovery=bool(state.get("recovery", False)),
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
            wait = _longer(run.wait)
        self._run = _Run(
            Phase.ENTERING,
            await self._pid(),
            run.since if run else None,
            0,
            wait,
            run.recovery if run else False,
        )
        await self._save()

    async def remove_marker(self) -> None:
        """Remove a marker Core did not read, or a later restart would obey it."""
        marker = self.sys_config.path_homeassistant / SAFE_MODE_FILENAME
        try:
            await self.sys_run_in_executor(marker.unlink, True)
        except OSError as err:
            _LOGGER.error("Can't remove the safe mode marker: %s", err)

    async def notice_recovery(self) -> None:
        """Start a run if Core came up in recovery mode, asking once per process."""
        pid = await self._pid()
        if pid is None or pid == self._seen_pid:
            return
        try:
            config = await self.sys_homeassistant.api.get_config()
        except HomeAssistantAPIError as err:
            _LOGGER.debug("Can't ask Core about recovery mode, trying later: %s", err)
            return
        # A Core migrating its database looks healthy, and can still fall into it.
        if config.get("state") != "RUNNING":
            return
        self._seen_pid = pid
        if config.get("recovery_mode"):
            _LOGGER.error("Home Assistant Core is in recovery mode")
            self._run = _Run(Phase.IN_SAFE_MODE, pid, time.time(), 0, 0, True)
            await self._save()

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
        # A Core migrating its database looks healthy, and hasn't reached a mode.
        if config.get("state") != "RUNNING":
            return None
        safe_mode = bool(config.get("safe_mode"))
        recovery_mode = bool(config.get("recovery_mode"))
        run.recovery = recovery_mode

        if run.phase == Phase.ENTERING:
            if pid == run.pid:
                # A stop that failed: this Core never read the marker.
                await self.remove_marker()
            if safe_mode or (recovery_mode and pid != run.pid):
                # In recovery with no safe mode: the safe start crashed, and this
                # is a later normal start. Waits, or this would be a restart loop.
                await self._reached_safe_mode(pid, recovery_mode)
            elif recovery_mode:
                # The safe start never began.
                return Action.BACK_TO_SAFE_MODE
            else:
                _LOGGER.error("Home Assistant Core did not go into safe mode")
                await self.remove_marker()
                await self._ended()
            return None

        if pid == run.pid:
            if safe_mode or recovery_mode:
                # Due, or a return that never got as far as restarting Core.
                run.phase = Phase.IN_SAFE_MODE
                return Action.NORMAL_START
            await self._ended()
            return None

        if run.phase == Phase.RETURNING:
            if recovery_mode:
                return Action.BACK_TO_SAFE_MODE
            if safe_mode:
                # A marker left behind by something else.
                run.wait = _longer(run.wait)
                await self._reached_safe_mode(pid, recovery_mode)
            else:
                await self._ended()
            return None

        if recovery_mode:
            await self._reached_safe_mode(pid, recovery_mode)
        elif safe_mode:
            _LOGGER.info("Core was restarted into safe mode by someone else")
            await self._clear()
        else:
            await self._ended()
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
        if not self._run.recovery:
            self._run.wait = _longer(self._run.wait)
        await self.postpone("it could not be restarted", self._run.wait)

    async def postpone(self, because: str, wait: int = FIRST_WAIT) -> None:
        """Stay where Core is for now, and say why."""
        assert self._run
        run = self._run
        run.phase = Phase.IN_SAFE_MODE
        run.retry_at = time.time() + (RECOVERY_WAIT if run.recovery else wait)
        await self._save()
        if run.recovery:
            stayed = (
                "recovery mode",
                "Almost nothing is loaded, including Matter and the locks,",
            )
        else:
            stayed = (
                "safe mode",
                "Custom integrations and custom frontend pieces are off",
            )
        await self._notify(
            f"Home Assistant Core is staying in {stayed[0]}, because {because}. "
            f"{stayed[1]} until it restarts normally. The next try is at "
            f"{_when(run.retry_at)}."
        )

    async def _reached_safe_mode(self, pid: int, recovery_mode: bool) -> None:
        assert self._run
        run = self._run
        now = time.time()
        again = run.since is not None
        run.phase = Phase.IN_SAFE_MODE
        run.pid = pid
        run.since = run.since or now
        run.recovery = recovery_mode
        if recovery_mode:
            # Doubling protects a mostly working house. There isn't one here.
            run.wait = 0
            run.retry_at = now + RECOVERY_WAIT
        else:
            run.retry_at = now + run.wait
        await self._save()

        cause = (
            "A normal start was tried again and failed."
            if again
            else "Normal starts failed several times in a row."
        )
        if recovery_mode:
            state = (
                "is in recovery mode, so almost nothing is loaded, including Matter "
                "and the locks"
            )
            stop = "To stop these restarts, turn off Core's watchdog."
        else:
            state = (
                "is running in safe mode, with custom integrations and custom "
                "frontend pieces off"
            )
            stop = (
                "To stay in safe mode for longer, restart Core into safe mode yourself."
            )
        await self._notify(
            f"Home Assistant Core {state}. {cause} A normal start will be tried at "
            f"{_when(run.retry_at)}. The Supervisor and Core logs may show why it "
            f"failed. {stop}"
        )

    async def _ended(self) -> None:
        """Forget the run, and say so if there was anything to say."""
        assert self._run
        since = self._run.since
        await self._clear()
        if since is not None:
            await self._notify(
                "Home Assistant Core is running normally again, with custom "
                f"integrations back on. It had not been running normally since "
                f"{_when(since)}."
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
