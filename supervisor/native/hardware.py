"""Hardware discovery on macOS, which has no udev.

For now this lists serial devices (/dev/cu.*), which is what radio dongles
(Zigbee, Thread, Z-Wave) show up as. Richer IOKit data comes later.
"""

from __future__ import annotations

from pathlib import Path

from ..hardware.data import Device

# Present on every Mac and never a user's device.
_IGNORED_SERIAL = ("cu.Bluetooth-Incoming-Port", "cu.debug-console")


def list_devices() -> list[Device]:
    """Return the host's serial devices."""
    devices = []
    for node in sorted(Path("/dev").glob("cu.*")):
        if node.name in _IGNORED_SERIAL:
            continue
        devices.append(
            Device(
                name=node.name,
                path=node,
                sysfs=node,
                subsystem="tty",
                parent=None,
                links=[],
                attributes={},
                children=[],
            )
        )
    return devices


def boot_time() -> int | None:
    """Return the host boot time as a Unix timestamp (sysctl kern.boottime)."""
    import ctypes  # noqa: PLC0415
    import ctypes.util  # noqa: PLC0415

    class Timeval(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_int32)]

    libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)
    value = Timeval()
    size = ctypes.c_size_t(ctypes.sizeof(value))
    if libc.sysctlbyname(
        b"kern.boottime", ctypes.byref(value), ctypes.byref(size), None, 0
    ):
        return None
    return int(value.tv_sec)
