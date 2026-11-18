"""Test switching the native Supervisor between the session and system layouts."""

import asyncio
from collections.abc import Iterator
import os
from pathlib import Path
import plistlib
import pwd
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from supervisor.native import helper, launchd, layout
from supervisor.native.homeassistant import STOP_TIMEOUT
from supervisor.native.launchd import LaunchAgent, read_env_file

USER = pwd.getpwuid(os.getuid()).pw_name
GUI = launchd.domain()
OTBR = "ha-macos.app.core_openthread_border_router"


def _write(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(plistlib.dumps(data))


class World:
    """Plist folders, a fake launchd and root helper, and a record of actions."""

    def __init__(self, root: Path) -> None:
        """Make empty plist folders under root."""
        self.agents = root / "LaunchAgents"
        self.daemons = root / "LaunchDaemons"
        self.data = root / "data"
        self.stash = root / "Application Support" / "ha-macos" / "ha-macos" / "stash"
        self.agents.mkdir()
        self.daemons.mkdir()
        self.log: list[tuple] = []
        self.gui: set[str] | None = set()  # None: no GUI session
        self.system: dict[str, str | None] = {OTBR: None}  # label -> user
        self.helper_version = 2
        self.fail: str | None = None
        self.app_start_fails: set[str] = set()
        self.supervisor = {
            "Label": "ha-macos.supervisor",
            "ProgramArguments": ["/x/supervisor/bin/python", "-m", "supervisor"],
            "EnvironmentVariables": {
                "SUPERVISOR_DATA": str(self.data),
                "SUPERVISOR_API_PORT": "4358",
                "SUPERVISOR_LAUNCHD_PREFIX": "ha-macos",
                "HOME": "/Users/x",
            },
            "WorkingDirectory": "/x",
            "StandardOutPath": "/x/logs/supervisor.log",
            "StandardErrorPath": "/x/logs/supervisor.log",
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
        }

    # launchd

    def _services(self, labels: list[str]) -> str:
        # The status column isn't always a number: "(pe)", "(jt)", "=" occur.
        statuses = ["-", "0", "(pe)", "(jt)", "="]
        rows = "".join(
            f"\t\t   123      {statuses[i % len(statuses)]} \t{label}\n"
            for i, label in enumerate(labels)
        )
        disabled = '\tdisabled services = {\n\t\t"ha-macos.app.ghost" => enabled\n\t}\n'
        return f"\tservices = {{\n{rows}\t}}\n{disabled}"

    async def launchctl(self, *args: str, **_: Any) -> tuple[int, str]:
        """Answer launchctl print, managername and bootstrap."""
        if args[0] == "managername":
            return 0, "Aqua\n" if self.gui is not None else "Background\n"
        if args[0] == "print":
            target = args[1]
            if target == GUI:
                if self.gui is None:
                    return 113, ""
                return 0, self._services(sorted(self.gui))
            if target == "system":
                return 0, self._services(sorted(self.system))
            domain, _, label = target.rpartition("/")
            loaded = (
                label in self.system
                if domain == "system"
                else self.gui is not None and label in self.gui
            )
            if not loaded:
                return 113, ""
            user = self.system.get(label) if domain == "system" else None
            return 0, "\tstate = running\n" + (f"\tusername = {user}\n" if user else "")
        self.log.append(("launchctl", *args))
        if args[0] == "bootstrap":
            assert self.gui is not None
            self.gui.add(Path(args[2]).stem)
        return 0, ""

    # root helper

    async def helper_call(self, command: str, **params: Any) -> dict[str, Any]:
        """Answer like helper v2."""
        if command != "ping":
            self.log.append(("helper", command, params.get("name")))
        if command == self.fail:
            raise helper.HelperError(f"Root helper refused {command}")
        if command == "ping":
            return {"version": self.helper_version}
        label = f"{params['prefix']}.{params['name']}"
        if command == "user_bootstrap":
            self.system[label] = USER
        if command == "user_bootout":
            self.system.pop(label, None)
        return {}

    def helper_call_sync(self, command: str, **params: Any) -> dict[str, Any]:
        """Write and remove user plists as helper v2 does (in a worker thread)."""
        self.log.append(("helper", command, params.get("name")))
        if command == self.fail:
            raise helper.HelperError(f"Root helper refused {command}")
        label = f"{params['prefix']}.{params['name']}"
        path = self.daemons / f"{label}.plist"
        if command == "user_service":
            self.service_request = params
            args = ["/bin/sh", "-c", "wrapper", label, params["env_file"]]
            args += [params["stdout"], params["stderr"], *params["program"]]
            _write(
                path,
                {
                    "Label": label,
                    "UserName": USER,
                    "ProgramArguments": args,
                    "EnvironmentVariables": {"HOME": "/Users/x"},
                    "WorkingDirectory": params["working_directory"],
                    "KeepAlive": params["keep_alive"],
                    "ThrottleInterval": params.get("throttle_interval"),
                },
            )
        if command == "user_unservice":
            self.system.pop(label, None)
            path.unlink(missing_ok=True)
        return {}

    # Supervisor API

    def api(self, port: str, token: str, method: str, path: str) -> dict:
        """Answer like the new Supervisor: running; app starts per app_start_fails."""
        assert token == "cli-token"
        self.log.append(("api", method, path))
        if path == "info":
            return {"result": "ok", "data": {"state": "running"}}
        slug = path.split("/")[1]
        if slug in self.app_start_fails:
            return {"result": "error", "message": "no luck"}
        return {"result": "ok", "data": {}}

    # layouts

    def _cli_token(self) -> None:
        self.data.mkdir(parents=True, exist_ok=True)
        (self.data / "cli.json").write_text('{"access_token": "cli-token"}')

    def session(self) -> None:
        """Lay out a running session install: Supervisor, Core and two apps."""
        self._cli_token()
        _write(self.agents / "ha-macos.supervisor.plist", self.supervisor)
        for label in ("ha-macos.core", "ha-macos.app.b", "ha-macos.app.a"):
            _write(self.agents / f"{label}.plist", {"Label": label})
        _write(self.agents / "ha-macos-dev.core.plist", {"Label": "ha-macos-dev.core"})
        assert self.gui is not None
        self.gui |= {
            "ha-macos.supervisor",
            "ha-macos.core",
            "ha-macos.app.a",
            "ha-macos.app.b",
            # loaded without a plist: still found, still stopped
            "ha-macos.app.c",
        }

    def system_layout(self) -> None:
        """Lay out a running system install (as the system Supervisor made it)."""
        self._cli_token()
        env_file = self.data / "launchd" / "ha-macos.supervisor.env"
        launchd.write_env_file(
            env_file,
            self.supervisor["EnvironmentVariables"] | {launchd.ENV_LAYOUT: "system"},
        )
        args = ["/bin/sh", "-c", "wrapper", "ha-macos.supervisor", str(env_file)]
        args += ["/x/logs/supervisor.log", "/x/logs/supervisor.log"]
        args += self.supervisor["ProgramArguments"]
        _write(
            self.daemons / "ha-macos.supervisor.plist",
            self.supervisor
            | {
                "UserName": USER,
                "ProgramArguments": args,
                "EnvironmentVariables": {"HOME": "/Users/x"},
            },
        )
        for label in ("ha-macos.core", "ha-macos.app.a"):
            _write(self.daemons / f"{label}.plist", {"Label": label, "UserName": USER})
        _write(self.daemons / f"{OTBR}.plist", {"Label": OTBR})
        self.system |= {
            "ha-macos.supervisor": USER,
            "ha-macos.core": USER,
            "ha-macos.app.a": USER,
            "ha-macos.app.d": USER,  # loaded without a plist
        }


@pytest.fixture(name="world")
def fixture_world(tmp_path: Path) -> Iterator[World]:
    """Patch every side effect of the switch into the world."""
    world = World(tmp_path)

    async def unload(svc: LaunchAgent) -> None:
        world.log.append(("unload", type(svc).__name__, svc.label))
        if type(svc) is LaunchAgent:
            assert world.gui is not None
            world.gui.discard(svc.label)
        else:
            world.system.pop(svc.label, None)

    async def bootout(self: LaunchAgent, timeout: float, **_: Any) -> None:
        world.log.append(("stop", type(self).__name__, self.label, timeout))
        if type(self) is LaunchAgent:
            assert world.gui is not None
            world.gui.discard(self.label)
        else:
            world.system.pop(self.label, None)

    with (
        patch.object(launchd, "LAUNCH_DAEMONS", world.daemons),
        patch.object(launchd, "_launchctl", side_effect=world.launchctl),
        patch.object(helper, "call", side_effect=world.helper_call),
        patch.object(helper, "call_sync", side_effect=world.helper_call_sync),
        patch.object(layout, "_unload_keepalive", side_effect=unload),
        patch.object(LaunchAgent, "bootout", bootout),
        patch.object(layout, "_wait_api", AsyncMock()),
        patch.object(layout, "_stash_dir", lambda prefix: world.stash),
        patch.object(layout, "_api", side_effect=world.api),
    ):
        yield world


def _args(world: World, **extra: Any) -> dict[str, Any]:
    return {"prefix": "ha-macos", "agents": world.agents, "daemons": world.daemons}


def _names(folder: Path, pattern: str = "*.plist") -> list[str]:
    return sorted(path.name for path in folder.glob(pattern))


async def test_to_system_stops_in_order_and_starts_the_supervisor_daemon(world: World):
    """Test Supervisor first, then Core, then apps; then the daemon starts."""
    await asyncio.to_thread(world.session)
    await layout.to_system(**_args(world))
    assert world.log == [
        ("unload", "LaunchAgent", "ha-macos.supervisor"),
        ("stop", "LaunchAgent", "ha-macos.core", STOP_TIMEOUT),
        ("stop", "LaunchAgent", "ha-macos.app.a", layout.APP_STOP_TIMEOUT),
        ("stop", "LaunchAgent", "ha-macos.app.b", layout.APP_STOP_TIMEOUT),
        ("stop", "LaunchAgent", "ha-macos.app.c", layout.APP_STOP_TIMEOUT),
        ("helper", "user_service", "supervisor"),
        ("helper", "user_bootstrap", "supervisor"),
        # A restart, not a boot, for the new Supervisor: it starts no apps.
        ("api", "GET", "info"),
        ("api", "POST", "addons/a/start"),
        ("api", "POST", "addons/b/start"),
        ("api", "POST", "addons/c/start"),
    ]
    request = world.service_request
    assert request["keep_alive"] is True
    assert request["throttle_interval"] == 10
    assert request["program"] == world.supervisor["ProgramArguments"]
    env = await asyncio.to_thread(read_env_file, Path(request["env_file"]))
    assert env[launchd.ENV_LAYOUT] == "system"
    assert env["SUPERVISOR_DATA"] == str(world.data)
    # Only the Supervisor's plist is kept; Core's and the apps' are rewritten.
    assert await asyncio.to_thread(_names, world.stash) == ["ha-macos.supervisor.plist"]
    # The next login loads nothing of ours; the dev Supervisor's agent stays.
    assert await asyncio.to_thread(_names, world.agents) == ["ha-macos-dev.core.plist"]


async def test_dry_run_changes_nothing(world: World):
    """Test --dry-run only prints the plan."""
    await asyncio.to_thread(world.session)
    before = await asyncio.to_thread(_names, world.agents)
    await layout.to_system(**_args(world), dry_run=True)
    assert world.log == []
    assert await asyncio.to_thread(_names, world.agents) == before


async def test_to_system_needs_helper_2(world: World):
    """Test an old helper is reported before anything stops."""
    await asyncio.to_thread(world.session)
    world.helper_version = 1
    with pytest.raises(layout.LayoutError, match="version 1"):
        await layout.to_system(**_args(world))
    assert world.log == []


async def test_to_system_twice_is_refused(world: World):
    """Test the switch doesn't run on an install already in the system layout."""
    await asyncio.to_thread(world.system_layout)
    with pytest.raises(layout.LayoutError, match="already"):
        await layout.to_system(**_args(world))
    assert world.log == []


@pytest.mark.parametrize(
    ("change", "error"),
    [
        pytest.param({"WorkingDirectory": None}, "lacks WorkingDirectory", id="key"),
        pytest.param(
            {"EnvironmentVariables": {"SUPERVISOR_LAUNCHD_PREFIX": "ha-macos-dev"}},
            "prefix",
            id="prefix",
        ),
        pytest.param({"EnvironmentVariables": {}}, "SUPERVISOR_DATA", id="data"),
    ],
)
async def test_to_system_checks_the_plist_first(
    world: World, change: dict[str, Any], error: str
):
    """Test a plist it can't use is refused before anything stops."""
    merged = world.supervisor | change
    world.supervisor = {
        key: value for key, value in merged.items() if value is not None
    }
    await asyncio.to_thread(world.session)
    with pytest.raises(layout.LayoutError, match=error):
        await layout.to_system(**_args(world))
    assert world.log == []


async def test_not_from_a_supervisor_job(world: World, monkeypatch: pytest.MonkeyPatch):
    """Test the tool refuses to run inside a job it would stop."""
    await asyncio.to_thread(world.session)
    monkeypatch.setenv("XPC_SERVICE_NAME", "ha-macos.app.x")
    with pytest.raises(layout.LayoutError, match="Terminal"):
        await layout.to_system(**_args(world))
    assert world.log == []


async def test_failed_start_goes_back_to_the_session(world: World):
    """Test a Supervisor daemon that can't start puts the session layout back."""
    await asyncio.to_thread(world.session)
    world.fail = "user_bootstrap"
    with pytest.raises(layout.LayoutError, match="system layout failed"):
        await layout.to_system(**_args(world))
    assert "ha-macos.supervisor.plist" in await asyncio.to_thread(_names, world.agents)
    assert not await asyncio.to_thread(
        (world.daemons / "ha-macos.supervisor.plist").exists
    )
    assert world.log[-1] == (
        "launchctl",
        "bootstrap",
        GUI,
        str(world.agents / "ha-macos.supervisor.plist"),
    )


async def test_failure_before_the_stash_restarts_the_session_supervisor(world: World):
    """Test a Core that won't stop leaves the session as it was, Supervisor back."""
    await asyncio.to_thread(world.session)

    async def stuck(self: LaunchAgent, timeout: float, **_: Any) -> None:
        raise launchd.LaunchdError(f"{self.label} is still loaded after bootout")

    with (
        patch.object(LaunchAgent, "bootout", stuck),
        pytest.raises(layout.LayoutError, match="still loaded"),
    ):
        await layout.to_system(**_args(world))
    assert "ha-macos.supervisor.plist" in await asyncio.to_thread(_names, world.agents)
    assert world.log[-1][:3] == ("launchctl", "bootstrap", GUI)


async def test_interrupt_after_the_stash_puts_the_session_back(world: World):
    """Test Ctrl-C half way leaves a Supervisor plist that the next login starts."""
    await asyncio.to_thread(world.session)

    def interrupted(command: str, **params: Any) -> dict[str, Any]:
        raise KeyboardInterrupt

    with (
        patch.object(helper, "call_sync", side_effect=interrupted),
        pytest.raises(KeyboardInterrupt),
    ):
        await layout.to_system(**_args(world))
    assert "ha-macos.supervisor.plist" in await asyncio.to_thread(_names, world.agents)
    assert world.log[-1][:3] == ("launchctl", "bootstrap", GUI)


async def test_to_session_leaves_root_daemons_alone(world: World):
    """Test the way back stops and removes only our user daemons."""
    await asyncio.to_thread(world.system_layout)
    await layout.to_session(**_args(world))
    assert world.log == [
        ("unload", "UserDaemon", "ha-macos.supervisor"),
        ("stop", "UserDaemon", "ha-macos.core", STOP_TIMEOUT),
        ("stop", "UserDaemon", "ha-macos.app.a", layout.APP_STOP_TIMEOUT),
        ("stop", "UserDaemon", "ha-macos.app.d", layout.APP_STOP_TIMEOUT),
        ("helper", "user_unservice", "core"),
        ("helper", "user_unservice", "app.a"),
        ("helper", "user_unservice", "app.d"),
        ("helper", "user_unservice", "supervisor"),
        (
            "launchctl",
            "bootstrap",
            GUI,
            str(world.agents / "ha-macos.supervisor.plist"),
        ),
        ("api", "GET", "info"),
        ("api", "POST", "addons/a/start"),
        ("api", "POST", "addons/d/start"),
    ]
    assert OTBR in world.system
    assert await asyncio.to_thread((world.daemons / f"{OTBR}.plist").exists)


async def test_to_session_without_saved_plist_derives_one(world: World):
    """Test an install set up in the system layout can still go to session."""
    await asyncio.to_thread(world.system_layout)
    await layout.to_session(**_args(world))
    data = await asyncio.to_thread(
        lambda: plistlib.loads(
            (world.agents / "ha-macos.supervisor.plist").read_bytes()
        )
    )
    assert data["ProgramArguments"] == world.supervisor["ProgramArguments"]
    assert launchd.ENV_LAYOUT not in data["EnvironmentVariables"]
    assert data["EnvironmentVariables"]["SUPERVISOR_DATA"] == str(world.data)
    assert "UserName" not in data
    assert data["KeepAlive"] is True
    assert data["RunAtLoad"] is True


async def test_to_session_needs_a_gui_session(world: World):
    """Test logged out (over SSH), nothing is torn down: HA would stay off."""
    await asyncio.to_thread(world.system_layout)
    world.gui = None
    with pytest.raises(layout.LayoutError, match="log in"):
        await layout.to_session(**_args(world))
    assert world.log == []
    assert world.system["ha-macos.supervisor"] == USER


async def test_to_session_refuses_someone_elses_supervisor(world: World):
    """Test a Supervisor daemon that isn't this user's is never touched."""
    await asyncio.to_thread(
        _write,
        world.daemons / "ha-macos.supervisor.plist",
        world.supervisor | {"UserName": "root"},
    )
    with pytest.raises(layout.LayoutError, match="not a user service"):
        await layout.to_session(**_args(world))
    assert world.log == []


async def test_round_trip(world: World):
    """Test system then session gives back the original Supervisor plist."""
    await asyncio.to_thread(world.session)
    await layout.to_system(**_args(world))
    await layout.to_session(**_args(world))
    data = await asyncio.to_thread(
        lambda: plistlib.loads(
            (world.agents / "ha-macos.supervisor.plist").read_bytes()
        )
    )
    assert data == world.supervisor
    assert not await asyncio.to_thread(
        (world.daemons / "ha-macos.supervisor.plist").exists
    )


async def test_to_system_needs_the_aqua_session(world: World):
    """Test over SSH or from a job (no Aqua session) nothing starts."""
    await asyncio.to_thread(world.session)
    world.gui = None
    with pytest.raises(layout.LayoutError, match="GUI session"):
        await layout.to_system(**_args(world))
    assert world.log == []


async def test_to_session_from_the_stash_alone(world: World):
    """Test the saved session Supervisor starts when no system one is left.

    That is the state after uninstalling the helper in the system layout.
    """
    await asyncio.to_thread(world.session)
    await layout.to_system(**_args(world))
    await asyncio.to_thread((world.daemons / "ha-macos.supervisor.plist").unlink)
    world.system.clear()
    world.log.clear()
    await layout.to_session(**_args(world))
    assert world.log == [
        ("launchctl", "bootstrap", GUI, str(world.agents / "ha-macos.supervisor.plist"))
    ]
    data = await asyncio.to_thread(
        lambda: plistlib.loads(
            (world.agents / "ha-macos.supervisor.plist").read_bytes()
        )
    )
    assert data == world.supervisor


async def test_to_session_with_nothing_to_go_back_to(world: World):
    """Test neither layout found is reported, nothing done."""
    with pytest.raises(layout.LayoutError, match="neither"):
        await layout.to_session(**_args(world))
    assert world.log == []


async def test_to_session_failure_starts_the_system_supervisor_again(world: World):
    """Test a Core that won't stop on the way back leaves the system layout running."""
    await asyncio.to_thread(world.system_layout)

    async def stuck(self: LaunchAgent, timeout: float, **_: Any) -> None:
        raise launchd.LaunchdError(f"{self.label} is still loaded after bootout")

    with (
        patch.object(LaunchAgent, "bootout", stuck),
        pytest.raises(launchd.LaunchdError, match="still loaded"),
    ):
        await layout.to_session(**_args(world))
    assert world.log[-1] == ("helper", "user_bootstrap", "supervisor")
    assert await asyncio.to_thread((world.daemons / "ha-macos.supervisor.plist").exists)


def test_service_labels_reads_only_the_services_block():
    """Test rows with any status are read, the disabled block is not."""
    text = (
        "gui/501 = {\n\tservices = {\n"
        "\t\t   12      - \tha-macos.core\n"
        "\t\t    0   (pe) \tha-macos.app.a\n"
        "\t\t  130   (jt) \tha-macos.app.b\n"
        "\t\t    7      = \tha-macos.app.c\n"
        "\t}\n\tdisabled services = {\n"
        '\t\t"ha-macos.app.ghost" => enabled\n\t}\n}\n'
    )
    assert layout._service_labels(text) == {
        "ha-macos.core",
        "ha-macos.app.a",
        "ha-macos.app.b",
        "ha-macos.app.c",
    }


async def test_only_apps_that_ran_are_started_again(world: World):
    """Test an app that was stopped before the switch stays stopped."""
    await asyncio.to_thread(world.session)
    assert world.gui is not None
    world.gui.discard("ha-macos.app.b")  # defined, but stopped by the user
    await layout.to_system(**_args(world))
    starts = [entry[2] for entry in world.log if entry[:2] == ("api", "POST")]
    assert starts == ["addons/a/start", "addons/c/start"]


async def test_an_app_that_fails_to_start_is_reported_not_rolled_back(world: World):
    """Test the switch stands; the app that didn't start is named."""
    await asyncio.to_thread(world.session)
    world.app_start_fails = {"b"}
    with pytest.raises(layout.LayoutError, match="didn't start: b"):
        await layout.to_system(**_args(world))
    assert world.system["ha-macos.supervisor"] == USER
    assert await asyncio.to_thread(_names, world.agents) == ["ha-macos-dev.core.plist"]
    starts = [entry[2] for entry in world.log if entry[:2] == ("api", "POST")]
    assert starts == ["addons/a/start", "addons/b/start", "addons/c/start"]
