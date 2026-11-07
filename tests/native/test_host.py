"""Test native host information."""

import asyncio
from pathlib import Path
from unittest.mock import MagicMock, patch
from zoneinfo import ZoneInfo

import pytest

from supervisor.native import host


def _runs(outputs: dict[str, str | None]):
    def run(*args: str) -> str | None:
        return outputs.get(args[0])

    return run


@pytest.mark.parametrize(
    ("battery", "chassis"),
    [
        pytest.param("Now drawing from 'AC Power'", "desktop", id="mac-mini"),
        pytest.param(
            "Now drawing from 'AC Power'\n -InternalBattery-0 (id=1)\t100%",
            "laptop",
            id="macbook",
        ),
    ],
)
def test_facts(tmp_path: Path, battery: str, chassis: str) -> None:
    """Test the facts come from the tools and the localtime link."""
    localtime = tmp_path / "localtime"
    localtime.symlink_to("/var/db/timezone/zoneinfo/America/New_York")
    outputs = {
        "/usr/sbin/scutil": "Example-Mac-mini",
        "/usr/bin/sw_vers": "26.5.2",
        "/usr/bin/pmset": battery,
    }
    with (
        patch.object(host, "_run", side_effect=_runs(outputs)),
        patch.object(host, "_LOCALTIME", localtime),
        patch.object(host, "boot_time", return_value=1786021180),
        patch.object(host.platform, "release", return_value="25.5.0"),
    ):
        facts = host.read_host_facts()
    assert facts == {
        "hostname": "Example-Mac-mini",
        "operating_system": "macOS 26.5.2",
        "kernel": "25.5.0",
        "chassis": chassis,
        "timezone": "America/New_York",
        "boot_timestamp": 1786021180 * 1_000_000,
    }


def test_facts_when_tools_fail(tmp_path: Path) -> None:
    """Test missing answers stay unknown instead of failing."""
    with (
        patch.object(host, "_run", return_value=None),
        patch.object(host, "_LOCALTIME", tmp_path / "missing"),
        patch.object(host, "boot_time", return_value=None),
    ):
        facts = host.read_host_facts()
    assert facts["hostname"] is None
    assert facts["operating_system"] is None
    assert facts["timezone"] is None
    assert facts["boot_timestamp"] is None
    assert facts["chassis"] == "desktop"


def test_facts_on_this_mac() -> None:
    """Test the real tools answer on the host running the tests."""
    facts = host.read_host_facts()
    assert str(facts["operating_system"]).startswith("macOS ")
    assert facts["hostname"]
    assert facts["boot_timestamp"]


async def test_info_center_serves_the_facts() -> None:
    """Test the info center reads facts on update and reports them."""
    loop = asyncio.get_running_loop()
    coresys = MagicMock()
    coresys.run_in_executor = lambda func, *args: loop.run_in_executor(
        None, func, *args
    )
    center = host.NativeInfoCenter(coresys)
    facts = {"hostname": "mini", "timezone": "Europe/Berlin", "boot_timestamp": 5}
    with patch.object(host, "read_host_facts", return_value=facts):
        await center.update()
    assert center.hostname == "mini"
    assert center.timezone_tzinfo == ZoneInfo("Europe/Berlin")
    assert center.boot_timestamp == 5
    assert center.broadcast_mdns is True
    assert center.use_ntp is None
