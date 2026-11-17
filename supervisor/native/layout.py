"""Switch the native Supervisor between the session and system layouts.

    python -m supervisor.native.layout {system,session} [--dry-run] [--prefix P]

session: the Supervisor, Core and apps are LaunchAgents in the user's GUI
session (Core can use the Mac's Bluetooth; a logout stops everything).
system: they are LaunchDaemons that run as the same user, defined by the root
helper (version 2 or later), so they keep running with nobody logged in.

Run it in the user's logged-in session (Terminal), never from a job of the
Supervisor: it stops the Supervisor first, so that nothing restarts Core or
the apps meanwhile, then Core, then the apps, and starts the Supervisor in the
other layout. The new Supervisor defines and starts Core and the apps itself,
as on any boot. Root apps (OTBR) are root LaunchDaemons in both layouts and
are never touched.

The session Supervisor's plist is kept in ~/Library/Application Support/
ha-macos/<prefix>/session-layout while
the system layout is in use, so the next login doesn't start it; "session"
puts it back (or derives it). Core's and the apps' plists are not kept: the
Supervisor writes them again. If anything fails half way, the tool puts the
layout it started from back.
"""

from __future__ import annotations

import argparse
import asyncio
import os
from pathlib import Path
import plistlib
import pwd
import re
import shutil
import sys
import threading
import urllib.error
import urllib.request

from . import helper, launchd
from .homeassistant import STOP_TIMEOUT
from .launchd import LaunchAgent, LaunchdError, UserDaemon

APP_STOP_TIMEOUT = 60
UNLOAD_TIMEOUT = 60
API_WAIT = 180
REQUIRED_KEYS = ("ProgramArguments", "WorkingDirectory", "StandardOutPath")
# A row of launchctl print's "services = {" block: pid, last status (a number,
# "-", or a reason such as "(pe)", "(jt)", "="), label.
_RE_SERVICE_ROW = re.compile(r"^\s+\S+\s+\S+\s+(\S+)\s*$")
_RE_USERNAME = re.compile(r"^\s*username = (\S+)", re.MULTILINE)


class LayoutError(Exception):
    """The switch can't be done as asked."""


def _say(text: str) -> None:
    sys.stdout.write(text + "\n")
    sys.stdout.flush()


def _user() -> str:
    return pwd.getpwuid(os.getuid()).pw_name


def _read(path: Path) -> dict:
    with path.open("rb") as file:
        return plistlib.load(file)


def _is_child(label: str, prefix: str) -> bool:
    return label == f"{prefix}.core" or label.startswith(f"{prefix}.app.")


def _order(labels: set[str], prefix: str) -> list[str]:
    """Return Core first, then the apps by name."""
    return sorted(labels, key=lambda label: (label != f"{prefix}.core", label))


async def _launchctl_print(target: str) -> str | None:
    code, text = await launchd.launchctl("print", target, check=False)
    return text if code == 0 else None


def _service_labels(text: str) -> set[str]:
    """Return the labels in the services block of launchctl print <domain>."""
    labels: set[str] = set()
    inside = False
    for line in text.splitlines():
        if line.strip() == "services = {":
            inside = True
        elif inside and line.strip() == "}":
            break
        elif inside and (m := _RE_SERVICE_ROW.match(line)):
            labels.add(m.group(1))
    return labels


async def _loaded(domain: str, prefix: str) -> set[str]:
    """Return Core and apps loaded in domain, from launchd rather than plists."""
    if (text := await _launchctl_print(domain)) is None:
        return set()
    return {label for label in _service_labels(text) if _is_child(label, prefix)}


async def _job_user(target: str) -> str | None:
    if (text := await _launchctl_print(target)) is None:
        return None
    return m.group(1) if (m := _RE_USERNAME.search(text)) else None


async def _session_children(agents: Path, prefix: str) -> list[str]:
    """Return Core and apps of the session layout: plists and loaded agents."""
    paths = await asyncio.to_thread(lambda: list(agents.glob(f"{prefix}.*.plist")))
    labels = {path.name.removesuffix(".plist") for path in paths}
    labels = {label for label in labels if _is_child(label, prefix)}
    labels |= await _loaded(launchd.domain(), prefix)
    return _order(labels, prefix)


async def _system_children(daemons: Path, prefix: str, user: str) -> list[str]:
    """Return Core and apps of the system layout that run as user, never root."""

    def from_plists() -> set[str]:
        return {
            path.name.removesuffix(".plist")
            for path in daemons.glob(f"{prefix}.*.plist")
            if _is_child(path.name.removesuffix(".plist"), prefix)
            and _read(path).get("UserName") == user
        }

    labels = await asyncio.to_thread(from_plists)
    for label in await _loaded("system", prefix):
        if label not in labels and await _job_user(f"system/{label}") == user:
            labels.add(label)
    return _order(labels, prefix)


async def _in_gui_session() -> bool:
    """Return True when run in the user's Aqua session (not SSH, not a job)."""
    code, text = await launchd.launchctl("managername", check=False)
    return code == 0 and text.strip() == "Aqua"


def _check_not_a_job(prefix: str) -> None:
    """Refuse to run inside a job this switch would stop."""
    if os.environ.get("XPC_SERVICE_NAME", "").startswith(f"{prefix}."):
        raise LayoutError("run this from Terminal, not from a Supervisor job")


def _state_dir(supervisor: dict) -> Path:
    data = supervisor.get("EnvironmentVariables", {}).get("SUPERVISOR_DATA")
    if not data:
        raise LayoutError("the Supervisor's plist has no SUPERVISOR_DATA")
    return Path(data) / "launchd"


def _check_plist(supervisor: dict, prefix: str) -> str:
    """Validate the Supervisor's plist for this prefix; return its API port."""
    missing = [key for key in REQUIRED_KEYS if not supervisor.get(key)]
    if missing:
        raise LayoutError(f"the Supervisor's plist lacks {', '.join(missing)}")
    env = supervisor.get("EnvironmentVariables", {})
    plist_prefix = env.get("SUPERVISOR_LAUNCHD_PREFIX", "ha-macos")
    if plist_prefix != prefix:
        raise LayoutError(
            f"the Supervisor's plist is for prefix {plist_prefix!r}, not {prefix!r}"
        )
    _state_dir(supervisor)
    return env.get("SUPERVISOR_API_PORT", "80")


async def _unload_keepalive(svc: LaunchAgent) -> None:
    """Unload a KeepAlive job: a signal would only have launchd restart it."""
    if not (await svc.status()).loaded:
        return
    await svc.unload()
    loop = asyncio.get_running_loop()
    deadline = loop.time() + UNLOAD_TIMEOUT
    while (await svc.status()).loaded:
        if loop.time() >= deadline:
            raise LayoutError(f"{svc.label} is still loaded after {UNLOAD_TIMEOUT}s")
        await asyncio.sleep(0.5)


async def _stop_children(labels: list[str], make: type[LaunchAgent]) -> None:
    for label in labels:
        timeout = STOP_TIMEOUT if label.endswith(".core") else APP_STOP_TIMEOUT
        _say(f"stopping {label}")
        await make(label).bootout(timeout)


async def _wait_api(port: str) -> None:
    host = "127.0.0.1" if port == "80" else f"127.0.0.1:{port}"
    url = f"http://{host}/supervisor/ping"
    loop = asyncio.get_running_loop()
    deadline = loop.time() + API_WAIT

    def ping() -> bool:
        try:
            with urllib.request.urlopen(url, timeout=5) as reply:  # noqa: S310
                return reply.status == 200
        except urllib.error.URLError, OSError:
            return False

    while not await loop.run_in_executor(None, ping):
        if loop.time() >= deadline:
            raise LayoutError(f"the Supervisor did not answer on {url}")
        await asyncio.sleep(2)
    _say(f"Supervisor answers on {url}")


def _stash_dir(prefix: str) -> Path:
    """Return where the session Supervisor's plist waits during the system layout."""
    return (
        Path.home() / "Library" / "Application Support" / "ha-macos" / prefix
    ) / "session-layout"


def _stash(agents: Path, prefix: str, stash: Path) -> None:
    """Keep the Supervisor's plist aside, so the next login doesn't start it."""
    stash.mkdir(mode=0o700, parents=True, exist_ok=True)
    shutil.move(
        agents / f"{prefix}.supervisor.plist", stash / f"{prefix}.supervisor.plist"
    )


def _drop(agents: Path, children: list[str]) -> None:
    """Delete Core's and the apps' agent plists (the Supervisor rewrites them)."""
    for label in children:
        (agents / f"{label}.plist").unlink(missing_ok=True)


def _restore(prefix: str, agents: Path, stash: Path, supervisor: dict | None) -> Path:
    """Put the Supervisor's plist back, or derive it from the daemon's."""
    agent_plist = agents / f"{prefix}.supervisor.plist"
    saved = stash / agent_plist.name
    if saved.exists():
        shutil.move(saved, agent_plist)
        return agent_plist
    if supervisor is None:
        raise LayoutError("no Supervisor plist to restore")
    data = {
        key: supervisor[key]
        for key in (
            "Label",
            "WorkingDirectory",
            "StandardOutPath",
            "StandardErrorPath",
            "ThrottleInterval",
        )
        if key in supervisor
    }
    environment = dict(supervisor.get("EnvironmentVariables", {}))
    environment.pop(launchd.ENV_LAYOUT, None)
    data |= {
        "ProgramArguments": supervisor["ProgramArguments"],
        "EnvironmentVariables": environment,
        "RunAtLoad": True,
        "KeepAlive": True,
    }
    with agent_plist.open("wb") as file:
        plistlib.dump(data, file)
    return agent_plist


async def _start_session_supervisor(prefix: str, agent_plist: Path) -> None:
    agent = LaunchAgent(f"{prefix}.supervisor")
    agent.plist_path = agent_plist
    if not (await agent.status()).loaded:
        await agent.bootstrap()


async def to_system(
    *, prefix: str, agents: Path, daemons: Path, dry_run: bool = False
) -> None:
    """Move the Supervisor, Core and apps from the session to the system domain."""
    _check_not_a_job(prefix)
    agent_plist = agents / f"{prefix}.supervisor.plist"
    if await asyncio.to_thread((daemons / f"{prefix}.supervisor.plist").exists):
        raise LayoutError("already in the system layout")
    if not await asyncio.to_thread(agent_plist.exists):
        raise LayoutError(f"no session Supervisor at {agent_plist}")
    supervisor = await asyncio.to_thread(_read, agent_plist)
    port = _check_plist(supervisor, prefix)
    state = _state_dir(supervisor)
    if not await _in_gui_session():
        raise LayoutError("no GUI session: run this in the logged-in user's Terminal")
    version = (await helper.call("ping")).get("version", 1)
    if version < 2:
        raise LayoutError(
            f"the root helper is version {version}; install version 2 first "
            "(sudo macos/helper/install.sh)"
        )
    children = await _session_children(agents, prefix)
    _say(
        f"plan: stop {prefix}.supervisor, then {', '.join(children) or 'nothing'}; "
        f"keep the Supervisor's plist in {_stash_dir(prefix)}; start "
        f"{prefix}.supervisor as a LaunchDaemon running as {_user()}"
    )
    if dry_run:
        return

    stashed = False
    # Set by the worker thread that defines the daemon: a thread keeps running
    # after Ctrl-C, and the rollback must not decide before it has finished.
    defined = threading.Event()
    defining = False
    environment = dict(supervisor.get("EnvironmentVariables", {}))
    environment[launchd.ENV_LAYOUT] = launchd.LAYOUT_SYSTEM
    daemon = UserDaemon(f"{prefix}.supervisor", state_dir=state)

    def define() -> None:
        try:
            daemon.write_plist(
                supervisor["ProgramArguments"],
                environment=environment,
                working_directory=Path(supervisor["WorkingDirectory"]),
                stdout=Path(supervisor["StandardOutPath"]),
                stderr=Path(
                    supervisor.get("StandardErrorPath", supervisor["StandardOutPath"])
                ),
                keep_alive=True,
                throttle_interval=int(supervisor.get("ThrottleInterval", 10)),
            )
        finally:
            defined.set()

    try:
        await _unload_keepalive(LaunchAgent(f"{prefix}.supervisor"))
        # A request the Supervisor sent the helper just before it stopped may
        # still be queued there: let it finish, then look again.
        await helper.call("ping")
        children = await _session_children(agents, prefix)
        await _stop_children(children, LaunchAgent)
        await asyncio.to_thread(_stash, agents, prefix, _stash_dir(prefix))
        stashed = True
        await asyncio.to_thread(_drop, agents, children)
        defining = True
        await asyncio.to_thread(define)
        await daemon.bootstrap()
        await _wait_api(port)
        if left := await _loaded(launchd.domain(), prefix):
            raise LayoutError(f"still loaded in the session: {', '.join(sorted(left))}")
    except BaseException as err:
        _say(f"the switch failed ({err!r}); going back to the session layout")
        if defining:
            await asyncio.to_thread(defined.wait, 60)
        if not stashed:
            await _start_session_supervisor(prefix, agent_plist)
        elif await asyncio.to_thread((daemons / f"{prefix}.supervisor.plist").exists):
            await to_session(prefix=prefix, agents=agents, daemons=daemons)
        else:
            restored = await asyncio.to_thread(
                _restore, prefix, agents, _stash_dir(prefix), None
            )
            await _start_session_supervisor(prefix, restored)
        if isinstance(err, Exception):
            raise LayoutError(f"system layout failed: {err}") from err
        raise


async def to_session(
    *, prefix: str, agents: Path, daemons: Path, dry_run: bool = False
) -> None:
    """Move the Supervisor, Core and apps back into the user's session."""
    _check_not_a_job(prefix)
    user = _user()
    agent_plist = agents / f"{prefix}.supervisor.plist"
    daemon_plist = daemons / f"{prefix}.supervisor.plist"
    if await asyncio.to_thread(agent_plist.exists):
        raise LayoutError("already in the session layout")
    if not await asyncio.to_thread(daemon_plist.exists):
        await _restore_stash_only(prefix, agents)
        return
    raw = await asyncio.to_thread(_read, daemon_plist)
    if raw.get("UserName") != user:
        raise LayoutError(f"{daemon_plist} is not a user service of {user}")
    probe = UserDaemon(f"{prefix}.supervisor", state_dir=Path(daemons))
    probe.plist_path = daemon_plist
    daemon = await asyncio.to_thread(probe.read_plist)
    assert daemon is not None
    port = _check_plist(daemon, prefix)
    state = _state_dir(daemon)
    # Without a GUI session there is nowhere to start a LaunchAgent: tearing
    # the system layout down now would leave Home Assistant off until a login.
    if not await _in_gui_session():
        raise LayoutError("no GUI session: log in at the Mac, then switch back")
    children = await _system_children(daemons, prefix, user)
    _say(
        f"plan: stop {prefix}.supervisor, then {', '.join(children) or 'nothing'}; "
        f"remove their LaunchDaemons; start {prefix}.supervisor as a LaunchAgent"
    )
    if dry_run:
        return

    supervisor_daemon = UserDaemon(f"{prefix}.supervisor", state_dir=state)
    try:
        await _unload_keepalive(supervisor_daemon)
        await helper.call("ping")  # let the helper finish the Supervisor's requests
        children = await _system_children(daemons, prefix, user)
        await _stop_children(children, UserDaemon)
    except BaseException as err:
        # Nothing removed yet: the system Supervisor starts Core and apps again.
        _say(f"the switch failed ({err!r}); starting the system Supervisor again")
        await supervisor_daemon.bootstrap()
        raise
    for label in children:
        await asyncio.to_thread(UserDaemon(label, state_dir=state).remove_plist)
    await asyncio.to_thread(
        UserDaemon(f"{prefix}.supervisor", state_dir=state).remove_plist
    )
    agent_plist = await asyncio.to_thread(
        _restore, prefix, agents, _stash_dir(prefix), daemon
    )
    await _start_session_supervisor(prefix, agent_plist)
    await _wait_api(port)
    if left := await _system_children(daemons, prefix, user):
        raise LayoutError(f"still defined in the system domain: {', '.join(left)}")


async def _restore_stash_only(prefix: str, agents: Path) -> None:
    """Start the saved session Supervisor when no system one is defined.

    That is the state after uninstalling the helper in the system layout:
    the daemons are gone and only the saved session plist is left.
    """
    saved = _stash_dir(prefix) / f"{prefix}.supervisor.plist"
    if not await asyncio.to_thread(saved.exists):
        raise LayoutError("neither a system Supervisor nor a saved session one")
    port = _check_plist(await asyncio.to_thread(_read, saved), prefix)
    if not await _in_gui_session():
        raise LayoutError("no GUI session: log in at the Mac, then switch back")
    _say(f"plan: no system Supervisor; start the saved {prefix}.supervisor agent")
    agent_plist = await asyncio.to_thread(_restore, prefix, agents, saved.parent, None)
    await _start_session_supervisor(prefix, agent_plist)
    await _wait_api(port)


def main() -> None:
    """Command line entry."""
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument(
        "layout", choices=[launchd.LAYOUT_SYSTEM, launchd.LAYOUT_SESSION]
    )
    parser.add_argument("--dry-run", action="store_true", help="print the plan only")
    parser.add_argument("--prefix", default="ha-macos", help="launchd prefix")
    args = parser.parse_args()
    switch = to_system if args.layout == launchd.LAYOUT_SYSTEM else to_session
    try:
        asyncio.run(
            switch(
                prefix=args.prefix,
                agents=launchd.LAUNCH_AGENTS,
                daemons=launchd.LAUNCH_DAEMONS,
                dry_run=args.dry_run,
            )
        )
    except (LayoutError, helper.HelperError, LaunchdError) as err:
        sys.exit(f"error: {err}")


if __name__ == "__main__":
    main()
