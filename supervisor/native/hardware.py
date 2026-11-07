"""Hardware discovery on macOS, which has no udev.

Serial devices (/dev/cu.*) are what radio dongles (Zigbee, Thread, Z-Wave)
show up as. IOKit supplies their USB vendor, product and serial number, which
are reported under udev's attribute names. Linux gives every USB serial
device a stable /dev/serial/by-id link that survives a move to another port;
/dev can't hold one here, so the Supervisor keeps the same links in its data
folder instead.
"""

from __future__ import annotations

import logging
from pathlib import Path
import plistlib
import re
import subprocess
from typing import Any

from ..hardware.data import Device

_LOGGER: logging.Logger = logging.getLogger(__name__)

# Present on every Mac and never a user's device.
_IGNORED_SERIAL = ("cu.Bluetooth-Incoming-Port", "cu.debug-console")
# udev keeps these characters in by-id names and replaces the rest
_RE_UNSAFE = re.compile(r"[^A-Za-z0-9#+\-.:=@_]")
_LINK_TMP_SUFFIX = ".supervisor-new"
# Inside the Supervisor's data folder, mirroring Linux's /dev/serial/by-id
NATIVE_BY_ID = Path("dev", "serial", "by-id")


def serial_nodes() -> set[str]:
    """Return the names of the serial devices present right now (cheap)."""
    return {
        node.name
        for node in Path("/dev").glob("cu.*")
        if node.name not in _IGNORED_SERIAL
    }


class NativeDevice(Device):
    """A device whose by-id link lives in the Supervisor's data folder."""

    __slots__ = ()

    @property
    def by_id(self) -> Path | None:
        """Return the by-id link."""
        for link in self.links:
            if link.parent.name == "by-id":
                return link
        return None


def _clean(value: Any) -> str:
    return _RE_UNSAFE.sub("_", str(value).strip().replace(" ", "_"))


def _usb_serial_info() -> dict[str, dict[str, Any]]:
    """Return USB facts per serial device path (/dev/cu.*), from IOKit."""
    output = subprocess.run(
        ["/usr/sbin/ioreg", "-r", "-c", "IOUSBHostDevice", "-l", "-a"],
        capture_output=True,
        check=True,
        timeout=15,
    ).stdout
    found: dict[str, dict[str, Any]] = {}

    def walk(node: dict[str, Any], usb: dict[str, Any] | None, iface: int | None):
        # The nearest USB device above the serial port, not a hub further up;
        # interfaces carry idVendor too but have an interface number.
        if "idVendor" in node and "bInterfaceNumber" not in node:
            usb = node
        if "bInterfaceNumber" in node:
            iface = node["bInterfaceNumber"]
        if (callout := node.get("IOCalloutDevice")) and usb is not None:
            found[callout] = {**usb, "_interface": iface or 0}
        for child in node.get("IORegistryEntryChildren", []):
            walk(child, usb, iface)

    for root in plistlib.loads(output) if output.strip() else []:
        walk(root, None, None)
    return found


def _attributes(node: Path, usb: dict[str, Any] | None) -> tuple[dict, str | None]:
    """Return udev-style attributes and the by-id name for a serial device."""
    attributes = {"DEVNAME": str(node), "SUBSYSTEM": "tty"}
    if not usb:
        return attributes, None
    vendor = usb.get("kUSBVendorString") or usb.get("USB Vendor Name")
    product = usb.get("kUSBProductString") or usb.get("USB Product Name")
    serial = usb.get("kUSBSerialNumberString") or usb.get("USB Serial Number")
    interface = int(usb.get("_interface") or 0)
    attributes |= {
        "ID_BUS": "usb",
        "ID_VENDOR_ID": f"{usb['idVendor']:04x}",
        "ID_MODEL_ID": f"{usb.get('idProduct', 0):04x}",
        "ID_USB_INTERFACE_NUM": f"{interface:02x}",
        "LOCATION_ID": f"{usb.get('locationID', 0):#010x}",
    }
    if vendor:
        attributes["ID_VENDOR"] = _clean(vendor)
    if product:
        attributes["ID_MODEL"] = _clean(product)
    if serial:
        attributes["ID_SERIAL_SHORT"] = _clean(serial)
    parts = [_clean(part) for part in (vendor, product, serial) if part]
    if parts:
        attributes["ID_SERIAL"] = "_".join(parts)
    # Without a serial number two identical dongles can't be told apart.
    if not serial:
        return attributes, None
    return attributes, f"usb-{attributes['ID_SERIAL']}-if{interface:02x}-port0"


def sync_links(links_dir: Path, wanted: dict[str, Path]) -> None:
    """Make links_dir hold exactly the wanted name -> device links."""
    links_dir.mkdir(parents=True, exist_ok=True)
    for entry in links_dir.iterdir():
        if entry.name not in wanted and entry.is_symlink():
            entry.unlink()
    for name, target in wanted.items():
        link = links_dir / name
        if link.is_symlink() and link.readlink() == target:
            continue
        tmp = links_dir / f"{name}{_LINK_TMP_SUFFIX}"
        tmp.unlink(missing_ok=True)
        tmp.symlink_to(target)
        tmp.replace(link)


def list_devices(links_dir: Path | None = None) -> list[Device]:
    """Return the host's serial devices, keeping their by-id links in links_dir."""
    nodes = [Path("/dev", name) for name in sorted(serial_nodes())]
    try:
        usb = _usb_serial_info() if nodes else {}
    except (OSError, subprocess.SubprocessError, plistlib.InvalidFileException) as err:
        _LOGGER.warning("Can't read USB details from IOKit: %s", err)
        usb = {}

    named: dict[Path, tuple[dict, str | None]] = {
        node: _attributes(node, usb.get(str(node))) for node in nodes
    }
    names = [name for _, name in named.values() if name]
    wanted = {
        name: node
        for node, (_, name) in named.items()
        if name and names.count(name) == 1
    }
    if links_dir is not None:
        try:
            sync_links(links_dir, wanted)
        except OSError as err:
            _LOGGER.warning("Can't update the by-id links in %s: %s", links_dir, err)
            links_dir = None

    devices: list[Device] = []
    for node, (attributes, name) in named.items():
        links = (
            [links_dir / name]
            if links_dir is not None and name and name in wanted
            else []
        )
        devices.append(
            NativeDevice(
                name=node.name,
                path=node,
                sysfs=node,
                subsystem="tty",
                parent=None,
                links=links,
                attributes=attributes,
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
