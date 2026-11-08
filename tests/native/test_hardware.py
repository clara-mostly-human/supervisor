"""Test native (macOS) hardware discovery."""

from pathlib import Path
import plistlib
import subprocess
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
import voluptuous as vol

from supervisor.apps.options import AppOptions, UiOptions
from supervisor.hardware import manager as hw_manager
from supervisor.hardware.data import Device
from supervisor.hardware.manager import HardwareManager
from supervisor.hardware.monitor import NATIVE_POLL_SECONDS, HwMonitor
from supervisor.native import hardware

SERIAL = "5a1e0001c0ffee00d00d0000beef0001"
BY_ID = f"usb-SONOFF_SONOFF_Dongle_Plus_MG24_{SERIAL}-if00-port0"


def _device(name: str) -> Device:
    node = Path("/dev", name)
    return Device(name, node, node, "tty", None, [], {}, [])


def _ioreg(callout: str, serial: str | None = SERIAL) -> bytes:
    """Return an ioreg -a tree: hub > dongle > interface > driver > port."""
    dongle = {
        "idVendor": 0x10C4,
        "idProduct": 0xEA60,
        "locationID": 0x03110000,
        "kUSBVendorString": "SONOFF",
        "kUSBProductString": "SONOFF Dongle Plus MG24",
        "IORegistryEntryChildren": [
            {
                "idVendor": 0x10C4,
                "bInterfaceNumber": 0,
                "IORegistryEntryChildren": [
                    {"IORegistryEntryChildren": [{"IOCalloutDevice": callout}]}
                ],
            }
        ],
    }
    if serial:
        dongle["kUSBSerialNumberString"] = serial
    hub = {
        "idVendor": 0x05AC,
        "locationID": 0x03100000,
        "kUSBProductString": "USB2.0 Hub",
        "kUSBSerialNumberString": "HUB",
        "IORegistryEntryChildren": [dongle],
    }
    return plistlib.dumps([hub])


def test_import_devices_returns_the_native_devices(tmp_path: Path) -> None:
    """Test load() gets the devices back, since it stores what this returns."""
    devices = [_device("cu.usbserial-1120"), _device("cu.usbserial-3110")]
    manager = MagicMock()
    manager.sys_config.path_supervisor = tmp_path
    manager.native_complete = True
    manager._native_linked = before = {}
    with patch.object(
        hw_manager,
        "native_devices",
        return_value=hardware.Listing(devices, False, {Path("/dev/x"): (1, "l")}),
    ) as found:
        result = HardwareManager._import_devices(manager)  # noqa: SLF001
    assert result == {device.name: device for device in devices}
    found.assert_called_once_with(
        tmp_path / "dev" / "serial" / "by-id", before, warn=True
    )
    assert manager.native_complete is False
    assert manager._native_linked == {Path("/dev/x"): (1, "l")}


def test_usb_info_takes_the_dongle_not_the_hub() -> None:
    """Test the nearest USB device above the port is the one reported."""
    run = MagicMock(stdout=_ioreg("/dev/cu.usbserial-3110"))
    with patch.object(hardware.subprocess, "run", return_value=run):
        info = hardware._usb_serial_info()
    assert info["/dev/cu.usbserial-3110"]["kUSBProductString"] == (
        "SONOFF Dongle Plus MG24"
    )
    assert info["/dev/cu.usbserial-3110"]["_interface"] == 0


def test_attributes_and_by_id_name_match_linux() -> None:
    """Test the udev attribute names and the by-id name Linux would give."""
    run = MagicMock(stdout=_ioreg("/dev/cu.usbserial-3110"))
    with patch.object(hardware.subprocess, "run", return_value=run):
        usb = hardware._usb_serial_info()["/dev/cu.usbserial-3110"]
    attributes, name = hardware._attributes(Path("/dev/cu.usbserial-3110"), usb)
    assert name == BY_ID
    assert attributes == {
        "DEVNAME": "/dev/cu.usbserial-3110",
        "SUBSYSTEM": "tty",
        "ID_BUS": "usb",
        "ID_VENDOR_ID": "10c4",
        "ID_MODEL_ID": "ea60",
        "ID_USB_INTERFACE_NUM": "00",
        "LOCATION_ID": "0x03110000",
        "ID_VENDOR": "SONOFF",
        "ID_MODEL": "SONOFF_Dongle_Plus_MG24",
        "ID_SERIAL_SHORT": SERIAL,
        "ID_SERIAL": f"SONOFF_SONOFF_Dongle_Plus_MG24_{SERIAL}",
    }


def test_no_serial_number_means_no_by_id_link() -> None:
    """Test a dongle without a serial number gets no link, as it isn't unique."""
    run = MagicMock(stdout=_ioreg("/dev/cu.usbserial-3110", serial=None))
    with patch.object(hardware.subprocess, "run", return_value=run):
        usb = hardware._usb_serial_info()["/dev/cu.usbserial-3110"]
    _, name = hardware._attributes(Path("/dev/cu.usbserial-3110"), usb)
    assert name is None


def test_links_follow_a_port_move(tmp_path: Path) -> None:
    """Test a dongle moved to another port keeps its by-id path."""
    links = tmp_path / "by-id"
    hardware.sync_links(links, {BY_ID: Path("/dev/cu.usbserial-3110")})
    assert (links / BY_ID).readlink() == Path("/dev/cu.usbserial-3110")
    hardware.sync_links(links, {BY_ID: Path("/dev/cu.usbserial-2110")})
    assert (links / BY_ID).readlink() == Path("/dev/cu.usbserial-2110")
    assert sorted(path.name for path in links.iterdir()) == [BY_ID]


def test_links_of_unplugged_dongles_go_and_other_files_stay(tmp_path: Path) -> None:
    """Test a dongle that's gone loses its link; files that aren't links stay."""
    links = tmp_path / "by-id"
    hardware.sync_links(links, {BY_ID: Path("/dev/cu.usbserial-3110")})
    (links / "notes.txt").write_text("mine")
    hardware.sync_links(links, {})
    assert sorted(path.name for path in links.iterdir()) == ["notes.txt"]


def test_list_devices_reports_the_link_as_by_id(tmp_path: Path) -> None:
    """Test the device carries its link, and identical names get none."""
    links = tmp_path / "by-id"
    run = MagicMock(stdout=_ioreg("/dev/cu.usbserial-3110"))
    with (
        patch.object(hardware, "serial_nodes", return_value={"cu.usbserial-3110"}),
        patch.object(hardware.subprocess, "run", return_value=run),
    ):
        listing = hardware.list_devices(links)
    (device,) = listing.devices
    assert listing.complete
    assert device.by_id == links / BY_ID
    assert device.links == [links / BY_ID]

    twice = {"/dev/cu.a": {}, "/dev/cu.b": {}}
    with (
        patch.object(hardware, "serial_nodes", return_value={"cu.a", "cu.b"}),
        patch.object(hardware, "_usb_serial_info", return_value=twice),
        patch.object(hardware, "_attributes", return_value=({"DEVNAME": "x"}, BY_ID)),
    ):
        devices = hardware.list_devices(links).devices
    assert [device.by_id for device in devices] == [None, None]


def test_ioreg_failure_still_lists_the_ports(tmp_path: Path) -> None:
    """Test the ports are listed even if IOKit can't be read."""
    with (
        patch.object(hardware, "serial_nodes", return_value={"cu.usbserial-3110"}),
        patch.object(hardware.subprocess, "run", side_effect=OSError("no ioreg")),
    ):
        listing = hardware.list_devices(tmp_path / "by-id")
    (device,) = listing.devices
    assert device.name == "cu.usbserial-3110"
    assert device.by_id is None
    assert not listing.complete


NODE = "cu.usbserial-3110"


def _listing(
    links: Path,
    linked: dict | None,
    number: int,
    **run: object,
) -> hardware.Listing:
    """List NODE (device number `number`) with ioreg doing `run`."""
    if not run:
        run = {"return_value": MagicMock(stdout=_ioreg(f"/dev/{NODE}"))}
    with (
        patch.object(hardware, "serial_nodes", return_value={NODE}),
        patch.object(
            hardware, "_device_numbers", return_value={Path("/dev", NODE): number}
        ),
        patch.object(hardware.subprocess, "run", **run),
    ):
        return hardware.list_devices(links, linked)


IOKIT_FAILURES = [
    pytest.param({"side_effect": subprocess.TimeoutExpired("ioreg", 15)}, id="timeout"),
    pytest.param(
        {"return_value": MagicMock(stdout=b"<plist><array><dict>")}, id="garbled"
    ),
]


@pytest.mark.parametrize("failure", IOKIT_FAILURES)
def test_ioreg_failure_keeps_the_links(tmp_path: Path, failure: dict) -> None:
    """Test links survive a failed IOKit read, which would make all look stale.

    Apps' device options name the links, so dropping them stops those apps.
    """
    links = tmp_path / "by-id"
    good = _listing(links, None, 7)
    assert good.complete
    assert good.linked == {Path("/dev", NODE): (7, BY_ID)}
    (links / "usb-unplugged-if00-port0").symlink_to("/dev/cu.gone")
    before = sorted(links.iterdir())

    for _ in range(2):  # and again, from the incomplete listing
        listing = _listing(links, good.linked, 7, **failure)
        (device,) = listing.devices
        assert not listing.complete
        assert device.by_id == links / BY_ID
        assert listing.linked == good.linked
        assert sorted(links.iterdir()) == before

    # Back to normal: the stale link goes
    assert _listing(links, listing.linked, 7).complete
    assert not (links / "usb-unplugged-if00-port0").is_symlink()


@pytest.mark.parametrize("failure", IOKIT_FAILURES)
def test_another_dongle_in_the_port_during_an_outage_gets_no_link(
    tmp_path: Path, failure: dict
) -> None:
    """Test a link goes once its port's device number changes.

    A dongle moved into the port meanwhile would otherwise pass for the old
    one. macOS may give it the same number, though; then only the hardware
    monitor's poll, seeing the port go and come back, tells them apart.
    """
    links = tmp_path / "by-id"
    good = _listing(links, None, 7)
    listing = _listing(links, good.linked, 8, **failure)
    (device,) = listing.devices
    assert device.by_id is None
    assert not (links / BY_ID).is_symlink()
    assert listing.linked == {}


@pytest.mark.parametrize(
    "run",
    [
        pytest.param({"side_effect": OSError("no ioreg")}, id="no-iokit"),
        pytest.param(
            {"return_value": MagicMock(stdout=plistlib.dumps([]))}, id="left-out"
        ),
    ],
)
def test_first_listing_starts_from_the_links_on_disk(tmp_path: Path, run: dict) -> None:
    """Test a Supervisor start without IOKit's details keeps the links it finds.

    A Mac restart during an IOKit outage would otherwise leave the app whose
    option names the link unable to start.
    """
    links = tmp_path / "by-id"
    _listing(links, None, 7)
    (links / "usb-unplugged-if00-port0").symlink_to("/dev/cu.gone")
    listing = _listing(links, None, 7, **run)
    assert not listing.complete
    assert listing.devices[0].by_id == links / BY_ID
    assert listing.linked == {Path("/dev", NODE): (7, BY_ID)}
    assert (links / BY_ID).readlink() == Path("/dev", NODE)


def test_first_listing_claims_no_port_with_two_links(tmp_path: Path) -> None:
    """Test a port two links point at gets neither: one of them is stale."""
    links = tmp_path / "by-id"
    _listing(links, None, 7)
    (links / "usb-OTHER-if00-port0").symlink_to(f"/dev/{NODE}")
    listing = _listing(links, None, 7, side_effect=OSError("no ioreg"))
    assert listing.devices[0].by_id is None
    assert listing.linked == {}
    assert (links / BY_ID).is_symlink()


def test_a_port_iokit_leaves_out_keeps_its_link(tmp_path: Path) -> None:
    """Test a linked port missing from a good ioreg answer keeps its link.

    Only while it holds the same device; and the listing is tried again.
    """
    links = tmp_path / "by-id"
    good = _listing(links, None, 7)
    empty = {"return_value": MagicMock(stdout=plistlib.dumps([]))}

    listing = _listing(links, good.linked, 7, **empty)
    assert not listing.complete
    assert listing.devices[0].by_id == links / BY_ID
    assert (links / BY_ID).is_symlink()

    listing = _listing(links, listing.linked, 8, **empty)
    assert listing.complete
    assert listing.devices[0].by_id is None
    assert not (links / BY_ID).is_symlink()


def _monitor(
    known: list[str],
    present: dict[str, int],
    usb_loaded: bool,
    seen: dict[str, int] | None = None,
    complete: bool = True,
    complete_after: bool | None = None,
) -> HwMonitor:
    """Return a monitor that knows `known` and now sees `present` (name: rdev).

    `complete` is the last listing's; a reload's is `complete_after`.
    """
    monitor = MagicMock()
    monitor.sys_hardware.devices = [_device(name) for name in known]
    monitor.sys_hardware.native_complete = complete
    monitor._native_seen = frozenset(seen.items()) if seen is not None else None
    monitor._native_retry_at = 0.0
    monitor._native_retry_delay = NATIVE_POLL_SECONDS
    monitor.sys_loop.time = MagicMock(return_value=100.0)

    async def reload() -> None:
        monitor.sys_hardware.devices = [_device(name) for name in sorted(present)]
        monitor.sys_hardware.native_complete = (
            complete if complete_after is None else complete_after
        )

    monitor.sys_hardware.load_devices = AsyncMock(side_effect=reload)
    monitor.sys_run_in_executor = AsyncMock(return_value=frozenset(present.items()))
    monitor.sys_homeassistant.core.is_running = AsyncMock(return_value=True)
    monitor.sys_homeassistant.websocket.async_send_command = AsyncMock(
        return_value={"components": ["usb"] if usb_loaded else ["http"]}
    )
    monitor._native_ask_core_to_scan = lambda: HwMonitor._native_ask_core_to_scan(
        monitor
    )
    return monitor


@pytest.mark.parametrize(
    "seen", [pytest.param(None, id="first-poll"), pytest.param({"cu.a": 1}, id="later")]
)
async def test_monitor_does_nothing_while_nothing_changes(
    seen: dict[str, int] | None,
) -> None:
    """Test the cheap check is all that runs when the ports are the same."""
    monitor = _monitor(["cu.a"], {"cu.a": 1}, usb_loaded=True, seen=seen)
    await HwMonitor._native_refresh(monitor)
    monitor.sys_hardware.load_devices.assert_not_awaited()


@pytest.mark.parametrize(("usb_loaded", "scans"), [(True, 1), (False, 0)])
async def test_monitor_reloads_and_has_core_scan_on_a_new_port(
    usb_loaded: bool, scans: int
) -> None:
    """Test a new port reloads the devices and asks Core to scan, if usb runs."""
    monitor = _monitor(
        ["cu.a"], {"cu.a": 1, "cu.b": 2}, usb_loaded=usb_loaded, seen={"cu.a": 1}
    )
    await HwMonitor._native_refresh(monitor)
    monitor.sys_hardware.load_devices.assert_awaited_once()
    send = monitor.sys_homeassistant.websocket.send_command
    assert send.call_count == scans


async def test_monitor_retries_an_incomplete_listing_less_often() -> None:
    """Test a listing without IOKit is redone after 5 s, 10 s, 20 s ..."""
    monitor = _monitor(
        ["cu.a"], {"cu.a": 1}, usb_loaded=True, seen={"cu.a": 1}, complete=False
    )
    waits = []
    for _ in range(8):
        monitor._native_retry_at = 0.0  # due
        await HwMonitor._native_refresh(monitor)
        waits.append(monitor._native_retry_at - 100.0)
    assert waits == [5, 10, 20, 40, 80, 160, 300, 300]
    assert monitor.sys_hardware.load_devices.await_count == 8
    monitor.sys_homeassistant.websocket.send_command.assert_not_called()

    # Not due yet: nothing
    monitor._native_retry_at = 150.0
    await HwMonitor._native_refresh(monitor)
    assert monitor.sys_hardware.load_devices.await_count == 8


async def test_monitor_has_core_scan_when_iokit_is_back_then_goes_quiet() -> None:
    """Test recovery asks Core to scan, resets the wait, and stops retrying."""
    monitor = _monitor(
        ["cu.a"],
        {"cu.a": 1},
        usb_loaded=True,
        seen={"cu.a": 1},
        complete=False,
        complete_after=True,
    )
    monitor._native_retry_delay = 80
    await HwMonitor._native_refresh(monitor)
    monitor.sys_hardware.load_devices.assert_awaited_once()
    monitor.sys_homeassistant.websocket.send_command.assert_called_once()
    assert monitor._native_retry_delay == NATIVE_POLL_SECONDS

    await HwMonitor._native_refresh(monitor)
    monitor.sys_hardware.load_devices.assert_awaited_once()


async def test_monitor_reloads_on_a_removal_without_a_scan() -> None:
    """Test an unplugged port reloads the devices, with no scan."""
    monitor = _monitor(
        ["cu.a", "cu.b"], {"cu.a": 1}, usb_loaded=True, seen={"cu.a": 1, "cu.b": 2}
    )
    await HwMonitor._native_refresh(monitor)
    monitor.sys_hardware.load_devices.assert_awaited_once()
    monitor.sys_homeassistant.websocket.send_command.assert_not_called()


async def test_monitor_notices_another_dongle_in_the_same_port() -> None:
    """Test a swap within one poll (same name, new device number) reloads."""
    monitor = _monitor(["cu.a"], {"cu.a": 7}, usb_loaded=True, seen={"cu.a": 1})
    await HwMonitor._native_refresh(monitor)
    monitor.sys_hardware.load_devices.assert_awaited_once()
    monitor.sys_homeassistant.websocket.send_command.assert_called_once()


def test_malformed_ioreg_still_lists_the_ports(tmp_path: Path) -> None:
    """Test broken IOKit output never empties the list apps are checked against."""
    for output in (b"<plist><array><dict>", plistlib.dumps({"not": "a list"})):
        with (
            patch.object(hardware, "serial_nodes", return_value={"cu.usbserial-3110"}),
            patch.object(
                hardware.subprocess, "run", return_value=MagicMock(stdout=output)
            ),
        ):
            (device,) = hardware.list_devices(tmp_path / "by-id").devices
        assert device.name == "cu.usbserial-3110"


def test_one_bad_link_keeps_the_others(tmp_path: Path) -> None:
    """Test a link that can't be written costs only its own device its by-id."""
    links = tmp_path / "by-id"
    links.mkdir()
    (links / "usb-blocked").mkdir()  # a folder in the way of one link
    (links / "usb-blocked" / "keep").write_text("x")
    in_place = hardware.sync_links(
        links, {"usb-blocked": Path("/dev/cu.a"), BY_ID: Path("/dev/cu.b")}
    )
    assert in_place == {BY_ID}
    assert (links / BY_ID).readlink() == Path("/dev/cu.b")


def test_long_names_are_shortened_uniquely() -> None:
    """Test USB strings long enough to pass the file name limit still fit."""
    long_a, long_b = "usb-" + "x" * 400 + "-a", "usb-" + "x" * 400 + "-b"
    fit_a, fit_b = hardware._fit(long_a), hardware._fit(long_b)
    assert len(fit_a.encode()) <= hardware._NAME_MAX
    assert fit_a != fit_b
    assert hardware._fit(BY_ID) == BY_ID


def test_this_mac(tmp_path: Path) -> None:
    """Test real discovery on the host running the tests."""
    devices = hardware.list_devices(tmp_path / "by-id").devices
    usb = [device for device in devices if "usbserial" in device.name]
    if not usb:
        pytest.skip("no USB serial device on this host")
    assert all(device.attributes.get("ID_VENDOR_ID") for device in usb)


def test_app_device_options_offer_and_accept_the_links(tmp_path: Path) -> None:
    """Test a device(subsystem=tty) option lists the links and accepts them.

    As on Linux, the settings page offers each port by its by-id link, and the
    port by name when it has none.
    """
    links = tmp_path / "dev" / "serial" / "by-id"
    plain = hardware.NativeDevice(
        "cu.usbserial-9", Path("/dev/cu.usbserial-9"), Path("/dev/cu.usbserial-9"),
        "tty", None, [], {"DEVNAME": "/dev/cu.usbserial-9", "SUBSYSTEM": "tty"}, [],
    )  # fmt: skip
    dongle = hardware.NativeDevice(
        "cu.usbserial-140", Path("/dev/cu.usbserial-140"),
        Path("/dev/cu.usbserial-140"), "tty", None, [links / BY_ID],
        {"DEVNAME": "/dev/cu.usbserial-140", "SUBSYSTEM": "tty"}, [],
    )  # fmt: skip
    coresys = MagicMock()
    coresys.hardware = HardwareManager(coresys, None)  # type: ignore[arg-type]
    for device in (plain, dongle):
        coresys.hardware.update_device(device)

    (ui,) = UiOptions(coresys)({"device": "device(subsystem=tty)"})
    assert ui["type"] == "select"
    assert sorted(ui["options"]) == sorted([str(links / BY_ID), "/dev/cu.usbserial-9"])

    schema = {"device": "device(subsystem=tty)"}
    for value in (str(links / BY_ID), "/dev/cu.usbserial-140"):
        options = AppOptions(coresys, schema, "OTBR", "otbr")
        assert options({"device": value}) == {"device": value}
        assert options.devices == {dongle}
    for value in (str(links / "usb-gone-if00-port0"), "", None):
        with pytest.raises(vol.Invalid):
            AppOptions(coresys, schema, "OTBR", "otbr")({"device": value})
