"""Test that the native watchdog keeps Core up.

These run without the coresys fixture, which needs a D-Bus daemon.
"""

import asyncio
from pathlib import Path
import sys
from unittest.mock import AsyncMock, MagicMock, patch

from awesomeversion import AwesomeVersion
import pytest

from supervisor.const import CoreState
from supervisor.exceptions import HomeAssistantError
from supervisor.misc.tasks import Tasks
from supervisor.native.launchd import LaunchAgent, LaunchdError, ServiceStatus


def make_tasks(*, running: bool = True, api_ok: bool = True) -> tuple[Tasks, MagicMock]:
    """Return Tasks on a fake coresys, and the fake Core."""
    coresys = MagicMock()
    coresys.core.state = CoreState.RUNNING
    homeassistant = coresys.homeassistant
    homeassistant.watchdog = True
    homeassistant.error_state = False
    homeassistant.version = AwesomeVersion("2026.10.2")
    homeassistant.api.check_api_state = AsyncMock(return_value=api_ok)
    core = homeassistant.core
    core.in_progress = False
    core.is_running = AsyncMock(return_value=running)
    core.instance.stop_requested = False
    core.start = AsyncMock()
    core.restart = AsyncMock()
    core.rebuild = AsyncMock()
    return Tasks(coresys), core


async def tick(tasks: Tasks) -> None:
    """Run the watchdog once, as the scheduler does."""
    with patch("supervisor.misc.tasks.async_capture_exception", AsyncMock()):
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
    await tick(tasks)
    await tick(tasks)
    assert core.restart.await_count == 2


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
