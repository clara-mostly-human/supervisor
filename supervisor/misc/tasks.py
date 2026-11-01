"""A collection of tasks."""

import asyncio
from datetime import datetime, timedelta
import logging
import time
from typing import cast

from ..apps.const import APP_UPDATE_CONDITIONS
from ..backups.const import LOCATION_CLOUD_BACKUP, LOCATION_TYPE
from ..const import ATTR_APP, ATTR_BACKUP, ATTR_TYPE, AppState, CoreState
from ..coresys import CoreSysAttributes
from ..exceptions import (
    AppsError,
    BackupFileNotFoundError,
    HomeAssistantAPIError,
    HomeAssistantError,
    HomeAssistantJobError,
    HomeAssistantWSError,
    JobException,
    ObserverError,
)
from ..homeassistant.const import LANDINGPAGE, WSType
from ..jobs.decorator import Job, JobCondition
from ..native import IS_MACOS
from ..native.safe_mode import Action, NativeSafeMode
from ..plugins.const import PLUGIN_UPDATE_CONDITIONS
from ..utils.dt import utcnow
from ..utils.sentry import async_capture_exception

_LOGGER: logging.Logger = logging.getLogger(__name__)

HASS_WATCHDOG_API_FAILURES = "HASS_WATCHDOG_API_FAILURES"
HASS_WATCHDOG_REANIMATE_FAILURES = "HASS_WATCHDOG_REANIMATE_FAILURES"
HASS_WATCHDOG_MAX_API_ATTEMPTS = 2
HASS_WATCHDOG_MAX_REANIMATE_ATTEMPTS = 5
HASS_WATCHDOG_NOT_RUNNING_SINCE = "HASS_WATCHDOG_NOT_RUNNING_SINCE"
HASS_WATCHDOG_NEXT_ATTEMPT = "HASS_WATCHDOG_NEXT_ATTEMPT"
# Natively nothing else watches Core, so check often. A Core that answers but
# isn't running is starting or stopping, which it gets longer for than a Core
# that doesn't answer at all, and failed attempts stay spaced out. After a
# restart of a Core that didn't answer the watchdog goes back to the slower pace
# for a while: on a starved host a restart doesn't help, and repeats would abort
# Core over and over.
HASS_WATCHDOG_NOT_RUNNING_GRACE = 150
HASS_WATCHDOG_RETRY_SECONDS = 120
HASS_WATCHDOG_LAST_HUNG_RESTART = "HASS_WATCHDOG_LAST_HUNG_RESTART"
HASS_WATCHDOG_RECENT_HUNG_SECONDS = 900
HASS_WATCHDOG_MAX_API_ATTEMPTS_AFTER_HUNG = 4
HASS_WATCHDOG_CONFIG_CHECK_SECONDS = 120
CONFIG_UNCHECKED = "its configuration could not be checked"
CONFIG_INVALID = "its configuration does not pass the check, see the Supervisor log"

RUN_UPDATE_APPS = 57600
RUN_UPDATE_CLI = 43200  # 12h, staggered +2min per plugin
RUN_UPDATE_DNS = 43320
RUN_UPDATE_AUDIO = 43440
RUN_UPDATE_MULTICAST = 43560
RUN_UPDATE_OBSERVER = 43680

RUN_RELOAD_APPS = 10800
RUN_RELOAD_BACKUPS = 72000
RUN_RELOAD_MOUNTS = 900
RUN_RELOAD_HOST = 7600
RUN_RELOAD_UPDATER = 86400  # 24h
RUN_RELOAD_INGRESS = 930

RUN_WATCHDOG_HOMEASSISTANT_API = 15 if IS_MACOS else 120

RUN_WATCHDOG_APP_APPLICATION = 120
RUN_WATCHDOG_OBSERVER_APPLICATION = 180

RUN_CORE_BACKUP_CLEANUP = 86200

PLUGIN_AUTO_UPDATE_CONDITIONS = PLUGIN_UPDATE_CONDITIONS + [
    JobCondition.AUTO_UPDATE,
    JobCondition.RUNNING,
]

OLD_BACKUP_THRESHOLD = timedelta(days=2)


class Tasks(CoreSysAttributes):
    """Handle Tasks inside Supervisor."""

    def __init__(self, coresys):
        """Initialize Tasks."""
        self.coresys = coresys
        self._cache = {}
        self._safe_mode = NativeSafeMode(coresys)

    async def load(self):
        """Add Tasks to scheduler."""
        if IS_MACOS:
            await self._safe_mode.load()

        # Update
        self.sys_scheduler.register_task(self._update_apps, RUN_UPDATE_APPS)
        self.sys_scheduler.register_task(self._update_cli, RUN_UPDATE_CLI)
        self.sys_scheduler.register_task(self._update_dns, RUN_UPDATE_DNS)
        self.sys_scheduler.register_task(self._update_audio, RUN_UPDATE_AUDIO)
        self.sys_scheduler.register_task(self._update_multicast, RUN_UPDATE_MULTICAST)
        self.sys_scheduler.register_task(self._update_observer, RUN_UPDATE_OBSERVER)

        # Reload
        self.sys_scheduler.register_task(self._reload_store, RUN_RELOAD_APPS)
        self.sys_scheduler.register_task(self.sys_updater.reload, RUN_RELOAD_UPDATER)
        self.sys_scheduler.register_task(self.sys_backups.reload, RUN_RELOAD_BACKUPS)
        self.sys_scheduler.register_task(self.sys_host.reload, RUN_RELOAD_HOST)
        self.sys_scheduler.register_task(self.sys_mounts.reload, RUN_RELOAD_MOUNTS)
        self.sys_scheduler.register_task(self.sys_ingress.reload, RUN_RELOAD_INGRESS)

        # Watchdog
        self.sys_scheduler.register_task(
            self._watchdog_homeassistant_api, RUN_WATCHDOG_HOMEASSISTANT_API
        )
        self.sys_scheduler.register_task(
            self._watchdog_observer_application, RUN_WATCHDOG_OBSERVER_APPLICATION
        )
        self.sys_scheduler.register_task(
            self._watchdog_app_application, RUN_WATCHDOG_APP_APPLICATION
        )

        # Cleanup
        self.sys_scheduler.register_task(
            self._core_backup_cleanup, RUN_CORE_BACKUP_CLEANUP
        )

        _LOGGER.info("All core tasks are scheduled")

    @Job(
        name="tasks_update_apps",
        conditions=APP_UPDATE_CONDITIONS + [JobCondition.RUNNING],
        internal=True,
    )
    async def _update_apps(self):
        """Check if an update is available for an App and update it."""
        for app in self.sys_apps.all:
            if not app.is_installed or not app.auto_update:
                continue

            # Evaluate available updates
            if not app.need_update:
                continue
            if not app.auto_update_available:
                _LOGGER.debug(
                    "Not updating app %s from %s to %s as that would cross a known breaking version",
                    app.slug,
                    app.version,
                    app.latest_version,
                )
                continue
            # Delay auto-updates for a day in case of issues
            if utcnow() < app.latest_version_timestamp + timedelta(days=1):
                _LOGGER.debug(
                    "Not updating app %s from %s to %s as the latest version is less than a day old",
                    app.slug,
                    app.version,
                    app.latest_version,
                )
                continue
            if not app.test_update_schema():
                _LOGGER.warning("App %s will be ignored, schema tests failed", app.slug)
                continue

            _LOGGER.info("App auto update process %s", app.slug)
            # Call Home Assistant Core to update app to make sure that backups
            # get created through the Home Assistant Core API (categorized correctly).
            # Ultimately auto updates should be handled by Home Assistant Core itself
            # through a update entity feature.
            message = {
                ATTR_TYPE: WSType.HASSIO_UPDATE_APP,
                ATTR_APP: app.slug,
                ATTR_BACKUP: True,
            }
            _LOGGER.debug(
                "Sending update app WebSocket command to Home Assistant Core: %s",
                message,
            )
            try:
                await self.sys_homeassistant.websocket.async_send_command(message)
            except HomeAssistantWSError as err:
                _LOGGER.warning(
                    "Could not send app update command to Home Assistant Core: %s",
                    err,
                )

    async def _prepare_native_restart(self, no_answer: bool, recent_hung: bool) -> None:
        """Save the stacks of a Core about to be restarted and choose how it stops."""
        instance = self.sys_homeassistant.core.instance
        await instance.capture_stacks()  # type: ignore[attr-defined]
        if no_answer:
            instance.hung = not recent_hung  # type: ignore[attr-defined]

    def _finish_native_restart(self, no_answer: bool) -> None:
        """Clear the short stop and, for a Core that didn't answer, slow down."""
        self.sys_homeassistant.core.instance.hung = False  # type: ignore[attr-defined]
        if no_answer:
            # From the end: a slow restart must not use up the slower pace.
            self._cache[HASS_WATCHDOG_LAST_HUNG_RESTART] = time.monotonic()

    @staticmethod
    def _is_safe_mode_attempt(reanimate_fails: int) -> bool:
        """Return True if this attempt should go into safe mode."""
        if IS_MACOS:
            reanimate_fails %= HASS_WATCHDOG_MAX_REANIMATE_ATTEMPTS + 1
        return reanimate_fails == HASS_WATCHDOG_MAX_REANIMATE_ATTEMPTS

    @staticmethod
    def _log_restart_reason(down: bool, api_fails: int) -> None:
        """Log why the watchdog is about to act on Core."""
        if down:
            _LOGGER.error(
                "Watchdog found Home Assistant Core not running. Starting it!"
            )
        else:
            _LOGGER.error(
                "Watchdog missed %s Home Assistant Core API responses in a row. Restarting Home Assistant Core!",
                api_fails,
            )

    async def _reanimate_failed(
        self,
        err: HomeAssistantError | JobException,
        safe_mode: bool,
        reanimate_fails: int,
    ) -> None:
        """Count a failed attempt at bringing Core back."""
        if reanimate_fails == 0 or safe_mode:
            await async_capture_exception(err)

        if safe_mode:
            _LOGGER.critical(
                "Safe mode restart failed. Watchdog cannot bring Home Assistant online."
            )
        else:
            _LOGGER.error("Home Assistant watchdog reanimation failed!")

        # A rejected attempt touched nothing, and the marker may be another job's.
        if IS_MACOS and safe_mode and not isinstance(err, HomeAssistantJobError):
            await self._safe_mode.remove_marker()

        self._cache[HASS_WATCHDOG_REANIMATE_FAILURES] = reanimate_fails + 1
        self._cache[HASS_WATCHDOG_NEXT_ATTEMPT] = (
            time.monotonic() + HASS_WATCHDOG_RETRY_SECONDS
        )

    async def _enter_safe_mode(self) -> None:
        """Restart Core into safe mode."""
        if IS_MACOS:
            await self._safe_mode.entering()
        await self.sys_homeassistant.core.rebuild(safe_mode=True)
        if IS_MACOS and await self._safe_mode.check() == Action.BACK_TO_SAFE_MODE:
            # Core survived the stop, so nothing happened. Don't retry on every check.
            self._cache[HASS_WATCHDOG_NEXT_ATTEMPT] = (
                time.monotonic() + HASS_WATCHDOG_RETRY_SECONDS
            )

    async def _back_to_safe_mode(self) -> None:
        """Try safe mode for a Core in recovery mode, unless a backup just started."""
        if self.sys_core.state != CoreState.RUNNING:
            return
        _LOGGER.error(
            "Home Assistant Core came up in recovery mode, going to safe mode"
        )
        try:
            await self._enter_safe_mode()
        except (HomeAssistantError, JobException) as err:
            await self._reanimate_failed(
                err, True, HASS_WATCHDOG_MAX_REANIMATE_ATTEMPTS
            )
            # Rejected: another job just started. Otherwise don't repeat it every
            # 2 minutes in silence, if Core is still up.
            if not isinstance(err, HomeAssistantJobError):
                await self._safe_mode.return_failed()

    async def _config_problem(self) -> str | None:
        """Return what fails Core's config check, if anything.

        The check sees custom integrations even when safe mode would hide them, so
        failing it doesn't prove a safe start can't get past the problem.
        """
        try:
            async with asyncio.timeout(HASS_WATCHDOG_CONFIG_CHECK_SECONDS):
                result = await self.sys_homeassistant.core.check_config()
        except (HomeAssistantError, OSError) as err:
            _LOGGER.error("Can't check Home Assistant Core's config: %s", err)
            return CONFIG_UNCHECKED
        if result.valid:
            return None
        _LOGGER.error("Home Assistant Core's config check failed:\n%s", result.log)
        return CONFIG_INVALID

    async def _hold_in_recovery(self, problem: str) -> None:
        """Try safe mode instead of a normal start, and say why.

        Failing the check is no proof: it can fail for something a start gets past,
        and the problem can be repaired, so this repeats at each look. It costs little,
        as with bad YAML Core stops right after reading it.
        """
        await self._back_to_safe_mode()
        if self._safe_mode.waiting:
            await self._safe_mode.postpone(problem)

    async def _try_normal_start(self) -> None:
        """Give a Core in safe mode or recovery mode another normal start."""
        # This restarts a healthy Core on purpose. Core refuses to restart during a
        # database migration or on a bad config, which would land it in recovery mode.
        try:
            state = await self.sys_homeassistant.api.get_core_state()
        except HomeAssistantAPIError as err:
            _LOGGER.debug("Can't ask Core for its state, trying later: %s", err)
            return
        if state.get("state") != "RUNNING" or state.get("recorder_state", {}).get(
            "migration_in_progress"
        ):
            return
        problem = await self._config_problem()
        # Someone may have stopped or restarted Core during the check.
        if not await self._safe_mode.same_core():
            return
        # A backup could have started during the check.
        if self.sys_core.state != CoreState.RUNNING:
            return
        if self._safe_mode.in_recovery:
            if problem == CONFIG_INVALID:
                await self._hold_in_recovery(problem)
                return
            # A check that can't run mustn't keep Core here for good: at worst the
            # restart lands in recovery mode again. In safe mode it would cost more.
        elif problem:
            await self._safe_mode.postpone(problem)
            return
        await self._safe_mode.return_started()

        _LOGGER.warning("Watchdog is trying Home Assistant Core without safe mode")
        try:
            await self.sys_homeassistant.core.restart()
        except HomeAssistantJobError:
            # Another Core job just started; not a verdict on Core.
            await self._safe_mode.return_rejected()
            return
        except HomeAssistantError, JobException:
            _LOGGER.error("Home Assistant Core failed to start without safe mode")
            self._cache[HASS_WATCHDOG_NEXT_ATTEMPT] = (
                time.monotonic() + HASS_WATCHDOG_RETRY_SECONDS
            )
            await self._safe_mode.return_failed()
            return
        # If a backup started meanwhile, the next healthy check sorts this out.
        if (
            self.sys_core.state == CoreState.RUNNING
            and await self._safe_mode.check() == Action.BACK_TO_SAFE_MODE
        ):
            await self._back_to_safe_mode()

    async def _check_safe_mode(self) -> None:
        """Look after a safe mode or recovery mode run, while Core looks healthy."""
        if self.sys_core.state != CoreState.RUNNING:
            return
        if not self._safe_mode.active:
            await self._safe_mode.notice_recovery()
        if not self._safe_mode.active:
            return
        action = await self._safe_mode.check()
        if action == Action.NORMAL_START:
            await self._try_normal_start()
        elif action == Action.BACK_TO_SAFE_MODE:
            await self._back_to_safe_mode()

    async def _watchdog_homeassistant_api(self):
        """Create scheduler task for monitoring running state of API.

        Try 2 times to call API before we restart Home-Assistant. Maybe we had
        a delay in our system.
        """
        if not self.sys_homeassistant.watchdog:
            # Watchdog is not enabled for Home Assistant
            return
        # Natively nothing else brings Core back (launchd's KeepAlive is off and
        # a failed restart leaves error_state set), so the watchdog only stands
        # aside for a running job or a stop someone asked for, and keeps trying.
        if self.sys_homeassistant.error_state and not IS_MACOS:
            # Home Assistant is in an error state, this is handled by the rollback feature
            return
        if self.sys_homeassistant.version == LANDINGPAGE:
            # Skip watchdog for landingpage
            return
        core = self.sys_homeassistant.core
        down = not await core.is_running()
        if down and (
            not IS_MACOS
            or core.instance.stop_requested  # type: ignore[attr-defined]
            or self.sys_core.state != CoreState.RUNNING
        ):
            # The home assistant container is not running
            return
        if core.in_progress:
            # Home Assistant has a task in progress
            return
        if (
            IS_MACOS
            and self._cache.get(HASS_WATCHDOG_NEXT_ATTEMPT, 0) > time.monotonic()
        ):
            return
        no_answer = False
        not_running_too_long = False
        recent_hung = False
        api_fails = 0
        if not down:
            if IS_MACOS:
                state = await self.sys_homeassistant.api.get_api_state()
                no_answer = state is None
                running = state is not None and (
                    state.core_state == "RUNNING" or state.offline_db_migration
                )
                if state is not None and not running:
                    self._cache[HASS_WATCHDOG_API_FAILURES] = 0
                    since = self._cache.setdefault(
                        HASS_WATCHDOG_NOT_RUNNING_SINCE, time.monotonic()
                    )
                    if time.monotonic() - since < HASS_WATCHDOG_NOT_RUNNING_GRACE:
                        return
                    not_running_too_long = True
            else:
                running = await self.sys_homeassistant.api.check_api_state()
            if running:
                # Home Assistant is running properly
                self._cache[HASS_WATCHDOG_REANIMATE_FAILURES] = 0
                self._cache[HASS_WATCHDOG_API_FAILURES] = 0
                self._cache.pop(HASS_WATCHDOG_NOT_RUNNING_SINCE, None)
                if IS_MACOS:
                    await self._check_safe_mode()
                return

            # Init cache data
            api_fails = self._cache.get(HASS_WATCHDOG_API_FAILURES, 0)

            # Look like we run into a problem
            api_fails += 1
            last_hung = self._cache.get(HASS_WATCHDOG_LAST_HUNG_RESTART)
            recent_hung = (
                last_hung is not None
                and time.monotonic() - last_hung < HASS_WATCHDOG_RECENT_HUNG_SECONDS
            )
            max_attempts = (
                HASS_WATCHDOG_MAX_API_ATTEMPTS_AFTER_HUNG
                if recent_hung
                else HASS_WATCHDOG_MAX_API_ATTEMPTS
            )
            if api_fails < max_attempts and not not_running_too_long:
                self._cache[HASS_WATCHDOG_API_FAILURES] = api_fails
                _LOGGER.warning("Watchdog missed an Home Assistant Core API response.")
                return

        # After 5 reanimation attempts switch to safe mode. If that fails, give up
        # (natively: keep trying, with safe mode again after every 5 normal attempts)
        reanimate_fails = self._cache.get(HASS_WATCHDOG_REANIMATE_FAILURES, 0)
        if reanimate_fails > HASS_WATCHDOG_MAX_REANIMATE_ATTEMPTS and not IS_MACOS:
            return

        returning = IS_MACOS and self._safe_mode.returning
        safe_mode = returning or self._is_safe_mode_attempt(reanimate_fails)
        if returning:
            _LOGGER.error("Going back to safe mode after a failed normal start")
        elif safe_mode:
            _LOGGER.critical(
                "Watchdog cannot reanimate Home Assistant Core, failed all %s attempts. Restarting into safe mode",
                reanimate_fails,
            )
        elif not_running_too_long:
            _LOGGER.error(
                "Watchdog found Home Assistant Core answering but not running for %s seconds. Restarting Home Assistant Core!",
                HASS_WATCHDOG_NOT_RUNNING_GRACE,
            )
        else:
            self._log_restart_reason(down, api_fails)

        if IS_MACOS and not down:
            await self._prepare_native_restart(no_answer, recent_hung)

        try:
            if safe_mode:
                await self._enter_safe_mode()
            elif down:
                await core.start()
            else:
                await core.restart()
        except (HomeAssistantError, JobException) as err:
            # An unexpected error inside a job arrives as a plain JobException.
            if not IS_MACOS and not isinstance(err, HomeAssistantError):
                raise
            await self._reanimate_failed(err, safe_mode, reanimate_fails)
        else:
            self._cache[HASS_WATCHDOG_REANIMATE_FAILURES] = 0
        finally:
            self._cache[HASS_WATCHDOG_API_FAILURES] = 0
            self._cache.pop(HASS_WATCHDOG_NOT_RUNNING_SINCE, None)
            if IS_MACOS:
                self._finish_native_restart(no_answer)

    @Job(
        name="tasks_update_cli",
        conditions=PLUGIN_AUTO_UPDATE_CONDITIONS,
        internal=True,
    )
    async def _update_cli(self):
        """Check and run update of cli."""
        if not self.sys_plugins.cli.need_update:
            return

        _LOGGER.info(
            "Found new cli version %s, updating", self.sys_plugins.cli.latest_version
        )
        await self.sys_plugins.cli.update()

    @Job(
        name="tasks_update_dns",
        conditions=PLUGIN_AUTO_UPDATE_CONDITIONS,
        internal=True,
    )
    async def _update_dns(self):
        """Check and run update of CoreDNS plugin."""
        if not self.sys_plugins.dns.need_update:
            return

        _LOGGER.info(
            "Found new CoreDNS plugin version %s, updating",
            self.sys_plugins.dns.latest_version,
        )
        await self.sys_plugins.dns.update()

    @Job(
        name="tasks_update_audio",
        conditions=PLUGIN_AUTO_UPDATE_CONDITIONS,
        internal=True,
    )
    async def _update_audio(self):
        """Check and run update of PulseAudio plugin."""
        if not self.sys_plugins.audio.need_update:
            return

        _LOGGER.info(
            "Found new PulseAudio plugin version %s, updating",
            self.sys_plugins.audio.latest_version,
        )
        await self.sys_plugins.audio.update()

    @Job(
        name="tasks_update_observer",
        conditions=PLUGIN_AUTO_UPDATE_CONDITIONS,
        internal=True,
    )
    async def _update_observer(self):
        """Check and run update of Observer plugin."""
        if not self.sys_plugins.observer.need_update:
            return

        _LOGGER.info(
            "Found new Observer plugin version %s, updating",
            self.sys_plugins.observer.latest_version,
        )
        await self.sys_plugins.observer.update()

    @Job(
        name="tasks_update_multicast",
        conditions=PLUGIN_AUTO_UPDATE_CONDITIONS,
        internal=True,
    )
    async def _update_multicast(self):
        """Check and run update of multicast."""
        if not self.sys_plugins.multicast.need_update:
            return

        _LOGGER.info(
            "Found new Multicast version %s, updating",
            self.sys_plugins.multicast.latest_version,
        )
        await self.sys_plugins.multicast.update()

    async def _watchdog_observer_application(self):
        """Check running state of application and rebuild if they is not response."""
        # The observer is a container plugin; a native host has none.
        if IS_MACOS:
            return
        # if observer plugin is active
        if (
            self.sys_plugins.observer.in_progress
            or await self.sys_plugins.observer.check_system_runtime()
        ):
            return
        _LOGGER.warning("Watchdog/Application found a problem with observer plugin!")

        try:
            await self.sys_plugins.observer.rebuild()
        except ObserverError:
            _LOGGER.error("Observer watchdog reanimation failed!")

    async def _watchdog_app_application(self):
        """Check running state of the application and start if they is hangs."""
        for app in self.sys_apps.installed:
            # Natively the event driven watchdog gives up (5 failed attempts, or
            # its rate limit in a crash loop) and no new event follows while the
            # state stays the same, so this tick keeps trying.
            if IS_MACOS and self.sys_core.state == CoreState.RUNNING:
                # Anything wrong with this app must not stop the others being checked.
                try:
                    if await app.native_down_unexpectedly():
                        _LOGGER.error(
                            "Watchdog found app %s not running. Starting it!", app.slug
                        )
                        # Don't wait for it to report started: an app in a crash
                        # loop would hold up this check for the other apps.
                        await app.start()
                        continue
                except AppsError as err:
                    _LOGGER.error(
                        "%s watchdog reanimation failed with %s", app.slug, err
                    )
                    continue
                except Exception:  # pylint: disable=broad-except
                    _LOGGER.exception("%s watchdog reanimation failed", app.slug)
                    continue

            # if watchdog need looking for
            if not app.watchdog or app.state != AppState.STARTED:
                continue

            # Init cache data
            retry_scan = self._cache.get(app.slug, 0)

            # if App have running actions / Application work
            if app.in_progress or await app.watchdog_application():
                self._cache[app.slug] = 0
                continue

            # Look like we run into a problem
            retry_scan += 1
            if retry_scan == 1:
                self._cache[app.slug] = retry_scan
                _LOGGER.warning(
                    "Watchdog missing application response from %s", app.slug
                )
                continue

            _LOGGER.warning("Watchdog found a problem with %s application!", app.slug)
            try:
                await (await app.restart())
            except AppsError as err:
                _LOGGER.error("%s watchdog reanimation failed with %s", app.slug, err)
                await async_capture_exception(err)
            finally:
                self._cache[app.slug] = 0

    @Job(
        name="tasks_reload_store",
        conditions=[
            JobCondition.SUPERVISOR_UPDATED,
            JobCondition.OS_SUPPORTED,
            JobCondition.HOME_ASSISTANT_CORE_SUPPORTED,
        ],
        internal=True,
    )
    async def _reload_store(self) -> None:
        """Reload store and check for app updates."""
        await self.sys_store.reload()

    @Job(
        name="tasks_core_backup_cleanup",
        conditions=[JobCondition.HEALTHY],
        internal=True,
    )
    async def _core_backup_cleanup(self) -> None:
        """Core backup is intended for transient use, remove any old backups that got left behind."""
        old_backups = [
            backup
            for backup in self.sys_backups.list_backups
            if LOCATION_CLOUD_BACKUP in backup.all_locations
            and datetime.fromisoformat(backup.date) < utcnow() - OLD_BACKUP_THRESHOLD
        ]
        for backup in old_backups:
            try:
                await self.sys_backups.remove(
                    backup, [cast(LOCATION_TYPE, LOCATION_CLOUD_BACKUP)]
                )
            except BackupFileNotFoundError as err:
                _LOGGER.debug("Can't remove backup %s: %s", backup.slug, err)
