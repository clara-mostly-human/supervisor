"""Test native (macOS) hardware discovery."""

from pathlib import Path
import plistlib
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from supervisor.hardware import manager as hw_manager
from supervisor.hardware.data import Device
from supervisor.hardware.manager import HardwareManager
from supervisor.hardware.monitor import HwMonitor
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
    with patch.object(hw_manager, "native_devices", return_value=devices) as found:
        result = HardwareManager._import_devices(manager)  # noqa: SLF001
    assert result == {device.name: device for device in devices}
    found.assert_called_once_with(tmp_path / "dev" / "serial" / "by-id")


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
        (device,) = hardware.list_devices(links)
    assert device.by_id == links / BY_ID
    assert device.links == [links / BY_ID]

    twice = {"/dev/cu.a": {}, "/dev/cu.b": {}}
    with (
        patch.object(hardware, "serial_nodes", return_value={"cu.a", "cu.b"}),
        patch.object(hardware, "_usb_serial_info", return_value=twice),
        patch.object(hardware, "_attributes", return_value=({"DEVNAME": "x"}, BY_ID)),
    ):
        devices = hardware.list_devices(links)
    assert [device.by_id for device in devices] == [None, None]


def test_ioreg_failure_still_lists_the_ports(tmp_path: Path) -> None:
    """Test the ports are listed even if IOKit can't be read."""
    with (
        patch.object(hardware, "serial_nodes", return_value={"cu.usbserial-3110"}),
        patch.object(hardware.subprocess, "run", side_effect=OSError("no ioreg")),
    ):
        (device,) = hardware.list_devices(tmp_path / "by-id")
    assert device.name == "cu.usbserial-3110"
    assert device.by_id is None


def _monitor(known: list[str], present: set[str], usb_loaded: bool) -> HwMonitor:
    monitor = MagicMock()
    devices = [_device(name) for name in known]
    monitor.sys_hardware.devices = devices

    async def reload() -> None:
        monitor.sys_hardware.devices = [_device(name) for name in sorted(present)]

    monitor.sys_hardware.load_devices = AsyncMock(side_effect=reload)
    monitor.sys_run_in_executor = AsyncMock(return_value=present)
    monitor.sys_homeassistant.core.is_running = AsyncMock(return_value=True)
    monitor.sys_homeassistant.websocket.async_send_command = AsyncMock(
        return_value={"components": ["usb"] if usb_loaded else ["http"]}
    )
    monitor._native_ask_core_to_scan = lambda: HwMonitor._native_ask_core_to_scan(
        monitor
    )
    return monitor


async def test_monitor_does_nothing_while_nothing_changes() -> None:
    """Test the cheap check is all that runs when the ports are the same."""
    monitor = _monitor(["cu.a"], {"cu.a"}, usb_loaded=True)
    await HwMonitor._native_refresh(monitor)
    monitor.sys_hardware.load_devices.assert_not_awaited()


@pytest.mark.parametrize(("usb_loaded", "scans"), [(True, 1), (False, 0)])
async def test_monitor_reloads_and_has_core_scan_on_a_new_port(
    usb_loaded: bool, scans: int
) -> None:
    """Test a new port reloads the devices and asks Core to scan, if usb runs."""
    monitor = _monitor(["cu.a"], {"cu.a", "cu.b"}, usb_loaded=usb_loaded)
    await HwMonitor._native_refresh(monitor)
    monitor.sys_hardware.load_devices.assert_awaited_once()
    send = monitor.sys_homeassistant.websocket.send_command
    assert send.call_count == scans


async def test_monitor_reloads_on_a_removal_without_a_scan() -> None:
    """Test an unplugged port reloads the devices, with no scan."""
    monitor = _monitor(["cu.a", "cu.b"], {"cu.a"}, usb_loaded=True)
    await HwMonitor._native_refresh(monitor)
    monitor.sys_hardware.load_devices.assert_awaited_once()
    monitor.sys_homeassistant.websocket.send_command.assert_not_called()


def test_this_mac(tmp_path: Path) -> None:
    """Test real discovery on the host running the tests."""
    devices = hardware.list_devices(tmp_path / "by-id")
    usb = [device for device in devices if "usbserial" in device.name]
    if not usb:
        pytest.skip("no USB serial device on this host")
    assert all(device.attributes.get("ID_VENDOR_ID") for device in usb)
