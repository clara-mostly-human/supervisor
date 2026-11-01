"""Test that the native watchdog keeps Core up.

These run without the coresys fixture, which needs a D-Bus daemon.
"""

import asyncio
import inspect
import json
import os
from pathlib import Path
import sys
import time
from unittest.mock import AsyncMock, MagicMock, patch

from awesomeversion import AwesomeVersion
import pytest

from supervisor.apps.app import App
from supervisor.const import AppState, CoreState
from supervisor.docker.const import ContainerState
from supervisor.docker.monitor import DockerContainerStateEvent
from supervisor.exceptions import (
    AppsError,
    DockerError,
    HomeAssistantAPIError,
    HomeAssistantError,
    HomeAssistantJobError,
    JobException,
)
from supervisor.homeassistant.api import APIState
from supervisor.misc.tasks import (
    HASS_WATCHDOG_LAST_HUNG_RESTART,
    HASS_WATCHDOG_NEXT_ATTEMPT,
    HASS_WATCHDOG_NOT_RUNNING_GRACE,
    HASS_WATCHDOG_NOT_RUNNING_SINCE,
    HASS_WATCHDOG_REANIMATE_FAILURES,
    HASS_WATCHDOG_RECENT_HUNG_SECONDS,
    Tasks,
)
from supervisor.native.app import NativeApp
from supervisor.native.homeassistant import (
    HUNG_STOP_TIMEOUT,
    STOP_TIMEOUT,
    NativeHomeAssistant,
    _run as native_run,
)
from supervisor.native.launchd import LaunchAgent, LaunchdError, ServiceStatus
from supervisor.native.safe_mode import (
    FIRST_WAIT,
    MAX_WAIT,
    RECOVERY_WAIT,
    STATE_FILE,
    Phase,
)

CORE_PID = 100
_data_dir: Path


@pytest.fixture(autouse=True)
def data_dir(tmp_path: Path) -> Path:
    """Give each test its own Supervisor data folder."""
    global _data_dir  # noqa: PLW0603 # pylint: disable=global-statement
    _data_dir = tmp_path
    (tmp_path / "core").mkdir()
    (tmp_path / "config").mkdir()
    return tmp_path


def _in_executor(funct, *args):
    return asyncio.get_running_loop().run_in_executor(None, funct, *args)


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
    coresys.config.path_supervisor = _data_dir
    coresys.config.path_homeassistant = _data_dir / "config"
    coresys.run_in_executor = _in_executor
    homeassistant.api.get_config = AsyncMock(
        return_value={"state": "RUNNING", "safe_mode": False}
    )
    homeassistant.api.make_request.return_value.__aenter__.return_value.status = 200
    core.instance.agent.status = AsyncMock(
        return_value=ServiceStatus(True, True, CORE_PID, None)
    )
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

    # Every sixth attempt is safe mode: one failure there must not rule it out.
    assert core.rebuild.await_count == 3
    assert core.rebuild.await_args.kwargs == {"safe_mode": True}
    assert core.start.await_count == 17


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


def state_file() -> Path:
    """Return where the watchdog's safe mode state is kept."""
    return _data_dir / "core" / STATE_FILE


def saved_state() -> dict | None:
    """Return the watchdog's safe mode state on disk."""
    return json.loads(state_file().read_text()) if state_file().exists() else None


def marker() -> Path:
    """Return the marker Core reads at start."""
    return _data_dir / "config" / "safe-mode"


class FakeCore:
    """Core as far as the watchdog can see it, honouring the safe mode marker."""

    def __init__(self, tasks: Tasks, core: MagicMock) -> None:
        """Replace the mocks with behaviour."""
        self.tasks = tasks
        self.core = core
        self.pid: int | None = CORE_PID
        self.state = "RUNNING"
        self.safe = False
        self.recovery = False
        self.broken = False  # A normal start crashes; safe mode works.
        self.recovery_on_start = False  # With or without safe mode.
        self.recovery_unless_safe = False  # Safe mode gets past it.
        self.wont_stop = False
        self.next_pid = CORE_PID
        self.notify_fails = False
        tasks.coresys.delivered = []
        api = tasks.sys_homeassistant.api
        api.make_request = MagicMock(side_effect=self._request)
        api.get_config = AsyncMock(side_effect=self._config)
        core.is_running = AsyncMock(side_effect=lambda: self.pid is not None)
        core.instance.agent.status = AsyncMock(side_effect=self._status)
        core.start = AsyncMock(side_effect=self._start)
        core.restart = AsyncMock(side_effect=self._restart)
        core.rebuild = AsyncMock(side_effect=self._rebuild)
        core.check_config = AsyncMock(return_value=MagicMock(valid=True))
        api.get_core_state = AsyncMock(
            return_value={"state": "RUNNING", "recorder_state": {}}
        )

    def _request(self, method: str, path: str, json: dict) -> MagicMock:
        assert (method, path) == (
            "post",
            "api/services/persistent_notification/create",
        )

        async def enter() -> MagicMock:
            # A Core that isn't running can't take a notification.
            if self.pid is None or self.notify_fails:
                raise HomeAssistantAPIError
            self.tasks.coresys.delivered.append(json["message"])
            return MagicMock(status=200)

        context = MagicMock()
        context.__aenter__ = AsyncMock(side_effect=enter)
        return context

    async def _config(self) -> dict:
        return {
            "state": self.state,
            "safe_mode": self.safe,
            "recovery_mode": self.recovery,
        }

    async def _status(self) -> ServiceStatus:
        return ServiceStatus(self.pid is not None, self.pid is not None, self.pid, None)

    def run_core(self) -> None:
        """Start a new process, which reads and removes the marker."""
        self.safe = marker().exists()
        marker().unlink(missing_ok=True)
        self.recovery = self.recovery_on_start or (
            self.recovery_unless_safe and not self.safe
        )
        if self.broken and not self.safe:
            self.pid = None
            raise HomeAssistantError
        self.next_pid += 1
        self.pid = self.next_pid

    async def _start(self) -> None:
        if self.pid is None:
            await asyncio.get_running_loop().run_in_executor(None, self.run_core)

    async def _restart(self) -> None:
        if self.wont_stop:
            raise HomeAssistantError
        self.pid = None
        await self._start()

    async def _rebuild(self, *, safe_mode: bool = False) -> None:
        loop = asyncio.get_running_loop()
        if safe_mode:
            await loop.run_in_executor(None, marker().touch)
        if not self.wont_stop:
            self.pid = None
        await self._start()

    def clear_calls(self) -> None:
        """Forget calls so far, keeping the behaviour."""
        self.core.start.reset_mock()
        self.core.restart.reset_mock()
        self.core.rebuild.reset_mock()
        self.tasks.coresys.delivered.clear()


def notifications(tasks: Tasks) -> list[str]:
    """Return the messages Core received as persistent notifications."""
    return tasks.coresys.delivered


async def fake_in_safe_mode() -> tuple[Tasks, MagicMock, FakeCore]:
    """Fail five starts, so the sixth attempt goes into safe mode and works."""
    tasks, core = make_tasks()
    fake = FakeCore(tasks, core)
    fake.pid = None
    fake.broken = True
    for _ in range(6):
        await tick(tasks)
    assert fake.safe
    assert saved_state()["phase"] == "in_safe_mode"
    fake.clear_calls()
    return tasks, core, fake


def after(seconds: int):
    """Move the clock the state file uses."""
    return patch("time.time", return_value=time.time() + seconds)


async def test_entering_safe_mode_is_recorded_and_announced():
    """Test the state is saved with the new Core's pid, and the user is told."""
    tasks, core = make_tasks()
    fake = FakeCore(tasks, core)
    fake.pid = None
    fake.broken = True
    for _ in range(6):
        await tick(tasks)

    state = saved_state()
    assert state["pid"] == fake.pid
    assert state["wait"] == FIRST_WAIT
    assert abs(state["retry_at"] - (time.time() + FIRST_WAIT)) < 5
    assert len(notifications(tasks)) == 1
    assert "safe mode" in notifications(tasks)[0]


async def test_intent_is_saved_before_the_attempt():
    """Test a Supervisor killed during the start can still pick it up."""
    tasks, core = make_tasks()
    fake = FakeCore(tasks, core)
    fake.pid = None
    fake.broken = True
    seen = []
    real_rebuild = core.rebuild.side_effect

    async def rebuild(*, safe_mode: bool = False) -> None:
        loop = asyncio.get_running_loop()
        seen.append(await loop.run_in_executor(None, saved_state))
        await real_rebuild(safe_mode=safe_mode)

    core.rebuild.side_effect = rebuild
    for _ in range(6):
        await tick(tasks)
    assert seen[0]["phase"] == "entering"


async def test_stays_until_the_time_comes():
    """Test a healthy Core in safe mode isn't restarted early."""
    tasks, core, _ = await fake_in_safe_mode()
    with after(FIRST_WAIT - 60):
        await tick(tasks)
    core.restart.assert_not_called()
    assert saved_state() is not None


async def test_goes_back_to_normal_when_the_time_comes():
    """Test the stuck safe mode: a healthy looking Core gets a normal start."""
    tasks, core, fake = await fake_in_safe_mode()
    fake.broken = False
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_awaited_once_with()
    core.rebuild.assert_not_called()
    assert not fake.safe
    assert saved_state() is None
    assert len(notifications(tasks)) == 1
    assert "running normally again" in notifications(tasks)[0]

    with after(FIRST_WAIT * 2):
        await tick(tasks)
    core.restart.assert_awaited_once()


async def test_failed_return_goes_straight_back_and_waits_longer():
    """Test a broken normal start costs one attempt, not five, and backs off."""
    tasks, core, fake = await fake_in_safe_mode()
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_awaited_once()
    assert notifications(tasks) == []

    await tick(tasks)
    core.rebuild.assert_awaited_once_with(safe_mode=True)
    core.start.assert_not_called()
    assert fake.safe
    assert saved_state()["wait"] == FIRST_WAIT * 2
    assert "tried again and failed" in notifications(tasks)[0]


async def test_straight_back_survives_a_supervisor_restart():
    """Test a failed return isn't forgotten, costing five more failed starts."""
    tasks, core, _ = await fake_in_safe_mode()
    with after(FIRST_WAIT + 1):
        await tick(tasks)

    restarted = Tasks(tasks.coresys)
    await restarted.load()
    await tick(restarted)
    core.rebuild.assert_awaited_once_with(safe_mode=True)
    core.start.assert_not_called()


async def test_failed_return_log_is_not_about_five_attempts(caplog):
    """Test the log says what happened."""
    tasks, _, _ = await fake_in_safe_mode()
    caplog.clear()
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    await tick(tasks)
    assert "failed all" not in caplog.text


async def test_wait_is_capped():
    """Test Core gets a normal start at least once a day, however often it failed."""
    tasks, core, _ = await fake_in_safe_mode()
    for round_ in range(1, 13):
        with after(MAX_WAIT * 2 * round_):
            await tick(tasks)
            await tick(tasks)
    assert core.restart.await_count == 12
    assert saved_state()["wait"] == MAX_WAIT


async def test_recovery_mode_after_the_return_goes_back_to_safe_mode():
    """Test recovery mode, with locks off, is never reported as normal."""
    tasks, core, fake = await fake_in_safe_mode()
    fake.broken = False
    fake.recovery_unless_safe = True
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_awaited_once()
    core.rebuild.assert_awaited_once_with(safe_mode=True)
    assert fake.safe
    assert saved_state()["wait"] == FIRST_WAIT * 2
    assert not any("running normally" in note for note in notifications(tasks))


async def test_recovery_mode_after_someone_elses_restart_is_looked_after():
    """Test it isn't called normal, and the run goes on with the new Core."""
    tasks, core, fake = await fake_in_safe_mode()
    fake.pid = 999
    fake.safe = False
    fake.recovery = True
    await tick(tasks)
    core.restart.assert_not_called()
    core.rebuild.assert_not_called()
    assert saved_state()["pid"] == 999
    assert abs(saved_state()["retry_at"] - (time.time() + RECOVERY_WAIT)) < 5
    assert "recovery mode" in notifications(tasks)[0]
    assert "running normally" not in notifications(tasks)[0]


async def test_stale_marker_during_the_return_is_a_failed_return():
    """Test landing in safe mode again isn't reported as normal."""
    tasks, core, fake = await fake_in_safe_mode()
    fake.broken = False
    marker().touch()
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_awaited_once()
    assert fake.safe
    assert saved_state()["phase"] == "in_safe_mode"
    assert saved_state()["pid"] == fake.pid
    assert saved_state()["wait"] == FIRST_WAIT * 2
    assert not any("running normally" in note for note in notifications(tasks))


async def test_invalid_config_stays_in_safe_mode_and_says_so():
    """Test a normal start that would land in recovery mode isn't tried."""
    tasks, core, fake = await fake_in_safe_mode()
    core.check_config.return_value = MagicMock(valid=False)
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_not_called()
    assert fake.safe
    assert "configuration" in notifications(tasks)[0]

    core.check_config.return_value = MagicMock(valid=True)
    fake.broken = False
    with after(FIRST_WAIT * 2 - 60):
        await tick(tasks)
    core.restart.assert_not_called()
    with after(FIRST_WAIT * 2 + 60):
        await tick(tasks)
    core.restart.assert_awaited_once()


@pytest.mark.parametrize(
    "error", [HomeAssistantError(), TimeoutError(), OSError("can't fork")]
)
async def test_config_check_that_breaks_keeps_safe_mode(error: Exception):
    """Test not knowing keeps safe mode, and isn't called a bad config."""
    tasks, core, _ = await fake_in_safe_mode()
    core.check_config.side_effect = error
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_not_called()
    assert saved_state()["phase"] == "in_safe_mode"
    assert "could not be checked" in notifications(tasks)[0]


async def test_config_check_that_hangs_is_given_up_on():
    """Test a stuck check can't hold up the watchdog."""
    tasks, core, _ = await fake_in_safe_mode()

    async def hang() -> None:
        await asyncio.sleep(3600)

    core.check_config = AsyncMock(side_effect=hang)
    with (
        patch("supervisor.misc.tasks.HASS_WATCHDOG_CONFIG_CHECK_SECONDS", 0.01),
        after(FIRST_WAIT + 1),
    ):
        async with asyncio.timeout(5):
            await tick(tasks)
    core.restart.assert_not_called()


async def test_core_that_wont_stop_is_told_and_backs_off():
    """Test the same Core isn't restarted every two minutes in silence."""
    tasks, core, fake = await fake_in_safe_mode()
    fake.wont_stop = True
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_awaited_once()
    assert saved_state()["wait"] == FIRST_WAIT * 2
    assert saved_state()["phase"] == "in_safe_mode"
    assert len(notifications(tasks)) == 1
    with after(FIRST_WAIT + 60):
        await tick(tasks)
    core.restart.assert_awaited_once()


async def test_failed_return_stays_spaced():
    """Test the next attempt keeps the normal spacing."""
    tasks, core, _ = await fake_in_safe_mode()
    with after(FIRST_WAIT + 1):
        await tick(tasks)
        with patch("supervisor.misc.tasks.async_capture_exception", AsyncMock()):
            await tasks._watchdog_homeassistant_api()
    core.rebuild.assert_not_called()


async def test_restart_by_someone_else_is_noticed_at_once():
    """Test a normal restart ends it, without waiting for the timer or restarting."""
    tasks, core, fake = await fake_in_safe_mode()
    fake.pid = 999
    fake.safe = False
    await tick(tasks)
    core.restart.assert_not_called()
    assert saved_state() is None
    assert "running normally again" in notifications(tasks)[0]


async def test_new_failure_after_someones_restart_starts_from_thirty_minutes():
    """Test the 24 h wait of an old problem isn't inherited by a new one."""
    tasks, _, fake = await fake_in_safe_mode()
    fake.pid = 999
    fake.safe = False
    await tick(tasks)

    fake.clear_calls()
    fake.pid = None
    for _ in range(6):
        await tick(tasks)
    assert saved_state()["wait"] == FIRST_WAIT
    assert "tried again" not in notifications(tasks)[-1]


async def test_safe_mode_chosen_by_the_user_is_left_alone():
    """Test the user's own safe mode, which arrives as a new Core in safe mode."""
    tasks, core, fake = await fake_in_safe_mode()
    fake.pid = 999
    await tick(tasks)
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_not_called()
    core.rebuild.assert_not_called()
    assert saved_state() is None
    assert notifications(tasks) == []


async def test_watchdogs_own_safe_mode_coming_up_late_is_adopted():
    """Test a start past its deadline isn't taken for someone else's, or lost."""
    tasks, core = make_tasks()
    fake = FakeCore(tasks, core)
    fake.pid = None
    fake.broken = True
    real_rebuild = core.rebuild.side_effect

    async def timed_out(*, safe_mode: bool = False) -> None:
        await real_rebuild(safe_mode=safe_mode)
        raise HomeAssistantError

    core.rebuild.side_effect = timed_out
    for _ in range(6):
        await tick(tasks)
    assert fake.safe
    assert saved_state()["phase"] == "entering"

    await tick(tasks)
    assert saved_state()["phase"] == "in_safe_mode"
    assert saved_state()["pid"] == fake.pid
    assert len(notifications(tasks)) == 1


async def test_supervisor_restart_during_the_start_is_picked_up():
    """Test a Core that reached safe mode with no Supervisor watching."""
    tasks, core = make_tasks()
    fake = FakeCore(tasks, core)
    fake.pid = None
    fake.broken = True
    real_rebuild = core.rebuild.side_effect

    async def killed_midway(*, safe_mode: bool = False) -> None:
        await real_rebuild(safe_mode=safe_mode)
        raise asyncio.CancelledError

    core.rebuild.side_effect = killed_midway
    for _ in range(5):
        await tick(tasks)
    with pytest.raises(asyncio.CancelledError):
        await tick(tasks)

    restarted = Tasks(tasks.coresys)
    await restarted.load()
    await tick(restarted)
    assert saved_state()["phase"] == "in_safe_mode"
    assert len(notifications(tasks)) == 1


async def test_launchctl_failure_changes_nothing():
    """Test not being able to read the pid isn't taken for a new Core."""
    tasks, core, fake = await fake_in_safe_mode()
    pid = fake.pid
    core.instance.agent.status = AsyncMock(
        return_value=ServiceStatus(False, False, None, None)
    )
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_not_called()
    assert saved_state()["pid"] == pid


async def test_no_restart_on_a_guess():
    """Test no restart happens while Core can't say what mode it is in."""
    tasks, core, fake = await fake_in_safe_mode()
    api = tasks.sys_homeassistant.api
    real_config = api.get_config.side_effect
    api.get_config.side_effect = HomeAssistantAPIError()
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_not_called()
    assert saved_state() is not None

    api.get_config.side_effect = real_config
    fake.broken = False
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_awaited_once()


async def test_waits_for_a_backup_to_finish():
    """Test a timed restart doesn't hit a backup in progress."""
    tasks, core, fake = await fake_in_safe_mode()
    fake.broken = False
    tasks.sys_core.state = CoreState.FREEZE
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_not_called()
    assert saved_state() is not None

    tasks.sys_core.state = CoreState.RUNNING
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_awaited_once()


async def test_backup_starting_during_the_config_check_stops_the_restart():
    """Test the state is checked again right before restarting."""
    tasks, core, _ = await fake_in_safe_mode()

    async def backup_starts() -> MagicMock:
        tasks.sys_core.state = CoreState.FREEZE
        return MagicMock(valid=True)

    core.check_config.side_effect = backup_starts
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_not_called()
    assert saved_state()["phase"] == "in_safe_mode"


@pytest.mark.parametrize(
    "core_state",
    [
        {
            "state": "RUNNING",
            "recorder_state": {
                "migration_in_progress": True,
                "migration_is_live": True,
            },
        },
        {
            "state": "NOT_RUNNING",
            "recorder_state": {
                "migration_in_progress": True,
                "migration_is_live": False,
            },
        },
    ],
)
async def test_waits_for_a_database_migration(core_state: dict):
    """Test a timed restart doesn't hit a migration, as Core itself refuses to."""
    tasks, core, _ = await fake_in_safe_mode()
    tasks.sys_homeassistant.api.get_core_state.return_value = core_state
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_not_called()
    assert saved_state() is not None


async def test_waits_for_a_running_core_job():
    """Test an update in progress is left alone."""
    tasks, core, _ = await fake_in_safe_mode()
    core.in_progress = True
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_not_called()
    assert saved_state() is not None


async def test_rejected_restart_is_not_a_failed_return():
    """Test losing a race for the job lock doesn't count against Core."""
    tasks, core, fake = await fake_in_safe_mode()
    fake.broken = False
    real_restart = core.restart.side_effect
    core.restart.side_effect = HomeAssistantJobError()
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_awaited_once()
    assert saved_state()["phase"] == "in_safe_mode"

    core.restart.side_effect = real_restart
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.rebuild.assert_not_called()
    assert core.restart.await_count == 2
    assert saved_state() is None


async def test_requested_stop_during_safe_mode_is_respected():
    """Test a Core the user stopped isn't brought back by the timer."""
    tasks, core, fake = await fake_in_safe_mode()
    fake.pid = None
    core.instance.stop_requested = True
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.start.assert_not_called()
    core.restart.assert_not_called()


async def test_survives_a_supervisor_restart():
    """Test the timer carries on after a Supervisor restart: Core kept running."""
    tasks, core, fake = await fake_in_safe_mode()
    fake.broken = False

    restarted = Tasks(tasks.coresys)
    await restarted.load()
    with after(FIRST_WAIT - 60):
        await tick(restarted)
    core.restart.assert_not_called()
    with after(FIRST_WAIT + 1):
        await tick(restarted)
    core.restart.assert_awaited_once()
    assert saved_state() is None


@pytest.mark.parametrize("content", ["{not json", "[]", '{"phase": "nonsense"}'])
async def test_corrupt_state_file_is_dropped(content: str):
    """Test a damaged file can't stop the Supervisor from starting."""
    state_file().write_text(content)
    tasks, core = make_tasks()
    FakeCore(tasks, core)
    await tasks.load()
    assert not state_file().exists()
    await tick(tasks)
    core.restart.assert_not_called()


async def test_state_that_cant_be_written_still_runs_the_timer():
    """Test the normal start still comes back if the file can't be saved."""
    tasks, core = make_tasks()
    fake = FakeCore(tasks, core)
    fake.pid = None
    fake.broken = True
    (_data_dir / "core").rmdir()
    for _ in range(6):
        await tick(tasks)
    fake.broken = False
    assert fake.safe
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_awaited_once()
    assert not fake.safe


async def test_marker_removed_when_the_attempt_fails():
    """Test a marker Core never read can't cause a silent safe mode later."""
    tasks, core = make_tasks(running=False)
    fake = FakeCore(tasks, core)
    fake.pid = None
    fake.broken = True

    async def fail_after_marker(*, safe_mode: bool) -> None:
        assert safe_mode
        await asyncio.get_running_loop().run_in_executor(None, marker().touch)
        raise HomeAssistantError

    core.rebuild.side_effect = fail_after_marker
    for _ in range(6):
        await tick(tasks)
    core.rebuild.assert_awaited_once()
    assert not marker().exists()


async def test_marker_removed_when_core_never_restarted():
    """Test a stop that failed, so Core carried on and never read the marker."""
    tasks, core = make_tasks()
    fake = FakeCore(tasks, core)
    core.start.side_effect = HomeAssistantError()
    fake.wont_stop = True
    tasks._cache[HASS_WATCHDOG_REANIMATE_FAILURES] = 5
    tasks.sys_homeassistant.api.get_api_state.return_value = None
    core.start.side_effect = None
    for _ in range(2):
        await tick(tasks)
    core.rebuild.assert_awaited_once()
    assert not marker().exists()
    assert saved_state() is None
    assert notifications(tasks) == []


async def test_marker_removed_when_a_new_core_did_not_read_it():
    """Test a marker from an attempt that didn't take can't fire on a later restart."""
    tasks, core = make_tasks()
    fake = FakeCore(tasks, core)
    await tasks._safe_mode.entering()
    fake.pid = 999
    marker().touch()
    await tick(tasks)
    assert not marker().exists()
    assert saved_state() is None


async def test_notification_failure_changes_nothing():
    """Test Core refusing a notification can't get in the way of recovery."""
    tasks, core = make_tasks()
    fake = FakeCore(tasks, core)
    fake.notify_fails = True
    fake.pid = None
    fake.broken = True
    for _ in range(6):
        await tick(tasks)
    assert saved_state()["phase"] == "in_safe_mode"
    fake.broken = False
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_awaited_once()
    assert saved_state() is None


async def test_not_macos_keeps_upstream_behaviour():
    """Test elsewhere it is one safe mode attempt, and no state once it works."""
    tasks, core = make_tasks()
    api = tasks.sys_homeassistant.api
    api.check_api_state = AsyncMock(return_value=False)
    core.restart.side_effect = HomeAssistantError()
    with patch("supervisor.misc.tasks.IS_MACOS", False):
        for _ in range(12):
            await tick(tasks)
        core.rebuild.assert_awaited_once_with(safe_mode=True)

        api.check_api_state.return_value = True
        with after(MAX_WAIT * 2):
            await tick(tasks)
    core.restart.assert_awaited()
    assert core.restart.await_count == 5
    assert saved_state() is None
    api.get_config.assert_not_called()
    api.make_request.assert_not_called()


async def test_failed_way_back_from_recovery_mode_keeps_trying():
    """Test recovery mode, with locks off, doesn't end the run if a rebuild fails."""
    tasks, core, fake = await fake_in_safe_mode()
    fake.broken = False
    fake.recovery_on_start = True
    real_rebuild = core.rebuild.side_effect
    core.rebuild.side_effect = HomeAssistantJobError()
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.rebuild.assert_awaited_once()
    assert saved_state() is not None

    core.rebuild.side_effect = HomeAssistantError()
    await tick(tasks)
    assert core.rebuild.await_count == 2
    assert saved_state() is not None
    assert "staying in recovery mode" in notifications(tasks)[-1]

    core.rebuild.side_effect = real_rebuild
    with after(RECOVERY_WAIT + 1):
        await tick(tasks)
    assert fake.safe
    assert saved_state()["phase"] == "in_safe_mode"


async def test_wait_doubles_once_however_many_safe_starts_fail():
    """Test repeated attempts at one return aren't each counted as another."""
    tasks, core, _ = await fake_in_safe_mode()
    real_rebuild = core.rebuild.side_effect
    core.rebuild.side_effect = HomeAssistantError()
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    for _ in range(7):
        await tick(tasks)
    core.rebuild.side_effect = real_rebuild
    assert core.rebuild.await_count == 2
    for _ in range(6):
        await tick(tasks)
    assert saved_state()["phase"] == "in_safe_mode"
    assert saved_state()["wait"] == FIRST_WAIT * 2


async def test_core_replaced_during_the_config_check_is_not_restarted():
    """Test a stop or a restart by the user during the check isn't overridden."""
    tasks, core, fake = await fake_in_safe_mode()

    async def user_restarts() -> MagicMock:
        fake.pid = 999
        return MagicMock(valid=True)

    core.check_config.side_effect = user_restarts
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_not_called()


async def test_backup_during_the_restart_defers_the_way_back():
    """Test landing in recovery mode during a backup waits for the backup."""
    tasks, core, fake = await fake_in_safe_mode()
    fake.broken = False
    fake.recovery_on_start = True
    real_restart = core.restart.side_effect

    async def backup_starts() -> None:
        await real_restart()
        tasks.sys_core.state = CoreState.FREEZE

    core.restart.side_effect = backup_starts
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.rebuild.assert_not_called()
    await tick(tasks)
    core.rebuild.assert_not_called()

    tasks.sys_core.state = CoreState.RUNNING
    await tick(tasks)
    core.rebuild.assert_awaited_once_with(safe_mode=True)


async def test_unexpected_error_in_the_restart_counts_as_failed():
    """Test it keeps the spacing, and what is left of Core is looked after."""
    tasks, core, fake = await fake_in_safe_mode()
    core.restart.side_effect = JobException()
    with after(FIRST_WAIT + 1):
        await tick(tasks)
        with patch("supervisor.misc.tasks.async_capture_exception", AsyncMock()):
            await tasks._watchdog_homeassistant_api()
    core.restart.assert_awaited_once()
    assert fake.safe
    assert saved_state()["phase"] == "in_safe_mode"
    assert saved_state()["wait"] == FIRST_WAIT * 2


async def test_rejected_safe_attempt_leaves_the_marker_alone():
    """Test it may be from the job that holds the lock, such as the user's."""
    tasks, core = make_tasks()
    fake = FakeCore(tasks, core)
    fake.pid = None
    fake.broken = True
    core.rebuild.side_effect = HomeAssistantJobError()
    for _ in range(5):
        await tick(tasks)
    marker().touch()
    await tick(tasks)
    core.rebuild.assert_awaited_once()
    assert marker().exists()


async def test_rebuild_that_did_nothing_on_a_safe_core_removes_the_marker():
    """Test the same Core, already in safe mode, never read the new marker."""
    tasks, core, fake = await fake_in_safe_mode()
    fake.wont_stop = True
    core.restart.side_effect = None
    await tasks._safe_mode.entering()
    await tasks.sys_homeassistant.core.rebuild(safe_mode=True)
    assert marker().exists()
    await tasks._safe_mode.check()
    assert not marker().exists()
    assert saved_state()["phase"] == "in_safe_mode"


async def test_interrupted_return_starts_over():
    """Test a Supervisor restart before Core was touched doesn't count against it."""
    tasks, core, fake = await fake_in_safe_mode()
    await tasks._safe_mode.return_started()

    restarted = Tasks(tasks.coresys)
    await restarted.load()
    tasks.sys_homeassistant.api.get_core_state.return_value = {"state": "STARTING"}
    with after(FIRST_WAIT + 1):
        await tick(restarted)
    core.restart.assert_not_called()
    assert not restarted._safe_mode.returning
    assert fake.safe


async def test_message_after_a_supervisor_restart_says_since_when():
    """Test the start of the run is kept on disk, for the closing message."""
    tasks, _, fake = await fake_in_safe_mode()
    fake.broken = False
    restarted = Tasks(tasks.coresys)
    await restarted.load()
    with after(FIRST_WAIT + 1):
        await tick(restarted)
    assert "not been running normally since" in notifications(tasks)[0]


async def test_invalid_config_says_so_and_logs_why(caplog):
    """Test the check's own output reaches the log."""
    tasks, core, _ = await fake_in_safe_mode()
    core.check_config.return_value = MagicMock(valid=False, log="bad thing on line 3")
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    assert "does not pass the check" in notifications(tasks)[0]
    assert "bad thing on line 3" in caplog.text


async def test_cancelled_command_does_not_leave_its_process_behind():
    """Test the config check's process goes when the wait for it is given up."""
    with pytest.raises(TimeoutError):
        async with asyncio.timeout(0.5):
            await native_run("/bin/sh", "-c", f"echo $$ > {_data_dir}/pid; sleep 60")
    await asyncio.sleep(0.2)
    pid = int((_data_dir / "pid").read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(pid, 0)


def fake_in_recovery() -> tuple[Tasks, MagicMock, FakeCore]:
    """Return a Core that came up in recovery mode, with no run going."""
    tasks, core = make_tasks()
    fake = FakeCore(tasks, core)
    fake.recovery = True
    return tasks, core, fake


async def test_recovery_mode_gets_a_normal_start_at_once():
    """Test a Core stuck with almost nothing loaded is restarted, and that's said."""
    tasks, core, fake = fake_in_recovery()
    await tick(tasks)
    core.restart.assert_awaited_once_with()
    core.rebuild.assert_not_called()
    assert not fake.recovery
    assert saved_state() is None
    assert "running normally again" in notifications(tasks)[0]

    await tick(tasks)
    core.restart.assert_awaited_once()


async def test_core_is_asked_once_per_process():
    """Test a healthy Core isn't asked about its mode on every check."""
    tasks, core = make_tasks()
    api = tasks.sys_homeassistant.api
    for _ in range(5):
        await tick(tasks)
    assert api.get_config.await_count == 1

    core.instance.agent.status.return_value = ServiceStatus(True, True, 999, None)
    for _ in range(5):
        await tick(tasks)
    assert api.get_config.await_count == 2
    core.restart.assert_not_called()
    assert saved_state() is None


async def test_unanswered_question_is_asked_again():
    """Test a Core that didn't answer isn't taken to be fine."""
    tasks, core, fake = fake_in_recovery()
    api = tasks.sys_homeassistant.api
    api.get_config.side_effect = HomeAssistantAPIError()
    await tick(tasks)
    core.restart.assert_not_called()
    api.get_config.side_effect = fake._config  # pylint: disable=protected-access
    await tick(tasks)
    core.restart.assert_awaited_once()


async def test_core_still_booting_is_asked_again():
    """Test Core can still fall into recovery mode during a database migration."""
    tasks, core, fake = fake_in_recovery()
    api = tasks.sys_homeassistant.api
    api.get_api_state.return_value = APIState("NOT_RUNNING", True)
    fake.recovery = False
    fake.state = "NOT_RUNNING"
    await tick(tasks)
    assert api.get_config.await_count == 1

    api.get_api_state.return_value = APIState("RUNNING", False)
    fake.recovery = True
    fake.state = "RUNNING"
    await tick(tasks)
    core.restart.assert_awaited_once()


async def test_unknown_pid_changes_nothing():
    """Test launchctl failing once doesn't start anything."""
    tasks, core, _ = fake_in_recovery()
    core.instance.agent.status = AsyncMock(
        return_value=ServiceStatus(True, True, None, None)
    )
    await tick(tasks)
    core.restart.assert_not_called()
    assert saved_state() is None


async def test_safe_mode_nobody_asked_the_watchdog_for_is_left_alone():
    """Test a safe mode chosen by hand, with no run going, starts no run."""
    tasks, core = make_tasks()
    fake = FakeCore(tasks, core)
    fake.safe = True
    for _ in range(3):
        with after(MAX_WAIT * 2):
            await tick(tasks)
    core.restart.assert_not_called()
    assert saved_state() is None
    assert notifications(tasks) == []


async def test_recovery_mode_twice_goes_to_safe_mode():
    """Test safe mode gets past what the real Core was seen to get past."""
    tasks, core, fake = fake_in_recovery()
    fake.recovery_unless_safe = True
    await tick(tasks)
    core.restart.assert_awaited_once()
    core.rebuild.assert_awaited_once_with(safe_mode=True)
    assert fake.safe
    assert not fake.recovery
    state = saved_state()
    assert state["phase"] == "in_safe_mode"
    assert state["wait"] == FIRST_WAIT
    assert abs(state["retry_at"] - (time.time() + FIRST_WAIT)) < 5
    assert "custom integrations" in notifications(tasks)[0]
    assert "tried again and failed" in notifications(tasks)[0]


async def test_safe_mode_after_recovery_goes_on_as_usual():
    """Test the return from it, which lands in recovery again, backs off."""
    tasks, core, fake = fake_in_recovery()
    fake.recovery_unless_safe = True
    await tick(tasks)
    fake.clear_calls()
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_awaited_once()
    core.rebuild.assert_awaited_once_with(safe_mode=True)
    assert saved_state()["wait"] == FIRST_WAIT * 2


async def test_recovery_even_in_safe_mode_tries_every_15_minutes():
    """Test a bad state never becomes a restart loop, nor a long wait."""
    tasks, core, fake = fake_in_recovery()
    fake.recovery_on_start = True
    await tick(tasks)
    core.restart.assert_awaited_once()
    core.rebuild.assert_awaited_once()
    assert fake.safe
    assert fake.recovery
    state = saved_state()
    assert state["phase"] == "in_safe_mode"
    assert state["recovery"]
    assert abs(state["retry_at"] - (time.time() + RECOVERY_WAIT)) < 5
    assert "recovery mode" in notifications(tasks)[0]
    assert "Matter" in notifications(tasks)[0]
    assert "custom integrations" not in notifications(tasks)[0]

    fake.clear_calls()
    for _ in range(5):
        await tick(tasks)
    with after(RECOVERY_WAIT - 60):
        await tick(tasks)
    core.restart.assert_not_called()
    core.rebuild.assert_not_called()

    for round_ in range(1, 21):
        fake.clear_calls()
        now = time.time() + (RECOVERY_WAIT + 1) * round_
        with patch("time.time", return_value=now):
            await tick(tasks)
            await tick(tasks)
        core.restart.assert_awaited_once()
        core.rebuild.assert_awaited_once()
        assert abs(saved_state()["retry_at"] - (now + RECOVERY_WAIT)) < 5


async def test_time_in_recovery_does_not_lengthen_safe_mode():
    """Test hours in recovery don't give a working safe mode a day-long wait."""
    tasks, core, fake = fake_in_recovery()
    fake.recovery_on_start = True
    await tick(tasks)
    for round_ in range(1, 8):
        with after((RECOVERY_WAIT + 1) * round_):
            await tick(tasks)
    fake.recovery_on_start = False
    fake.recovery_unless_safe = True
    with after((RECOVERY_WAIT + 1) * 8):
        await tick(tasks)
    assert fake.safe
    assert not fake.recovery
    assert saved_state()["wait"] == FIRST_WAIT
    assert not saved_state()["recovery"]


def fake_with_bad_yaml() -> tuple[Tasks, MagicMock, FakeCore]:
    """Return a Core in recovery that no start can get out of: invalid YAML."""
    tasks, core, fake = fake_in_recovery()
    fake.recovery_on_start = True
    core.check_config.return_value = MagicMock(valid=False, log="bad")
    return tasks, core, fake


async def test_bad_yaml_tries_safe_mode_at_each_look_and_says_why():
    """Test a failing check isn't taken as proof, and no normal start is wasted."""
    tasks, core, fake = fake_with_bad_yaml()
    await tick(tasks)
    core.restart.assert_not_called()
    core.rebuild.assert_awaited_once_with(safe_mode=True)
    assert fake.safe
    assert fake.recovery
    state = saved_state()
    assert state["phase"] == "in_safe_mode"
    assert abs(state["retry_at"] - (time.time() + RECOVERY_WAIT)) < 5
    # The last message reaches the new Core and says why, not only that it failed.
    assert "recovery mode" in notifications(tasks)[-1]
    assert "does not pass the check" in notifications(tasks)[-1]
    assert "custom integrations" not in notifications(tasks)[-1].lower()

    fake.clear_calls()
    with after(RECOVERY_WAIT - 60):
        await tick(tasks)
    assert core.check_config.await_count == 1
    core.rebuild.assert_not_called()

    for round_ in range(1, 4):
        fake.clear_calls()
        now = time.time() + (RECOVERY_WAIT + 1) * round_
        with patch("time.time", return_value=now):
            await tick(tasks)
            await tick(tasks)
        core.restart.assert_not_called()
        core.rebuild.assert_awaited_once_with(safe_mode=True)
        assert abs(saved_state()["retry_at"] - (now + RECOVERY_WAIT)) < 5
    assert core.check_config.await_count == 4


async def test_typo_repaired_while_the_check_still_fails_gets_out():
    """Test a check that fails for another reason can't hold Core in recovery."""
    tasks, core, fake = fake_with_bad_yaml()
    await tick(tasks)
    assert fake.recovery
    fake.clear_calls()

    fake.recovery_on_start = False
    with after(RECOVERY_WAIT + 1):
        await tick(tasks)
    core.rebuild.assert_awaited_once_with(safe_mode=True)
    assert fake.safe
    assert not fake.recovery
    assert not saved_state()["recovery"]


async def test_bad_yaml_repaired_gets_a_normal_start():
    """Test the repair is picked up at the next look."""
    tasks, core, fake = fake_with_bad_yaml()
    await tick(tasks)
    fake.clear_calls()
    fake.recovery_on_start = False
    core.check_config.return_value = MagicMock(valid=True)
    with after(RECOVERY_WAIT + 1):
        await tick(tasks)
    core.restart.assert_awaited_once()
    assert not fake.recovery
    assert not fake.safe
    assert saved_state() is None
    assert "running normally again" in notifications(tasks)[0]


async def test_failing_check_doesnt_hold_back_what_safe_mode_can_fix():
    """Test a custom integration the check trips over, that safe mode gets past."""
    tasks, core, fake = fake_in_recovery()
    fake.recovery_unless_safe = True
    core.check_config.return_value = MagicMock(valid=False, log="bad")
    await tick(tasks)
    core.restart.assert_not_called()
    core.rebuild.assert_awaited_once_with(safe_mode=True)
    assert fake.safe
    assert not fake.recovery
    state = saved_state()
    assert state["wait"] == FIRST_WAIT
    assert abs(state["retry_at"] - (time.time() + FIRST_WAIT)) < 5
    # It says what happened, as no normal start was tried.
    assert "custom integrations" in notifications(tasks)[-1].lower()
    assert "does not pass the check" in notifications(tasks)[-1]

    # Still failing when the time comes: stays in safe mode, as ever.
    fake.clear_calls()
    with after(FIRST_WAIT + 1):
        await tick(tasks)
    core.restart.assert_not_called()
    core.rebuild.assert_not_called()
    assert "staying in safe mode" in notifications(tasks)[0]


async def test_rejected_safe_start_for_bad_yaml_tries_again():
    """Test a rejected attempt is neither lost nor announced as staying."""
    tasks, core, fake = fake_with_bad_yaml()
    real_rebuild = core.rebuild.side_effect
    core.rebuild.side_effect = HomeAssistantJobError()
    await tick(tasks)
    assert saved_state()["phase"] == "entering"
    assert notifications(tasks) == []
    core.rebuild.side_effect = real_rebuild
    await tick(tasks)
    assert fake.safe
    assert saved_state()["phase"] == "in_safe_mode"


async def test_recovery_repaired_and_restarted_by_hand():
    """Test the run ends, and the message says so."""
    tasks, core, fake = fake_with_bad_yaml()
    await tick(tasks)
    fake.clear_calls()
    fake.pid = 999
    fake.safe = False
    fake.recovery = False
    await tick(tasks)
    core.restart.assert_not_called()
    assert saved_state() is None
    assert "running normally again" in notifications(tasks)[0]


async def test_recovery_again_after_a_restart_by_hand_keeps_the_pace():
    """Test the new Core is picked up, without a restart straight on top of it."""
    tasks, core, fake = fake_with_bad_yaml()
    await tick(tasks)
    fake.clear_calls()
    fake.pid = 999
    fake.safe = False
    await tick(tasks)
    core.restart.assert_not_called()
    core.rebuild.assert_not_called()
    state = saved_state()
    assert state["pid"] == 999
    assert state["recovery"]
    assert abs(state["retry_at"] - (time.time() + RECOVERY_WAIT)) < 5


async def test_restart_by_hand_during_the_check_is_not_told_it_stayed():
    """Test no message about a Core that has gone goes to the new one."""
    tasks, core, fake = fake_with_bad_yaml()

    async def user_restarts() -> MagicMock:
        fake.pid = 999
        fake.recovery = False
        return MagicMock(valid=False, log="bad")

    core.check_config.side_effect = user_restarts
    await tick(tasks)
    core.rebuild.assert_not_called()
    assert notifications(tasks) == []


async def test_booting_core_is_not_called_normal():
    """Test a Core migrating its database hasn't reached any mode yet."""
    tasks, core, fake = await fake_in_safe_mode()
    api = tasks.sys_homeassistant.api
    api.get_api_state.return_value = APIState("NOT_RUNNING", True)
    fake.pid = 999
    fake.safe = False
    fake.state = "NOT_RUNNING"
    await tick(tasks)
    assert saved_state() is not None
    assert notifications(tasks) == []

    api.get_api_state.return_value = APIState("RUNNING", False)
    fake.state = "RUNNING"
    await tick(tasks)
    assert saved_state() is None
    assert "running normally again" in notifications(tasks)[0]


async def test_safe_start_that_changed_nothing_keeps_its_spacing():
    """Test a Core that survives the stop isn't sent to safe mode on every check."""
    tasks, core, fake = fake_in_recovery()
    fake.recovery_on_start = True
    await tasks._safe_mode.notice_recovery()  # pylint: disable=protected-access
    core.rebuild.side_effect = None
    await tasks._back_to_safe_mode()  # pylint: disable=protected-access
    assert HASS_WATCHDOG_NEXT_ATTEMPT in tasks._cache  # pylint: disable=protected-access
    await tasks._watchdog_homeassistant_api()  # pylint: disable=protected-access
    core.rebuild.assert_awaited_once()


async def test_backup_before_the_way_back_waits():
    """Test a backup that started during the question isn't restarted over."""
    tasks, core, fake = fake_in_recovery()
    fake.recovery_on_start = True
    await tasks._safe_mode.notice_recovery()  # pylint: disable=protected-access
    tasks.sys_core.state = CoreState.FREEZE
    await tasks._back_to_safe_mode()  # pylint: disable=protected-access
    core.rebuild.assert_not_called()


def rebuild_fails_after_the_marker(core: MagicMock, err: Exception) -> None:
    """Make a safe start fail with Core still up, once the marker is in place."""

    async def rebuild(*, safe_mode: bool = False) -> None:
        await _in_executor(marker().touch)
        raise err

    core.rebuild.side_effect = rebuild


async def test_failed_safe_start_with_core_still_up_says_so_and_waits():
    """Test a full disk during the rebuild neither kills the check, nor repeats."""
    tasks, core, fake = fake_in_recovery()
    fake.recovery_unless_safe = True
    rebuild_fails_after_the_marker(core, JobException())
    with patch("supervisor.misc.tasks.async_capture_exception", AsyncMock()):
        await tasks._watchdog_homeassistant_api()  # pylint: disable=protected-access
    core.rebuild.assert_awaited_once()
    assert not marker().exists()
    state = saved_state()
    assert state["phase"] == "in_safe_mode"
    assert state["pid"] == fake.pid
    assert abs(state["retry_at"] - (time.time() + RECOVERY_WAIT)) < 5
    assert "staying in recovery mode" in notifications(tasks)[-1]

    await tick(tasks)
    core.rebuild.assert_awaited_once()

    fake.clear_calls()
    with after(RECOVERY_WAIT + 1):
        await tick(tasks)
    core.restart.assert_awaited_once()
    core.rebuild.assert_awaited_once()


async def test_rejected_safe_start_is_not_a_failure_to_report():
    """Test another job holding the lock costs one retry, not 15 minutes."""
    tasks, core, fake = fake_in_recovery()
    fake.recovery_unless_safe = True
    core.rebuild.side_effect = HomeAssistantJobError()
    await tick(tasks)
    assert saved_state()["phase"] == "entering"
    assert not any("staying" in note for note in notifications(tasks))


async def test_state_from_before_is_corrected_by_what_core_says():
    """Test a run saved without the mode gets the recovery pace and message."""
    tasks, core, fake = fake_with_bad_yaml()
    fake.safe = True
    state_file().write_text(
        json.dumps(
            {
                "phase": "in_safe_mode",
                "pid": CORE_PID,
                "since": 1.0,
                "retry_at": time.time() - 1,
                "wait": FIRST_WAIT,
            }
        )
    )
    await tasks.load()
    await tick(tasks)
    assert "recovery mode" in notifications(tasks)[-1]
    assert "custom integrations" not in notifications(tasks)[-1].lower()
    assert abs(saved_state()["retry_at"] - (time.time() + RECOVERY_WAIT)) < 5


async def test_backup_during_the_check_holds_the_safe_start():
    """Test no restart, no message, and it goes ahead soon after."""
    tasks, core, _ = fake_with_bad_yaml()

    async def backup_starts() -> MagicMock:
        tasks.sys_core.state = CoreState.FREEZE
        return MagicMock(valid=False, log="bad")

    core.check_config.side_effect = backup_starts
    await tick(tasks)
    core.rebuild.assert_not_called()
    assert notifications(tasks) == []

    core.check_config.side_effect = None
    core.check_config.return_value = MagicMock(valid=False, log="bad")
    tasks.sys_core.state = CoreState.RUNNING
    await tick(tasks)
    core.rebuild.assert_awaited_once()


async def test_safe_start_for_bad_yaml_that_comes_up_normal_ends_the_run():
    """Test a repair during the check isn't told it failed."""
    tasks, core, fake = fake_with_bad_yaml()

    async def rebuild(*, safe_mode: bool = False) -> None:
        fake.pid += 1
        fake.safe = False
        fake.recovery = False

    core.rebuild.side_effect = rebuild
    await tick(tasks)
    assert saved_state() is None
    assert "running normally again" in notifications(tasks)[-1]


async def test_supervisor_restart_during_a_safe_start_from_recovery():
    """Test Core coming up after the Supervisor did is picked up, and not restarted."""
    tasks, core, fake = fake_with_bad_yaml()
    real_rebuild = core.rebuild.side_effect

    async def supervisor_dies(*, safe_mode: bool = False) -> None:
        await real_rebuild(safe_mode=safe_mode)
        raise asyncio.CancelledError

    core.rebuild.side_effect = supervisor_dies
    with pytest.raises(asyncio.CancelledError):
        await tick(tasks)
    assert saved_state()["phase"] == "entering"
    fake.clear_calls()
    core.rebuild.side_effect = real_rebuild

    restarted = Tasks(tasks.coresys)
    await restarted.load()
    await tick(restarted)
    core.rebuild.assert_not_called()
    core.restart.assert_not_called()
    assert saved_state()["phase"] == "in_safe_mode"
    assert saved_state()["pid"] == fake.pid
    assert saved_state()["recovery"]


async def test_unexpected_error_starting_core_is_spaced():
    """Test a Core that can't even be started is tried at the normal pace."""
    tasks, core = make_tasks(running=False)
    core.start.side_effect = JobException()
    with patch("supervisor.misc.tasks.async_capture_exception", AsyncMock()):
        await tasks._watchdog_homeassistant_api()
        await tasks._watchdog_homeassistant_api()
    core.start.assert_awaited_once()


async def test_unexpected_error_elsewhere_still_raises():
    """Test other platforms keep upstream's behaviour for this."""
    tasks, core = make_tasks(running=False, api_ok=False)
    tasks.sys_homeassistant.api.check_api_state = AsyncMock(return_value=False)
    core.is_running = AsyncMock(return_value=True)
    core.restart.side_effect = JobException()
    with (
        patch("supervisor.misc.tasks.IS_MACOS", False),
        patch("supervisor.misc.tasks.async_capture_exception", AsyncMock()),
    ):
        await tasks._watchdog_homeassistant_api()
        with pytest.raises(JobException):
            await tasks._watchdog_homeassistant_api()


@pytest.mark.parametrize("error", [HomeAssistantError(), TimeoutError(), OSError()])
async def test_recovery_with_a_broken_check_restarts_anyway(error):
    """Test a check tool that can't run can't leave Core stuck for good."""
    tasks, core, fake = fake_in_recovery()
    core.check_config.side_effect = error
    await tick(tasks)
    core.restart.assert_awaited_once()
    assert not fake.recovery


async def test_failed_restart_from_recovery_goes_to_safe_mode():
    """Test a crash on the way back counts the same as recovery mode again."""
    tasks, core, fake = fake_in_recovery()
    fake.broken = True
    await tick(tasks)
    core.restart.assert_awaited_once()
    assert fake.pid is None
    core.rebuild.assert_not_called()

    await tick(tasks)
    core.rebuild.assert_awaited_once_with(safe_mode=True)
    assert fake.safe
    assert saved_state()["wait"] == FIRST_WAIT


async def test_recovery_core_that_wont_stop_keeps_the_pace():
    """Test the wait stays short, and the message is about recovery mode."""
    tasks, core, fake = fake_in_recovery()
    fake.wont_stop = True
    await tick(tasks)
    core.restart.assert_awaited_once()
    state = saved_state()
    assert state["phase"] == "in_safe_mode"
    assert state["wait"] == 0
    assert abs(state["retry_at"] - (time.time() + RECOVERY_WAIT)) < 5
    assert "staying in recovery mode" in notifications(tasks)[0]

    await tick(tasks)
    core.restart.assert_awaited_once()


async def test_new_core_in_recovery_while_entering_is_not_a_restart_loop():
    """Test a crashed safe start, then a normal start landing in recovery, waits."""
    tasks, core, fake = fake_in_recovery()
    await tasks._safe_mode.notice_recovery()  # pylint: disable=protected-access
    await tasks._safe_mode.entering()  # pylint: disable=protected-access
    fake.clear_calls()
    fake.pid = 999
    assert not fake.safe
    for _ in range(3):
        await tick(tasks)
    core.rebuild.assert_not_called()
    core.restart.assert_not_called()
    state = saved_state()
    assert state["phase"] == "in_safe_mode"
    assert state["pid"] == 999
    assert abs(state["retry_at"] - (time.time() + RECOVERY_WAIT)) < 5


async def test_same_core_in_recovery_after_a_rejected_safe_start_tries_again():
    """Test a safe start that never began isn't taken for having landed."""
    tasks, core, fake = fake_in_recovery()
    fake.recovery_unless_safe = True
    real_rebuild = core.rebuild.side_effect
    core.rebuild.side_effect = HomeAssistantJobError()
    await tick(tasks)
    assert saved_state()["phase"] == "entering"
    core.rebuild.side_effect = real_rebuild
    await tick(tasks)
    assert core.rebuild.await_count == 2
    assert fake.safe
    assert saved_state()["phase"] == "in_safe_mode"


async def test_stale_marker_on_the_way_back_from_recovery_is_not_a_restart_loop():
    """Test doubling a wait that was never set doesn't retry at once."""
    tasks, core, fake = fake_in_recovery()
    marker().touch()
    await tick(tasks)
    core.restart.assert_awaited_once()
    assert fake.safe
    assert saved_state()["wait"] == FIRST_WAIT
    assert abs(saved_state()["retry_at"] - (time.time() + FIRST_WAIT)) < 5
    await tick(tasks)
    core.restart.assert_awaited_once()


async def test_recovery_during_a_backup_waits_for_the_backup():
    """Test a restore from the recovery UI isn't restarted over."""
    tasks, core, _ = fake_in_recovery()
    tasks.sys_core.state = CoreState.FREEZE
    await tick(tasks)
    core.restart.assert_not_called()
    assert saved_state() is None
    tasks.sys_core.state = CoreState.RUNNING
    await tick(tasks)
    core.restart.assert_awaited_once()


async def test_recovery_run_survives_a_supervisor_restart():
    """Test the wait and the mode are kept on disk."""
    tasks, core, fake = fake_with_bad_yaml()
    await tick(tasks)
    fake.clear_calls()

    restarted = Tasks(tasks.coresys)
    await restarted.load()
    await tick(restarted)
    assert core.check_config.await_count == 1
    core.rebuild.assert_not_called()
    fake.recovery_on_start = False
    core.check_config.return_value = MagicMock(valid=True)
    with after(RECOVERY_WAIT + 1):
        await tick(restarted)
    core.restart.assert_awaited_once()


async def test_recovery_found_after_a_supervisor_restart():
    """Test a Core the last Supervisor had already seen is asked again."""
    tasks, core, fake = fake_in_recovery()
    fake.recovery = False
    await tick(tasks)
    fake.recovery = True
    await tick(tasks)
    core.restart.assert_not_called()

    restarted = Tasks(tasks.coresys)
    await restarted.load()
    await tick(restarted)
    core.restart.assert_awaited_once()


async def test_state_saved_before_recovery_was_handled_still_loads():
    """Test a run from the version before this one isn't dropped."""
    tasks, _, _ = fake_in_recovery()
    state_file().write_text(
        json.dumps(
            {
                "phase": "in_safe_mode",
                "pid": CORE_PID,
                "since": 1.0,
                "retry_at": time.time() + 600,
                "wait": FIRST_WAIT,
            }
        )
    )
    await tasks.load()
    run = tasks._safe_mode._run  # pylint: disable=protected-access
    assert run.phase == Phase.IN_SAFE_MODE
    assert not run.recovery


async def test_recovery_is_left_alone_elsewhere():
    """Test other platforms don't ask, and don't restart."""
    tasks, core, _ = fake_in_recovery()
    tasks.sys_homeassistant.api.check_api_state = AsyncMock(return_value=True)
    with patch("supervisor.misc.tasks.IS_MACOS", False):
        await tick(tasks)
    core.restart.assert_not_called()
    tasks.sys_homeassistant.api.get_config.assert_not_called()
