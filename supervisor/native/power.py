"""Keep a native macOS host awake while the Supervisor runs.

HA OS never sleeps. A Mac goes to sleep when it is idle, which stops Core,
the apps and every radio. The Supervisor holds a power assertion that blocks
idle sleep. macOS drops it when the process exits, and the user can still
choose Sleep from the Apple menu.
"""

from __future__ import annotations

import ctypes
import ctypes.util
import logging
from typing import Any

_LOGGER: logging.Logger = logging.getLogger(__name__)

ASSERTION_TYPE = "PreventUserIdleSystemSleep"
ASSERTION_NAME = "Home Assistant Supervisor: keep the host awake like HA OS"
_LEVEL_ON = 255  # kIOPMAssertionLevelOn
_UTF8 = 0x08000100  # kCFStringEncodingUTF8

# The assertion lasts as long as this process; keep its id for the log.
_assertion_id: int | None = None


def _frameworks() -> tuple[Any, Any]:
    iokit = ctypes.CDLL(ctypes.util.find_library("IOKit"))
    corefoundation = ctypes.CDLL(ctypes.util.find_library("CoreFoundation"))
    corefoundation.CFStringCreateWithCString.restype = ctypes.c_void_p
    corefoundation.CFStringCreateWithCString.argtypes = [
        ctypes.c_void_p,
        ctypes.c_char_p,
        ctypes.c_uint32,
    ]
    corefoundation.CFRelease.restype = None
    corefoundation.CFRelease.argtypes = [ctypes.c_void_p]
    iokit.IOPMAssertionCreateWithName.restype = ctypes.c_int32
    iokit.IOPMAssertionCreateWithName.argtypes = [
        ctypes.c_void_p,
        ctypes.c_uint32,
        ctypes.c_void_p,
        ctypes.POINTER(ctypes.c_uint32),
    ]
    return iokit, corefoundation


def hold_awake(frameworks: tuple[Any, Any] | None = None) -> int | None:
    """Block idle sleep for the life of this process; return the assertion id.

    Never raises: a Mac that may sleep is logged, not a reason to stop.
    """
    global _assertion_id  # noqa: PLW0603
    if _assertion_id is not None:
        return _assertion_id
    try:
        iokit, corefoundation = frameworks or _frameworks()
        kind = corefoundation.CFStringCreateWithCString(
            None, ASSERTION_TYPE.encode(), _UTF8
        )
        name = corefoundation.CFStringCreateWithCString(
            None, ASSERTION_NAME.encode(), _UTF8
        )
        if not kind or not name:
            raise OSError("CFStringCreateWithCString failed")
        try:
            assertion = ctypes.c_uint32(0)
            result = iokit.IOPMAssertionCreateWithName(
                kind, _LEVEL_ON, name, ctypes.pointer(assertion)
            )
        finally:
            corefoundation.CFRelease(kind)
            corefoundation.CFRelease(name)
        if result != 0:
            raise OSError(f"IOPMAssertionCreateWithName returned {result:#x}")
    except Exception as err:  # pylint: disable=broad-except
        _LOGGER.error(
            "Can't keep the Mac awake (%s); it may sleep when idle and stop Core", err
        )
        return None
    _assertion_id = assertion.value
    _LOGGER.info("Holding a power assertion so the Mac doesn't sleep when idle")
    return _assertion_id
