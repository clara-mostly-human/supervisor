"""Test native (macOS) hardware discovery."""

from pathlib import Path
from unittest.mock import MagicMock, patch

from supervisor.hardware import manager as hw_manager
from supervisor.hardware.data import Device
from supervisor.hardware.manager import HardwareManager


def _device(name: str) -> Device:
    node = Path("/dev", name)
    return Device(name, node, node, "tty", None, [], {}, [])


def test_import_devices_returns_the_native_devices() -> None:
    """Test load() gets the devices back, since it stores what this returns."""
    devices = [_device("cu.usbserial-1120"), _device("cu.usbserial-3110")]
    with patch.object(hw_manager, "native_devices", return_value=devices):
        found = HardwareManager._import_devices(MagicMock())  # noqa: SLF001
    assert found == {device.name: device for device in devices}
