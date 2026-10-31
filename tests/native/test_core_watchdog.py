"""Test that the native watchdog keeps Core up.

These run without the coresys fixture, which needs a D-Bus daemon.
"""

import asyncio
import inspect
import os
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from awesomeversion import AwesomeVersion
import pytest

from supervisor.apps.app import App
from supervisor.const import AppState, CoreState
from supervisor.docker.const import ContainerState
from supervisor.docker.monitor import DockerContainerStateEvent
from supervisor.exceptions import AppsError, DockerError, HomeAssistantError
from supervisor.homeassistant.api import APIState
from supervisor.misc.tasks import (
    HASS_WATCHDOG_LAST_HUNG_RESTART,
    HASS_WATCHDOG_NEXT_ATTEMPT,
    HASS_WATCHDOG_NOT_RUNNING_GRACE,
    HASS_WATCHDOG_NOT_RUNNING_SINCE,
    HASS_WATCHDOG_RECENT_HUNG_SECONDS,
    Tasks,
)
from supervisor.native.app import NativeApp
from supervisor.native.homeassistant import (
    HUNG_STOP_TIMEOUT,
    STOP_TIMEOUT,
    NativeHomeAssistant,
)
from supervisor.native.launchd import LaunchAgent, LaunchdError, ServiceStatus


def make_tasks(*, running: bool = True, api_ok: bool = True) -> tuple[Tasks, MagicMock]:
    """Return Tasks on a fake coresys, and the fake Core."""
    coresys = MagicMock()
    coresys.core.state = CoreState.RUNNING
    homeassistant = coresys.homeassistant
    homeassistant.watchdog = True
    homeassistant.error_state = False
    homeassistant.version = AwesomeVersion("2026.10.2")
    state = APIState("RUNNING", False) if api_ok else None
    homeassistant.api.get_api_state = AsyncMock(return_value=state)
    core = homeassistant.core
    core.in_progress = False
    core.is_running = AsyncMock(return_value=running)
    core.instance.stop_requested = False
    core.instance.capture_stacks = AsyncMock()
    core.instance.hung = False
    core.start = AsyncMock()
    core.restart = AsyncMock()
    core.rebuild = AsyncMock()
    return Tasks(coresys), core


async def tick(tasks: Tasks) -> None:
    """Run the watchdog once, as the scheduler does."""
    with patch("supervisor.misc.tasks.async_capture_exception", AsyncMock()):
        # Spacing of failed attempts has its own test.
        tasks._cache.pop(HASS_WATCHDOG_NEXT_ATTEMPT, None)
        await tasks._watchdog_homeassistant_api()


async def test_healthy_core_left_alone():
    """Test nothing happens while Core answers."""
    tasks, core = make_tasks()
    await tick(tasks)
    core.start.assert_not_called()
    core.restart.assert_not_called()


async def test_down_core_is_started_at_once():
    """Test a Core that is not running is started on the next tick."""
    tasks, core = make_tasks(running=False)
    await tick(tasks)
    core.start.assert_awaited_once()
    core.restart.assert_not_called()


async def test_keeps_trying_after_failures_and_error_state():
    """Test the 2026-10-28 outage: a failed attempt must not be the last one."""
    tasks, core = make_tasks(running=False)
    core.start.side_effect = HomeAssistantError()
    core.rebuild.side_effect = HomeAssistantError()
    tasks.sys_homeassistant.error_state = True

    for _ in range(20):
        await tick(tasks)

    # One safe mode attempt after five failures, normal starts for the rest.
    assert core.rebuild.await_count == 1
    assert core.rebuild.await_args.kwargs == {"safe_mode": True}
    assert core.start.await_count == 19


async def test_recovery_resets_the_count():
    """Test a later failure starts counting from zero again."""
    tasks, core = make_tasks(running=False)
    core.start.side_effect = HomeAssistantError()
    for _ in range(3):
        await tick(tasks)
    core.start.side_effect = None
    await tick(tasks)
    core.is_running.return_value = True
    await tick(tasks)
    core.is_running.return_value = False
    core.start.reset_mock()
    for _ in range(5):
        await tick(tasks)
    core.rebuild.assert_not_called()
    assert core.start.await_count == 5


async def test_hung_core_restarted_after_two_misses():
    """Test a running Core that stops answering is restarted, also after failures."""
    tasks, core = make_tasks(api_ok=False)
    core.restart.side_effect = HomeAssistantError()
    tasks.sys_homeassistant.error_state = True
    await tick(tasks)
    core.restart.assert_not_called()
    await tick(tasks)
    core.restart.assert_awaited_once()
    # A failed restart still counts as recent: 4 misses before the next one.
    for _ in range(3):
        await tick(tasks)
    assert core.restart.await_count == 1
    await tick(tasks)
    assert core.restart.await_count == 2


async def test_hung_core_stacks_captured_before_restart():
    """Test the stacks are saved first, and only for a Core that hung."""
    tasks, core = make_tasks(api_ok=False)
    order = MagicMock()
    order.attach_mock(core.instance.capture_stacks, "capture")
    order.attach_mock(core.restart, "restart")
    await tick(tasks)
    core.instance.capture_stacks.assert_not_called()
    await tick(tasks)
    assert [c[0] for c in order.mock_calls] == ["capture", "restart"]

    tasks, core = make_tasks(running=False)
    await tick(tasks)
    core.instance.capture_stacks.assert_not_called()


async def test_requested_stop_is_respected():
    """Test a stop someone asked for keeps Core down."""
    tasks, core = make_tasks(running=False)
    core.instance.stop_requested = True
    await tick(tasks)
    core.start.assert_not_called()


async def test_running_job_is_respected():
    """Test an update or restart in progress is left alone."""
    tasks, core = make_tasks(running=False)
    core.in_progress = True
    await tick(tasks)
    core.start.assert_not_called()


async def test_supervisor_shutdown_is_respected():
    """Test Core is not started while the Supervisor shuts down."""
    tasks, core = make_tasks(running=False)
    tasks.coresys.core.state = CoreState.SHUTDOWN
    await tick(tasks)
    core.start.assert_not_called()


def make_app(
    state: ContainerState, *, was_running: bool = True, manual_stop: bool = False
) -> App:
    """Return an App with only what the down check reads."""
    app = App.__new__(App)
    app._manual_stop = manual_stop
    app.instance = MagicMock(was_running=was_running)
    app.instance.current_state = AsyncMock(return_value=state)
    return app


@pytest.mark.parametrize(
    ("state", "was_running", "manual_stop", "expected"),
    [
        (ContainerState.RUNNING, True, False, False),
        # Crashed: restart, also if it crashed before this Supervisor started.
        (ContainerState.FAILED, True, False, True),
        (ContainerState.FAILED, False, False, True),
        # Exited 0 or unloaded by hand after running.
        (ContainerState.STOPPED, True, False, True),
        (ContainerState.UNKNOWN, True, False, True),
        # Not loaded and never seen running: may be a stop from before a restart.
        (ContainerState.UNKNOWN, False, False, False),
        # Stopped on purpose.
        (ContainerState.UNKNOWN, True, True, False),
        (ContainerState.FAILED, True, True, False),
    ],
)
async def test_app_down_unexpectedly(state, was_running, manual_stop, expected):
    """Test which down states the app watchdog acts on."""
    app = make_app(state, was_running=was_running, manual_stop=manual_stop)
    with (
        patch.object(App, "watchdog", True),
        patch.object(App, "in_progress", False),
    ):
        assert await app.native_down_unexpectedly() is expected


async def test_app_down_watchdog_off_or_job_running():
    """Test the watchdog option and a running job are respected."""
    app = make_app(ContainerState.FAILED)
    with patch.object(App, "watchdog", False), patch.object(App, "in_progress", False):
        assert await app.native_down_unexpectedly() is False
    with patch.object(App, "watchdog", True), patch.object(App, "in_progress", True):
        assert await app.native_down_unexpectedly() is False


async def test_app_watchdog_keeps_trying():
    """Test failed starts are retried on every tick, with no limit."""
    tasks, _ = make_tasks()
    app = MagicMock(slug="core_test", state=AppState.UNKNOWN, watchdog=True)
    app.native_down_unexpectedly = AsyncMock(return_value=True)
    app.start = AsyncMock(side_effect=AppsError())
    tasks.coresys.apps.installed = [app]
    for _ in range(30):
        await tasks._watchdog_app_application()
    assert app.start.await_count == 30


async def test_app_watchdog_stands_aside_during_shutdown():
    """Test no app is started while the Supervisor shuts down."""
    tasks, _ = make_tasks()
    tasks.coresys.core.state = CoreState.SHUTDOWN
    app = MagicMock(slug="core_test", state=AppState.UNKNOWN, watchdog=True)
    app.native_down_unexpectedly = AsyncMock(return_value=True)
    app.start = AsyncMock()
    tasks.coresys.apps.installed = [app]
    await tasks._watchdog_app_application()
    app.start.assert_not_called()


async def test_bootout_raises_if_service_stays_loaded():
    """Test a service launchd never unloads is reported, not assumed gone."""
    agent = LaunchAgent("ha-macos-test.stuck")
    loaded = ServiceStatus(True, False, None, None)
    with (
        patch.object(LaunchAgent, "status", AsyncMock(return_value=loaded)),
        patch("supervisor.native.launchd._launchctl", AsyncMock(return_value=(0, ""))),
        patch("supervisor.native.launchd.BOOTOUT_TIMEOUT", 0),
        pytest.raises(LaunchdError),
    ):
        await agent.bootout(1)


@pytest.mark.skipif(sys.platform != "darwin", reason="needs launchd")
async def test_restart_of_a_hung_service_really_restarts(tmp_path: Path):
    """Test against real launchd: stop then start runs a second process."""
    loop = asyncio.get_running_loop()
    runs = tmp_path / "runs"
    script = tmp_path / "stub.py"
    await loop.run_in_executor(
        None,
        script.write_text,
        "import signal, time\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        f"open({str(runs)!r}, 'a').write('run\\n')\n"
        "while True:\n    time.sleep(1)\n",
    )
    agent = LaunchAgent(f"ha-macos-test.{tmp_path.name.lower()}")
    await loop.run_in_executor(
        None,
        lambda: agent.write_plist(
            ["/usr/bin/python3", str(script)],
            environment={},
            working_directory=tmp_path,
            stdout=tmp_path / "out",
            stderr=tmp_path / "out",
        ),
    )
    try:
        await agent.bootstrap()
        await asyncio.sleep(2)
        await agent.bootout(1)
        assert not (await agent.status()).loaded
        await agent.bootstrap()
        await asyncio.sleep(2)
        status = await agent.status()
        assert status.running
        text = await loop.run_in_executor(None, runs.read_text)
        assert text.count("run") == 2
    finally:
        await agent.bootout(1)
        await loop.run_in_executor(None, agent.remove_plist)


# Aborting a process pops up macOS's crash dialog, so this runs only when asked.
@pytest.mark.skipif(
    sys.platform != "darwin" or not os.environ.get("HA_TEST_REAL_ABORT"),
    reason="needs launchd and HA_TEST_REAL_ABORT",
)
async def test_stuck_service_leaves_its_stacks_behind(tmp_path: Path):
    """Test against real launchd: sample, then SIGABRT reaches the fault handler."""
    loop = asyncio.get_running_loop()
    fault = tmp_path / "fault"
    sampled = tmp_path / "sampled"
    script = tmp_path / "stub.py"
    await loop.run_in_executor(
        None,
        script.write_text,
        "import faulthandler, signal, time\n"
        f"faulthandler.enable(open({str(fault)!r}, 'a'))\n"
        "signal.signal(signal.SIGTERM, signal.SIG_IGN)\n"
        "def stuck_here():\n    time.sleep(100000)\n"
        "stuck_here()\n",
    )
    agent = LaunchAgent(f"ha-macos-test.{tmp_path.name.lower()}")
    await loop.run_in_executor(
        None,
        lambda: agent.write_plist(
            ["/usr/bin/python3", str(script)],
            environment={},
            working_directory=tmp_path,
            stdout=tmp_path / "out",
            stderr=tmp_path / "out",
        ),
    )
    try:
        await agent.bootstrap()
        await asyncio.sleep(2)
        assert await agent.sample(sampled)
        assert "Thread_" in await loop.run_in_executor(None, sampled.read_text)
        await agent.bootout(1, abort_first=True)
        assert not (await agent.status()).loaded
        assert "stuck_here" in await loop.run_in_executor(None, fault.read_text)
    finally:
        await agent.bootout(1)
        await loop.run_in_executor(None, agent.remove_plist)


async def test_sample_failure_never_raises(tmp_path: Path):
    """Test a service without a process just reports no sample."""
    agent = LaunchAgent("ha-macos-test.absent")
    down = ServiceStatus(False, False, None, None)
    with patch.object(LaunchAgent, "status", AsyncMock(return_value=down)):
        assert not await agent.sample(tmp_path / "sampled")


async def test_capture_stacks_prunes_and_never_raises(tmp_path: Path):
    """Test old samples are pruned and a failing sample doesn't stop the restart."""
    loop = asyncio.get_running_loop()
    hass = MagicMock()
    hass.path_logs = tmp_path / "logs"
    hass.sys_run_in_executor = lambda func: loop.run_in_executor(None, func)

    def make_old() -> None:
        hass.path_logs.mkdir()
        for number in range(12):
            (hass.path_logs / f"hang-{number:02}.sample.txt").touch()

    await loop.run_in_executor(None, make_old)
    hass.agent.sample = AsyncMock(return_value=True)
    await NativeHomeAssistant.capture_stacks(hass)
    left = await loop.run_in_executor(None, lambda: list(hass.path_logs.iterdir()))
    assert len(left) == 9
    assert hass.agent.sample.await_args.args[0].name.startswith("hang-2")

    hass.agent.sample = AsyncMock(side_effect=OSError("no sample"))
    await NativeHomeAssistant.capture_stacks(hass)


async def test_failed_attempts_stay_spaced_out():
    """Test a Core that won't start isn't hammered at the fast check rate."""
    tasks, core = make_tasks(running=False)
    core.start.side_effect = HomeAssistantError()
    await tick(tasks)
    assert core.start.await_count == 1
    with patch("supervisor.misc.tasks.async_capture_exception", AsyncMock()):
        await tasks._watchdog_homeassistant_api()
    assert core.start.await_count == 1
    await tick(tasks)
    assert core.start.await_count == 2


async def test_starting_core_gets_longer():
    """Test a Core that answers but isn't running is given time, then restarted."""
    tasks, core = make_tasks()
    tasks.sys_homeassistant.api.get_api_state.return_value = APIState(
        "NOT_RUNNING", False
    )
    for _ in range(5):
        await tick(tasks)
    core.restart.assert_not_called()
    tasks._cache[HASS_WATCHDOG_NOT_RUNNING_SINCE] -= HASS_WATCHDOG_NOT_RUNNING_GRACE
    await tick(tasks)
    core.restart.assert_awaited_once()


async def test_hung_stop_timeout_only_for_the_hung_restart():
    """Test the watchdog's restart uses the short timeout, and only that one."""
    tasks, core = make_tasks(api_ok=False)
    seen = []
    core.restart.side_effect = lambda: seen.append(core.instance.hung)
    await tick(tasks)
    await tick(tasks)
    assert seen == [True]
    assert core.instance.hung is False


def record_hung(core: MagicMock) -> list[bool]:
    """Return a list that collects the hung flag at each restart."""
    seen: list[bool] = []
    core.restart.side_effect = lambda: seen.append(core.instance.hung)
    return seen


async def test_core_answering_but_not_running_gets_the_full_stop():
    """Test a Core that answers isn't treated as frozen when it is restarted."""
    tasks, core = make_tasks()
    seen = record_hung(core)
    tasks.sys_homeassistant.api.get_api_state.return_value = APIState(
        "NOT_RUNNING", False
    )
    await tick(tasks)
    tasks._cache[HASS_WATCHDOG_NOT_RUNNING_SINCE] -= HASS_WATCHDOG_NOT_RUNNING_GRACE
    await tick(tasks)
    assert seen == [False]
    assert HASS_WATCHDOG_LAST_HUNG_RESTART not in tasks._cache


async def test_second_freeze_soon_after_goes_at_the_old_pace():
    """Test 4 misses and the full stop within the window, then back to fast."""
    tasks, core = make_tasks(api_ok=False)
    seen = record_hung(core)
    await tick(tasks)
    await tick(tasks)
    assert seen == [True]

    for _ in range(3):
        await tick(tasks)
    assert seen == [True]
    await tick(tasks)
    assert seen == [True, False]

    tasks._cache[HASS_WATCHDOG_LAST_HUNG_RESTART] -= HASS_WATCHDOG_RECENT_HUNG_SECONDS
    await tick(tasks)
    await tick(tasks)
    assert seen == [True, False, True]


async def test_any_answer_resets_the_misses():
    """Test misses separated by an answer are not in a row."""
    tasks, core = make_tasks(api_ok=False)
    api = tasks.sys_homeassistant.api
    await tick(tasks)
    api.get_api_state.return_value = APIState("NOT_RUNNING", False)
    await tick(tasks)
    api.get_api_state.return_value = None
    await tick(tasks)
    core.restart.assert_not_called()


def test_short_stop_only_when_hung():
    """Test the stop timeout follows the hung flag."""
    hass = MagicMock(hung=False)
    assert NativeHomeAssistant.timeout.fget(hass) == STOP_TIMEOUT
    hass.hung = True
    assert NativeHomeAssistant.timeout.fget(hass) == HUNG_STOP_TIMEOUT
    assert HUNG_STOP_TIMEOUT >= 60


async def test_stop_uses_the_timeout_and_aborts_first():
    """Test stop passes the current timeout on, so the flag really has an effect."""
    hass = MagicMock(timeout=HUNG_STOP_TIMEOUT)
    hass.agent.bootout = AsyncMock()
    await NativeHomeAssistant.stop(hass, remove_container=False)
    hass.agent.bootout.assert_awaited_once_with(HUNG_STOP_TIMEOUT, abort_first=True)


def startable_app() -> App:
    """Return an App, stopped on purpose, whose start can run far enough."""
    app = make_app(ContainerState.UNKNOWN, was_running=False, manual_stop=True)
    app.slug = "core_test"
    app._wait_for_startup_task = MagicMock()
    app._wait_for_startup_task.done.return_value = False
    return app


async def test_start_means_it_should_be_running():
    """Test a start ends a stop, without waiting to see the app run."""
    app = startable_app()
    app.instance.is_running = AsyncMock(return_value=True)
    await inspect.unwrap(App.start)(app)
    assert app._manual_stop is False
    assert app.instance.was_running is True


async def test_failed_start_still_counts_as_wanted():
    """Test an app whose start fails is picked up by the watchdog (the stuck bug)."""
    app = startable_app()
    app.instance.is_running = AsyncMock(side_effect=DockerError())
    with pytest.raises(DockerError):
        await inspect.unwrap(App.start)(app)
    with patch.object(App, "watchdog", True), patch.object(App, "in_progress", False):
        assert await app.native_down_unexpectedly() is True


async def test_one_app_failing_does_not_stop_the_others():
    """Test any error from one app still lets the next app be checked."""
    tasks, _ = make_tasks()
    bad = MagicMock(slug="bad", state=AppState.UNKNOWN, watchdog=True)
    bad.native_down_unexpectedly = AsyncMock(side_effect=RuntimeError("boom"))
    good = MagicMock(slug="good", state=AppState.UNKNOWN, watchdog=True)
    good.native_down_unexpectedly = AsyncMock(return_value=True)
    good.start = AsyncMock()
    tasks.coresys.apps.installed = [bad, good]
    await tasks._watchdog_app_application()
    good.start.assert_awaited_once()


async def test_stuck_core_answering_on_and_off_is_still_restarted():
    """Test a miss now and then doesn't keep restarting the not running grace."""
    tasks, core = make_tasks()
    api = tasks.sys_homeassistant.api
    not_running = APIState("NOT_RUNNING", False)
    api.get_api_state.return_value = not_running
    await tick(tasks)
    api.get_api_state.return_value = None
    await tick(tasks)
    api.get_api_state.return_value = not_running
    tasks._cache[HASS_WATCHDOG_NOT_RUNNING_SINCE] -= HASS_WATCHDOG_NOT_RUNNING_GRACE
    await tick(tasks)
    core.restart.assert_awaited_once()


async def test_recent_window_starts_when_the_restart_ends():
    """Test a slow restart doesn't use up the window it is meant to start."""
    tasks, core = make_tasks(api_ok=False)
    during: list[bool] = []
    core.restart.side_effect = lambda: during.append(
        HASS_WATCHDOG_LAST_HUNG_RESTART in tasks._cache
    )
    await tick(tasks)
    await tick(tasks)
    assert during == [False]
    assert HASS_WATCHDOG_LAST_HUNG_RESTART in tasks._cache


async def test_user_stop_during_event_restart_is_respected():
    """Test a stop that lands while the event watchdog restarts the app wins."""
    app = make_app(ContainerState.FAILED)
    app.instance.current_state = AsyncMock(return_value=ContainerState.FAILED)

    async def user_stop_lands(**_) -> None:
        app._manual_stop = True

    app.instance.stop = AsyncMock(side_effect=user_stop_lands)
    with (
        patch.object(App, "in_progress", False),
        patch.object(App, "name", "Test"),
        patch.object(App, "start", AsyncMock()) as start,
    ):
        await inspect.unwrap(App._restart_after_problem)(app, ContainerState.FAILED, 1)
    start.assert_not_called()


async def test_user_stop_during_the_down_check_is_respected():
    """Test a stop that lands while the state is being read counts."""
    app = make_app(ContainerState.FAILED)

    async def user_stop_lands() -> ContainerState:
        app._manual_stop = True
        return ContainerState.FAILED

    app.instance.current_state = AsyncMock(side_effect=user_stop_lands)
    with patch.object(App, "watchdog", True), patch.object(App, "in_progress", False):
        assert await app.native_down_unexpectedly() is False


@pytest.mark.parametrize(
    ("event_state", "current", "restarts"),
    [
        (ContainerState.STOPPED, ContainerState.UNKNOWN, False),
        (ContainerState.STOPPED, ContainerState.RUNNING, False),
        (ContainerState.STOPPED, ContainerState.STOPPED, True),
        (ContainerState.FAILED, ContainerState.FAILED, True),
    ],
)
async def test_stale_event_does_not_use_the_restart_limit(
    event_state: ContainerState, current: ContainerState, restarts: bool
):
    """Test the STOPPED that each start fires doesn't use a rate limit slot."""
    app = make_app(current)
    app.instance.name = "app"
    event = DockerContainerStateEvent("app", event_state, "id", 0)
    with (
        patch.object(App, "watchdog", True),
        patch.object(App, "_restart_after_problem", AsyncMock()) as restart,
    ):
        await app.watchdog_container(event)
    assert restart.await_count == int(restarts)


async def test_any_failed_app_stop_is_a_docker_error():
    """Test App.restart can carry on after a failed stop, so the stop flag clears."""
    native = MagicMock()
    native._service.stop = AsyncMock(side_effect=TimeoutError())
    with pytest.raises(DockerError):
        await NativeApp.stop(native)
    assert native._expected_down is False


@pytest.mark.parametrize("cls", [NativeApp, NativeHomeAssistant])
async def test_poll_does_not_report_running_over_a_stop(cls: type):
    """Test a stop landing during the state read isn't undone by a RUNNING event."""
    native = MagicMock(_expected_down=False, _last_state=ContainerState.UNKNOWN)

    async def stop_lands() -> ContainerState:
        native._expected_down = True
        return ContainerState.RUNNING

    native.current_state = AsyncMock(side_effect=stop_lands)
    with (
        patch("asyncio.sleep", AsyncMock(side_effect=[None, asyncio.CancelledError])),
        pytest.raises(asyncio.CancelledError),
    ):
        await cls._watch(native)
    native._fire.assert_not_called()
