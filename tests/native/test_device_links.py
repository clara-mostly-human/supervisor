"""Test device options naming a by-id link from another host or data folder.

On macOS the Supervisor keeps the by-id links in its data folder, so a path
saved in a backup names that folder. Restored elsewhere (another user, another
folder, or a Linux backup with /dev/serial/by-id) the dongle is found by the
link name, as the path itself doesn't exist.
"""

from pathlib import Path
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol

from supervisor.apps.app import App
from supervisor.apps.options import AppOptions
from supervisor.hardware.manager import HardwareManager
from supervisor.native import hardware

NAME = "usb-SONOFF_SONOFF_Dongle_Plus_MG24_5a1e0001c0ffee00d00d0000beef0001-if00-port0"
OTHER = "usb-SONOFF_SONOFF_Dongle_Plus_MG24_5a1e0002c0ffee00d00d0000beef0002-if00-port0"
GENERIC = "usb-Silicon_Labs_CP2102_USB_to_UART_Bridge_Controller_0001-if00-port0"
ELSEWHERE = f"/Users/example/ha-supervisor/data/dev/serial/by-id/{NAME}"
SCHEMA = {"device": "device(subsystem=tty)", "baudrate": "int"}


@pytest.fixture
def coresys(tmp_path: Path) -> MagicMock:
    """Return a coresys with this host's two dongles and a generic CP2102."""
    links = tmp_path / "data" / "dev" / "serial" / "by-id"
    coresys = MagicMock()
    coresys.hardware = HardwareManager(coresys, None)  # type: ignore[arg-type]
    for node, name, serial in (
        ("cu.usbserial-140", NAME, "5a1e0001c0ffee00d00d0000beef0001"),
        ("cu.usbserial-1340", OTHER, "5a1e0002c0ffee00d00d0000beef0002"),
        ("cu.usbserial-9", GENERIC, "0001"),
    ):
        path = Path("/dev", node)
        coresys.hardware.update_device(
            hardware.NativeDevice(
                node,
                path,
                path,
                "tty",
                None,
                [links / name],
                {"ID_SERIAL_SHORT": serial},
                [],
            )  # fmt: skip
        )
    coresys.links = links
    return coresys


@pytest.mark.parametrize(
    ("path", "name"),
    [
        (ELSEWHERE, NAME),
        (f"/dev/serial/by-id/{NAME}", NAME),
        ("/dev/cu.usbserial-140", None),
        (f"/somewhere/by-id/{NAME}", None),
        (f"/dev/serial/by-path/{NAME}", None),
    ],
)
def test_by_id_link_name(path: str, name: str | None) -> None:
    """Test only paths ending in dev/serial/by-id/<name> count as by-id links."""
    assert hardware.by_id_link_name(Path(path)) == name


@pytest.mark.parametrize(
    "saved", [ELSEWHERE, f"/dev/serial/by-id/{NAME}"], ids=["other-folder", "linux"]
)
def test_option_from_elsewhere_names_this_hosts_link(
    coresys: MagicMock, saved: str
) -> None:
    """Test a by-id path that doesn't exist here resolves by its link name."""
    options = AppOptions(coresys, SCHEMA, "OTBR", "otbr")
    result = options({"device": saved, "baudrate": 460800})
    assert result["device"] == str(coresys.links / NAME)
    assert [device.name for device in options.devices] == ["cu.usbserial-140"]


@pytest.mark.parametrize(
    "saved",
    [
        "/elsewhere/data/dev/serial/by-id/usb-unplugged-if00-port0",
        "/dev/cu.usbserial-99",
        "/elsewhere/data/dev/serial/by-path/" + NAME,
        f"/elsewhere/data/dev/serial/by-id/{GENERIC}",
    ],
    ids=["unplugged", "plain-node", "by-path", "generic-serial"],
)
def test_option_still_refuses_what_isnt_here(coresys: MagicMock, saved: str) -> None:
    """Test a missing dongle, a non by-id path or a generic serial stays invalid."""
    with pytest.raises(vol.Invalid, match="does not exist"):
        AppOptions(coresys, SCHEMA, "OTBR", "otbr")({"device": saved, "baudrate": 1})


def test_linux_is_unchanged(coresys: MagicMock) -> None:
    """Test the link-name match is macOS only; Linux keeps upstream's check."""
    with (
        patch("supervisor.apps.options.IS_MACOS", False),
        pytest.raises(vol.Invalid, match="does not exist"),
    ):
        AppOptions(coresys, SCHEMA, "OTBR", "otbr")(
            {"device": ELSEWHERE, "baudrate": 1}
        )


def _app(coresys: MagicMock, stored: dict) -> MagicMock:
    """Return an app whose stored (user) options are `stored`."""
    app = MagicMock()
    app.slug = "otbr"
    app.persist = {"options": stored}
    app.options = "untouched"
    app.schema = AppOptions(coresys, SCHEMA, "OTBR", "otbr")
    app.sys_hardware = coresys.hardware
    app.save_persist = AsyncMock()
    return app


async def test_restore_saves_this_hosts_link_and_nothing_else(
    coresys: MagicMock,
) -> None:
    """Test only the moved path changes: no defaults merged in, unknowns kept."""
    stored = {"device": ELSEWHERE, "baudrate": "460800", "retired_option": True}
    app = _app(coresys, stored)
    await App._native_adopt_device_links(app)  # noqa: SLF001
    assert app.options == {
        "device": str(coresys.links / NAME),
        "baudrate": "460800",
        "retired_option": True,
    }
    assert stored["device"] == ELSEWHERE  # the stored dict wasn't edited in place
    app.save_persist.assert_awaited_once()


@pytest.mark.parametrize(
    "device",
    [
        pytest.param("here", id="already-this-hosts-link"),
        pytest.param(
            "/elsewhere/data/dev/serial/by-id/usb-unplugged-if00-port0",
            id="dongle-not-here",
        ),
        pytest.param("!secret thread_dongle", id="secret"),
        pytest.param(f"/elsewhere/data/dev/serial/by-id/{GENERIC}", id="generic"),
    ],
)
async def test_restore_leaves_options_alone(coresys: MagicMock, device: str) -> None:
    """Test nothing is saved when the link is right, missing, a secret or generic."""
    if device == "here":
        device = str(coresys.links / NAME)
    app = _app(coresys, {"device": device, "baudrate": 460800})
    await App._native_adopt_device_links(app)  # noqa: SLF001
    assert app.options == "untouched"
    app.save_persist.assert_not_awaited()
