"""Test the two launchd layouts: the user's session and the system domain."""

import json
from pathlib import Path
import plistlib
import signal
import socket
import stat
import subprocess
import tempfile
import threading
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest

from supervisor.native import helper, launchd
from supervisor.native.app import AgentService
from supervisor.native.launchd import (
    LaunchAgent,
    LaunchdError,
    ServiceStatus,
    UserDaemon,
    layout,
    read_env_file,
    service,
    write_env_file,
)

RUNNING = ServiceStatus(True, True, 4242, None)
STOPPED = ServiceStatus(True, False, None, 0)
GONE = ServiceStatus(False, False, None, None)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        pytest.param(None, "session", id="default"),
        pytest.param("session", "session", id="session"),
        pytest.param("system", "system", id="system"),
    ],
)
def test_layout(monkeypatch: pytest.MonkeyPatch, value: str | None, expected: str):
    """Test the layout follows SUPERVISOR_LAUNCHD_LAYOUT, session by default."""
    if value is None:
        monkeypatch.delenv(launchd.ENV_LAYOUT, raising=False)
    else:
        monkeypatch.setenv(launchd.ENV_LAYOUT, value)
    assert layout() == expected


def test_unknown_layout_is_refused(monkeypatch: pytest.MonkeyPatch):
    """Test a typo in the layout is an error, not a silent session layout."""
    monkeypatch.setenv(launchd.ENV_LAYOUT, "daemon")
    with pytest.raises(LaunchdError):
        layout()


@pytest.mark.parametrize(
    ("value", "cls"),
    [
        pytest.param("session", LaunchAgent, id="session"),
        pytest.param("system", UserDaemon, id="system"),
    ],
)
def test_core_and_apps_follow_the_layout(
    monkeypatch: pytest.MonkeyPatch, value: str, cls: type
):
    """Test Core's and apps' services are LaunchAgents or user daemons."""
    monkeypatch.setenv(launchd.ENV_LAYOUT, value)
    assert type(service("ha-macos.core")) is cls
    assert type(AgentService("ha-macos.app.local_nimbled").agent) is cls


def test_user_daemon_names(monkeypatch: pytest.MonkeyPatch, tmp_path: Path):
    """Test a user daemon lives in the system domain and LaunchDaemons."""
    monkeypatch.setattr(launchd, "LAUNCH_DAEMONS", tmp_path)
    daemon = UserDaemon("ha-macos.app.local_nimbled")
    assert daemon.target == "system/ha-macos.app.local_nimbled"
    assert daemon.plist_path == tmp_path / "ha-macos.app.local_nimbled.plist"
    assert daemon._params == {"prefix": "ha-macos", "name": "app.local_nimbled"}
    assert daemon.read_plist() is None


def test_user_daemon_definition_goes_through_the_helper(tmp_path: Path):
    """Test the token goes to a 0600 file and the definition to the helper."""
    daemon = UserDaemon("ha-macos.core", state_dir=tmp_path / "launchd")
    environment = {"SUPERVISOR": "127.0.0.1:4358", "SUPERVISOR_TOKEN": "s3cr3t"}
    with patch.object(helper, "call_sync", MagicMock(return_value={})) as call_sync:
        daemon.write_plist(
            ["/x/bin/hass", "-c", "/x/config"],
            environment=environment,
            working_directory=tmp_path,
            stdout=tmp_path / "stdout.log",
            stderr=tmp_path / "stderr.log",
        )
        env_file = tmp_path / "launchd" / "ha-macos.core.env"
        assert stat.S_IMODE(env_file.stat().st_mode) == 0o600
        assert stat.S_IMODE(env_file.parent.stat().st_mode) == 0o700
        assert read_env_file(env_file) == environment
        assert call_sync.call_args_list == [
            call(
                "user_service",
                prefix="ha-macos",
                name="core",
                program=["/x/bin/hass", "-c", "/x/config"],
                env_file=str(env_file),
                working_directory=str(tmp_path),
                stdout=str(tmp_path / "stdout.log"),
                stderr=str(tmp_path / "stderr.log"),
                run_at_load=True,
                keep_alive=False,
            )
        ]
        assert "s3cr3t" not in repr(call_sync.call_args_list)
        daemon.remove_plist()
    assert call_sync.call_args_list[-1] == call(
        "user_unservice", prefix="ha-macos", name="core"
    )
    assert not env_file.exists()


@pytest.mark.parametrize(
    "value",
    [
        pytest.param("plain", id="plain"),
        pytest.param("", id="empty"),
        pytest.param('it\'s "quoted" $HOME `id` \\', id="shell"),
        pytest.param("two\nlines", id="newline"),
    ],
)
def test_env_file_round_trip(tmp_path: Path, value: str):
    """Test any value survives the env file, read back and by a real sh."""
    path = tmp_path / "x.env"
    write_env_file(path, {"VALUE": value, "TZ": "UTC"})
    assert read_env_file(path) == {"VALUE": value, "TZ": "UTC"}
    out = subprocess.run(  # noqa: S603
        ["/bin/sh", "-c", 'set -a; . "$1"; printf %s "$VALUE"', "sh", str(path)],
        capture_output=True,
        text=True,
        check=True,
    )
    assert out.stdout == value


def test_env_file_refuses_a_bad_name(tmp_path: Path):
    """Test a variable name sh would read as code is refused."""
    with pytest.raises(LaunchdError):
        write_env_file(tmp_path / "x.env", {"X;id": "1"})
    assert not (tmp_path / "x.env").exists()


def test_user_daemon_reads_back_what_the_supervisor_wrote(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
):
    """Test the wrapper plist reads back as program, environment and logs.

    Core compares the plist's ProgramArguments with its program to decide
    whether to define the service again, so the wrapper must not show.
    """
    monkeypatch.setattr(launchd, "LAUNCH_DAEMONS", tmp_path)
    env_file = tmp_path / "ha-macos.core.env"
    write_env_file(env_file, {"SUPERVISOR_TOKEN": "t"})
    (tmp_path / "ha-macos.core.plist").write_bytes(
        plistlib.dumps(
            {
                "Label": "ha-macos.core",
                "UserName": "u",
                "ProgramArguments": ["/bin/sh", "-c", "wrapper", "ha-macos.core"]
                + [
                    str(env_file),
                    "/x/out.log",
                    "/x/err.log",
                    "/x/bin/hass",
                    "-c",
                    "/c",
                ],
                "EnvironmentVariables": {"HOME": "/Users/u"},
            }
        )
    )
    data = UserDaemon("ha-macos.core", state_dir=tmp_path).read_plist()
    assert data is not None
    assert data["ProgramArguments"] == ["/x/bin/hass", "-c", "/c"]
    assert data["EnvironmentVariables"] == {"SUPERVISOR_TOKEN": "t"}
    assert data["StandardOutPath"] == "/x/out.log"
    assert data["StandardErrorPath"] == "/x/err.log"
    assert data["UserName"] == "u"


async def test_user_daemon_load_and_start_go_through_the_helper():
    """Test loading and starting are the helper's user verbs."""
    daemon = UserDaemon("ha-macos.app.local_nimbled")
    with patch.object(helper, "call", AsyncMock(return_value={})) as helper_call:
        await daemon.bootstrap()
        await daemon.kickstart()
    params = {"prefix": "ha-macos", "name": "app.local_nimbled"}
    assert helper_call.call_args_list == [
        call("user_bootstrap", **params),
        call("user_kickstart", **params),
    ]


@pytest.mark.parametrize("method", ["bootstrap", "kickstart"])
async def test_helper_refusal_is_a_launchd_error(method: str):
    """Test callers see a LaunchdError, which Core and apps already handle."""
    daemon = UserDaemon("ha-macos.core")
    refused = AsyncMock(side_effect=helper.HelperError("Root helper refused"))
    with patch.object(helper, "call", refused), pytest.raises(LaunchdError):
        await getattr(daemon, method)()


def test_helper_refusal_on_define_is_a_launchd_error(tmp_path: Path):
    """Test a refused definition is a LaunchdError too."""
    daemon = UserDaemon("ha-macos.core", state_dir=tmp_path)
    refused = MagicMock(side_effect=helper.HelperError("Root helper refused"))
    with patch.object(helper, "call_sync", refused), pytest.raises(LaunchdError):
        daemon.write_plist(
            ["/x"],
            environment={},
            working_directory=tmp_path,
            stdout=tmp_path / "o",
            stderr=tmp_path / "o",
        )


async def test_user_daemon_stop_signals_the_process_then_unloads():
    """Test SIGTERM goes straight to the process (same user), then the unload."""
    daemon = UserDaemon("ha-macos.core")
    statuses = iter([RUNNING, RUNNING, STOPPED, GONE])
    with (
        patch.object(
            UserDaemon, "status", AsyncMock(side_effect=lambda: next(statuses))
        ),
        patch.object(launchd.os, "kill") as kill,
        patch.object(helper, "call", AsyncMock(return_value={})) as helper_call,
        patch.object(launchd.asyncio, "sleep", AsyncMock()),
    ):
        await daemon.bootout(30)
    assert kill.call_args_list == [call(4242, signal.SIGTERM)]
    assert helper_call.call_args_list == [
        call("user_bootout", prefix="ha-macos", name="core")
    ]


async def test_stuck_user_daemon_gets_sigabrt_then_sigkill():
    """Test a Core ignoring SIGTERM gets SIGABRT (stacks), then SIGKILL."""
    daemon = UserDaemon("ha-macos.core")
    with (
        patch.object(
            UserDaemon,
            "status",
            AsyncMock(side_effect=[RUNNING, RUNNING, RUNNING, RUNNING, GONE]),
        ),
        patch.object(UserDaemon, "_wait_stopped", AsyncMock(return_value=False)),
        patch.object(launchd.os, "kill") as kill,
        patch.object(helper, "call", AsyncMock(return_value={})),
    ):
        await daemon.bootout(1, abort_first=True)
    assert kill.call_args_list == [
        call(4242, signal.SIGTERM),
        call(4242, signal.SIGABRT),
        call(4242, signal.SIGKILL),
    ]


async def test_signal_to_a_process_that_just_exited_is_ignored():
    """Test the process exiting between status and kill is not an error."""
    daemon = UserDaemon("ha-macos.core")
    with (
        patch.object(UserDaemon, "status", AsyncMock(return_value=RUNNING)),
        patch.object(launchd.os, "kill", side_effect=ProcessLookupError),
    ):
        await daemon._signal(signal.SIGTERM)


class FakeHelperSocket:
    """A one-thread stand-in for the root helper's socket."""

    def __init__(self, reply: dict[str, Any]) -> None:
        """Listen on a short socket path (macOS allows 104 bytes)."""
        self.dir = Path(tempfile.mkdtemp(dir="/tmp", prefix="hlp"))  # noqa: S108
        self.path = str(self.dir / "s")
        self.reply = reply
        self.requests: list[dict[str, Any]] = []
        self.server = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        self.server.bind(self.path)
        self.server.listen(1)
        self.thread = threading.Thread(target=self._serve, daemon=True)
        self.thread.start()

    def _serve(self) -> None:
        conn, _ = self.server.accept()
        with conn:
            data = b""
            while not data.endswith(b"\n"):
                data += conn.recv(65536)
            self.requests.append(json.loads(data))
            conn.sendall((json.dumps(self.reply) + "\n").encode())

    def close(self) -> None:
        """Stop listening and remove the socket."""
        self.thread.join(5)
        self.server.close()
        Path(self.path).unlink()
        self.dir.rmdir()


def test_call_sync_speaks_the_helper_protocol(monkeypatch: pytest.MonkeyPatch):
    """Test the blocking call sends one JSON line and returns the result."""
    fake = FakeHelperSocket({"ok": True, "result": {"label": "ha-macos.core"}})
    monkeypatch.setattr(helper, "SOCKET", fake.path)
    try:
        result = helper.call_sync("user_service", prefix="ha-macos", name="core")
    finally:
        fake.close()
    assert result == {"label": "ha-macos.core"}
    assert fake.requests == [
        {"command": "user_service", "prefix": "ha-macos", "name": "core"}
    ]


def test_call_sync_reports_a_refusal(monkeypatch: pytest.MonkeyPatch):
    """Test a refusal becomes a HelperError with the helper's reason."""
    fake = FakeHelperSocket({"ok": False, "error": "bad service name"})
    monkeypatch.setattr(helper, "SOCKET", fake.path)
    try:
        with pytest.raises(helper.HelperError, match="bad service name"):
            helper.call_sync("user_service", prefix="ha-macos", name="x")
    finally:
        fake.close()


def test_call_sync_without_helper(monkeypatch: pytest.MonkeyPatch):
    """Test a missing helper is a HelperError, not an OSError."""
    monkeypatch.setattr(helper, "SOCKET", "/tmp/no-such-helper-socket")  # noqa: S108
    with pytest.raises(helper.HelperError, match="not reachable"):
        helper.call_sync("ping")
