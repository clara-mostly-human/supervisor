"""Resource stats of native (launchd) services, in the shape Docker reports.

A service is a process tree: its launchd pid and every descendant. Memory is
the physical footprint (what Activity Monitor shows), CPU the user and system
time. The Supervisor runs as the user, so for a root-owned app (OTBR) macOS
refuses those reads; then /bin/ps supplies the resident size and CPU time.
Per-process network and disk counters don't exist on macOS and stay 0.

Differences from Docker's cgroup numbers: CPU time of a child that exits
between two samples is lost with it, so a windowed percentage can read low;
and a root-owned tree counts resident size, not footprint.
"""

from __future__ import annotations

import asyncio
import ctypes
import ctypes.util
from functools import cache
import os
import subprocess
import time
from typing import Any

from ..docker.stats import DockerStats
from ..exceptions import (
    DockerContainerNotRunningError,
    DockerError,
    DockerStatsTimeoutError,
)

_SAMPLE_SECONDS = 1.0
_RUSAGE_INFO_V2 = 2


class _RusageInfo(ctypes.Structure):
    _fields_ = [("ri_uuid", ctypes.c_uint8 * 16)] + [
        (name, ctypes.c_uint64)
        for name in (
            "ri_user_time",
            "ri_system_time",
            "ri_pkg_idle_wkups",
            "ri_interrupt_wkups",
            "ri_pageins",
            "ri_wired_size",
            "ri_resident_size",
            "ri_phys_footprint",
            "ri_proc_start_abstime",
            "ri_proc_exit_abstime",
            "ri_child_user_time",
            "ri_child_system_time",
            "ri_child_pkg_idle_wkups",
            "ri_child_interrupt_wkups",
            "ri_child_pageins",
            "ri_child_elapsed_abstime",
            "ri_diskio_bytesread",
            "ri_diskio_byteswritten",
        )
    ]
    # struct rusage_info_v2 from <sys/resource.h>: the kernel writes all of it,
    # so a short struct here corrupts the memory after it.


# Room to spare, so a kernel that writes more than this layout can't overflow.
_RUSAGE_BUFFER_SIZE = 1024
assert ctypes.sizeof(_RusageInfo) == 160


class _Timebase(ctypes.Structure):
    _fields_ = [("numer", ctypes.c_uint32), ("denom", ctypes.c_uint32)]


@cache
def _libc() -> Any:
    """Load libc on first use, so importing this module works anywhere."""
    return ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


@cache
def _timebase() -> tuple[int, int]:
    """Return the Mach time units' numerator and denominator (to nanoseconds)."""
    timebase = _Timebase()
    _libc().mach_timebase_info(ctypes.byref(timebase))
    return timebase.numer, timebase.denom


def _ps_table() -> dict[int, tuple[int, int, int]]:
    """Return pid -> (parent pid, resident bytes, CPU nanoseconds) from ps."""
    output = subprocess.run(
        ["/bin/ps", "-A", "-o", "pid=,ppid=,rss=,time="],
        capture_output=True,
        text=True,
        check=True,
        timeout=10,
    ).stdout
    table = {}
    for line in output.splitlines():
        fields = line.split()
        if len(fields) != 4:
            continue
        pid, ppid, rss, cputime = fields
        table[int(pid)] = (int(ppid), int(rss) * 1024, _parse_cputime(cputime))
    return table


def _parse_cputime(value: str) -> int:
    """Parse ps cumulative CPU time ([[dd-]hh:]mm:ss.cc) into nanoseconds."""
    days, _, clock = value.rpartition("-")
    seconds = 0.0
    for part in clock.split(":"):
        seconds = seconds * 60 + float(part)
    if days:
        seconds += int(days) * 86400
    return int(seconds * 1_000_000_000)


def _tree(table: dict[int, tuple[int, int, int]], root: int) -> list[int]:
    """Return the root pid and all of its descendants present in the table."""
    children: dict[int, list[int]] = {}
    for pid, (ppid, _, _) in table.items():
        children.setdefault(ppid, []).append(pid)
    pids, todo = [], [root]
    while todo:
        pid = todo.pop()
        if pid in table and pid not in pids:
            pids.append(pid)
            todo.extend(children.get(pid, []))
    return pids


def _rusage(pid: int) -> tuple[int, int] | None:
    """Return (footprint bytes, CPU nanoseconds), or None if macOS refuses."""
    buffer = ctypes.create_string_buffer(_RUSAGE_BUFFER_SIZE)
    if _libc().proc_pid_rusage(pid, _RUSAGE_INFO_V2, buffer) != 0:
        return None
    info = _RusageInfo.from_buffer_copy(buffer)
    numer, denom = _timebase()
    return info.ri_phys_footprint, (
        (info.ri_user_time + info.ri_system_time) * numer // denom
    )


def _host_cpu_ns() -> int:
    """Return the CPU time all cores had since boot, in nanoseconds.

    Like the total in /proc/stat that Docker reports, busy or idle. The Mach
    tick counters are 32-bit and wrap within weeks, so derive it from uptime.
    """
    return time.monotonic_ns() * (os.cpu_count() or 1)


def _memory_total() -> int:
    return os.sysconf("SC_PAGE_SIZE") * os.sysconf("SC_PHYS_PAGES")


def _sample(root: int) -> tuple[int, int, int] | None:
    """Return (memory bytes, process CPU ns, host CPU ns) for a process tree."""
    table = _ps_table()
    if root not in table:
        return None
    memory = cpu = 0
    for pid in _tree(table, root):
        if (usage := _rusage(pid)) is not None:
            memory += usage[0]
            cpu += usage[1]
        else:
            memory += table[pid][1]
            cpu += table[pid][2]
    return memory, cpu, _host_cpu_ns()


def _docker_shape(
    now: tuple[int, int, int], before: tuple[int, int, int] | None
) -> dict[str, Any]:
    memory, cpu, host = now
    stats: dict[str, Any] = {
        "memory_stats": {
            "usage": memory,
            "limit": _memory_total(),
            "stats": {"inactive_file": 0},
        },
        "cpu_stats": {
            "cpu_usage": {"total_usage": cpu},
            "system_cpu_usage": host,
            "online_cpus": os.cpu_count() or 1,
        },
    }
    if before is not None:
        stats["precpu_stats"] = {
            "cpu_usage": {"total_usage": before[1]},
            "system_cpu_usage": before[2],
        }
    return stats


async def process_stats(pid: int, *, name: str, one_shot: bool = False) -> DockerStats:
    """Return the stats of the process tree of service name.

    Without one_shot, two samples a second apart give a CPU percentage, as
    Docker's windowed stats do.
    """
    loop = asyncio.get_running_loop()
    try:
        first = await loop.run_in_executor(None, _sample, pid)
        if first is None:
            raise DockerContainerNotRunningError(name=name)
        if one_shot:
            return DockerStats(_docker_shape(first, None))
        await asyncio.sleep(_SAMPLE_SECONDS)
        second = await loop.run_in_executor(None, _sample, pid)
    except subprocess.TimeoutExpired as err:
        raise DockerStatsTimeoutError(name=name) from err
    except (OSError, subprocess.SubprocessError, ValueError) as err:
        raise DockerError(f"Can't read the processes of {name}: {err}") from err
    if second is None:
        raise DockerContainerNotRunningError(name=name)
    return DockerStats(_docker_shape(second, first))
