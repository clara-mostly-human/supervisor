"""Supervisor Hardware monitor based on udev."""

import asyncio
import logging
from pathlib import Path
from pprint import pformat
from typing import Any

import pyudev

from ..const import ATTR_TYPE, BusEvent
from ..coresys import CoreSys, CoreSysAttributes
from ..exceptions import HardwareNotFound, HomeAssistantWSError
from ..native import IS_MACOS
from ..native.hardware import serial_nodes
from ..resolution.const import UnhealthyReason
from .const import HardwareAction, UdevKernelAction
from .data import Device

_LOGGER: logging.Logger = logging.getLogger(__name__)

NATIVE_POLL_SECONDS = 5


class HwMonitor(CoreSysAttributes):
    """Hardware monitor for supervisor."""

    def __init__(self, coresys: CoreSys, context: pyudev.Context):
        """Initialize Hardware Monitor object."""
        self.coresys: CoreSys = coresys
        self.context = context
        self.monitor: pyudev.Monitor | None = None
        self.observer: pyudev.MonitorObserver | None = None
        self._native_task: asyncio.Task | None = None

    async def load(self) -> None:
        """Start hardware monitor."""
        if IS_MACOS:
            self._native_task = self.sys_create_task(self._native_watch())
            _LOGGER.info("Started Supervisor hardware monitor (polling serial devices)")
            return
        try:
            self.monitor = pyudev.Monitor.from_netlink(self.context, "kernel")
            self.monitor.set_receive_buffer_size(32 * 1024 * 1024)

            self.observer = pyudev.MonitorObserver(
                self.monitor,
                callback=lambda x: self.sys_loop.call_soon_threadsafe(
                    self._udev_events, x
                ),
            )
        except OSError:
            self.sys_resolution.add_unhealthy_reason(UnhealthyReason.PRIVILEGED)
            _LOGGER.critical("Not privileged to run udev monitor!")
        else:
            self.observer.start()
            _LOGGER.info("Started Supervisor hardware monitor")

    async def unload(self) -> None:
        """Shutdown sessions."""
        if self._native_task is not None:
            self._native_task.cancel()
            self._native_task = None
        if self.observer is None:
            return

        self.observer.stop()
        _LOGGER.info("Stopped Supervisor hardware monitor")

    def _udev_events(self, kernel: pyudev.Device):
        """Incoming events from udev."""
        _LOGGER.debug("Hardware monitor: %s - %s", kernel.action, pformat(kernel))

        if kernel.action in (UdevKernelAction.UNBIND, UdevKernelAction.BIND):
            return
        self.sys_create_task(self._async_udev_events(kernel))

    async def _async_udev_events(self, kernel: pyudev.Device):
        """Incoming events from udev into loop."""
        # Update device List
        if not kernel.device_node or self.sys_hardware.helper.hide_virtual_device(
            kernel
        ):
            return

        hw_action: HardwareAction | None = None
        device: Device | None = None

        ##
        # Remove
        if kernel.action == UdevKernelAction.REMOVE:
            try:
                device = self.sys_hardware.get_by_path(Path(kernel.sys_path))
            except HardwareNotFound:
                return
            else:
                self.sys_hardware.delete_device(device)
                hw_action = HardwareAction.REMOVE

        ##
        # Add
        if kernel.action in (UdevKernelAction.ADD, UdevKernelAction.CHANGE):
            # We get pure Kernel events only inside container.
            # But udev itself need also time to initialize the device
            # before we can use it correctly
            udev = None
            for _ in range(3):
                await asyncio.sleep(2)
                try:
                    udev = await self.sys_run_in_executor(
                        pyudev.Devices.from_sys_path, self.context, kernel.sys_path
                    )
                except pyudev.DeviceNotFoundAtPathError:
                    continue
                if udev is not None and udev.is_initialized:
                    break

            # Is not ready
            if not udev:
                _LOGGER.warning(
                    "Ignore device %s / fails to initialize by udev", kernel.sys_path
                )
                return

            device = Device.import_udev(udev)
            self.sys_hardware.update_device(device)

            # If it's a new device - process actions
            if kernel.action == UdevKernelAction.ADD:
                hw_action = HardwareAction.ADD

        # Ignore event for future processing
        if device is None or hw_action is None:
            return
        _LOGGER.info(
            "Detecting %s hardware %s - %s", hw_action, device.path, device.by_id
        )

        # Fire Hardware event to bus
        if hw_action == HardwareAction.ADD:
            self.sys_bus.fire_event(BusEvent.HARDWARE_NEW_DEVICE, device)
        elif hw_action == HardwareAction.REMOVE:
            self.sys_bus.fire_event(BusEvent.HARDWARE_REMOVE_DEVICE, device)

    async def _native_watch(self) -> None:
        """Follow serial devices on macOS, which sends no udev events."""
        while True:
            await asyncio.sleep(NATIVE_POLL_SECONDS)
            try:
                await self._native_refresh()
            except Exception as err:  # pylint: disable=broad-except
                _LOGGER.warning("Can't refresh the serial devices: %s", err)

    async def _native_refresh(self) -> None:
        """Reload the devices if any serial device came, went or moved port."""
        present = await self.sys_run_in_executor(serial_nodes)
        known = {device.name for device in self.sys_hardware.devices}
        if present == known:
            return
        await self.sys_hardware.load_devices()
        current = {device.name: device for device in self.sys_hardware.devices}
        for name in sorted(known - current.keys()):
            _LOGGER.info("Detecting %s hardware /dev/%s", HardwareAction.REMOVE, name)
        added = sorted(current.keys() - known)
        for name in added:
            device = current[name]
            _LOGGER.info(
                "Detecting %s hardware %s - %s",
                HardwareAction.ADD,
                device.path,
                device.by_id,
            )
        if added:
            await self._native_ask_core_to_scan()

    async def _native_ask_core_to_scan(self) -> None:
        """Have Core look for new USB devices, as a udev event does on Linux."""
        if not await self.sys_homeassistant.core.is_running():
            return
        try:
            configuration: (
                dict[str, Any] | None
            ) = await self.sys_homeassistant.websocket.async_send_command(
                {ATTR_TYPE: "get_config"}
            )
        except HomeAssistantWSError as err:
            _LOGGER.debug("Not asking Core to scan for USB devices: %s", err)
            return
        if configuration and "usb" in configuration.get("components", []):
            self.sys_homeassistant.websocket.send_command({ATTR_TYPE: "usb/scan"})
