"""Test native process stats."""

import os
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from supervisor.exceptions import (
    DockerContainerNotRunningError,
    DockerError,
    DockerStatsTimeoutError,
)
from supervisor.native import stats
from supervisor.native.homeassistant import NativeHomeAssistant
from supervisor.native.launchd import ServiceStatus


@pytest.mark.parametrize(
    ("value", "seconds"),
    [
        pytest.param("0:00.02", 0.02, id="seconds"),
        pytest.param("21:46.13", 1306.13, id="minutes"),
        pytest.param("69:30.47", 4170.47, id="over-an-hour-as-minutes"),
        pytest.param("1:02:03.50", 3723.5, id="hours"),
        pytest.param("2-01:00:00.00", 176400.0, id="days"),
    ],
)
def test_parse_cputime(value: str, seconds: float) -> None:
    """Test every form ps prints cumulative CPU time in."""
    assert stats._parse_cputime(value) == round(seconds * 1e9)


def test_tree_takes_every_descendant_and_nothing_else() -> None:
    """Test a service counts its children and grandchildren, not its siblings."""
    table = {
        1: (0, 0, 0),
        10: (1, 0, 0),  # the service
        11: (10, 0, 0),
        12: (11, 0, 0),
        20: (1, 0, 0),  # another service
    }
    assert sorted(stats._tree(table, 10)) == [10, 11, 12]
    assert stats._tree(table, 99) == []


def test_sample_prefers_footprint_and_falls_back_to_ps() -> None:
    """Test own processes report their footprint, root ones ps' numbers."""
    table = {10: (1, 1000, 5), 11: (10, 2000, 7)}
    with (
        patch.object(stats, "_ps_table", return_value=table),
        patch.object(stats, "_rusage", side_effect=[(300, 40), None]),
        patch.object(stats, "_host_cpu_ns", return_value=10_000),
    ):
        assert stats._sample(10) == (300 + 2000, 40 + 7, 10_000)


def test_docker_shape_gives_a_windowed_cpu_percent() -> None:
    """Test two samples turn into the same percentages Docker's would."""
    shape = stats._docker_shape((512, 300, 2000), (512, 100, 1000))
    result = stats.DockerStats(shape)
    assert result.cpu_percent == 20.0
    assert result.memory_usage == 512
    assert result.memory_limit == stats._memory_total()
    assert result.online_cpus == os.cpu_count()


async def test_one_shot_has_counters_but_no_percent() -> None:
    """Test a one-shot sample, as the v2 API asks for, has no CPU window."""
    with patch.object(stats, "_sample", return_value=(512, 300, 2000)):
        result = await stats.process_stats(1, name="app", one_shot=True)
    assert result is not None
    assert (result.cpu_percent, result.cpu_usage, result.cpu_system_usage) == (
        None,
        300,
        2000,
    )


async def test_a_vanished_process_is_not_running() -> None:
    """Test a pid gone before or during the window reads as not running."""
    with (
        patch.object(stats, "_sample", return_value=None),
        pytest.raises(DockerContainerNotRunningError),
    ):
        await stats.process_stats(1, name="app", one_shot=True)
    with (
        patch.object(stats, "_sample", side_effect=[(1, 1, 1), None]),
        patch.object(stats, "_SAMPLE_SECONDS", 0),
        pytest.raises(DockerContainerNotRunningError),
    ):
        await stats.process_stats(1, name="app")


@pytest.mark.parametrize(
    ("failure", "raised"),
    [
        pytest.param(
            subprocess.TimeoutExpired("ps", 10), DockerStatsTimeoutError, id="timeout"
        ),
        pytest.param(
            subprocess.CalledProcessError(1, "ps"), DockerError, id="ps-failed"
        ),
        pytest.param(FileNotFoundError("/bin/ps"), DockerError, id="no-ps"),
        pytest.param(ValueError("garbage"), DockerError, id="unparsable"),
    ],
)
async def test_failures_map_to_docker_errors(
    failure: Exception, raised: type[Exception]
) -> None:
    """Test a failing ps becomes the errors the callers map, not a bare 500."""
    with (
        patch.object(stats, "_ps_table", side_effect=failure),
        pytest.raises(raised),
    ):
        await stats.process_stats(1, name="app", one_shot=True)


def test_host_cpu_time_grows_and_never_wraps() -> None:
    """Test the host CPU total is uptime times cores, as on Linux."""
    first = stats._host_cpu_ns()
    assert first > 0
    assert stats._host_cpu_ns() >= first
    with patch.object(stats.time, "monotonic_ns", return_value=60 * 86400 * 10**9):
        assert stats._host_cpu_ns() == 60 * 86400 * 10**9 * os.cpu_count()


async def test_this_process_on_the_real_host() -> None:
    """Test the real sampling against the test process and a child of it."""
    child = subprocess.Popen(["/bin/sleep", "30"])
    try:
        result = await stats.process_stats(os.getpid(), name="tests", one_shot=True)
    finally:
        child.kill()
        child.wait()
    assert result is not None
    assert result.memory_usage > 10 * 2**20
    assert 0 < result.cpu_usage < result.cpu_system_usage


@pytest.mark.parametrize(
    "status",
    [
        pytest.param(ServiceStatus(False, False, None, None), id="not-loaded"),
        pytest.param(ServiceStatus(True, False, None, 1), id="crashed"),
    ],
)
async def test_core_stats_when_core_is_down(status: ServiceStatus) -> None:
    """Test Core stats raise the error the API turns into a message."""
    hass = MagicMock()
    hass.agent.status = AsyncMock(return_value=status)
    with pytest.raises(DockerContainerNotRunningError):
        await NativeHomeAssistant.stats(hass)
