"""Test the root helper (macos/helper/ha_macos_helper.py): user services and safety.

User services are LaunchDaemons that run as the Supervisor's user. The helper
writes and loads them as root, so the tests check above all what it refuses:
a user verb must never replace, unload or remove a root daemon, and no
request may make root follow a link or run with a user-chosen environment.
"""

import importlib.util
import os
from pathlib import Path
import plistlib
import stat
import subprocess
import sys
from types import ModuleType
from typing import Any

import pytest

HELPER_PATH = Path(__file__).parents[2] / "macos" / "helper" / "ha_macos_helper.py"


def _load_helper() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ha_macos_helper", HELPER_PATH)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


helper_module = _load_helper()
ROOT_LABEL = "ha-macos.app.core_openthread_border_router"


class FakeLaunchd:
    """Records launchctl calls; a bootstrapped plist is loaded as its user."""

    def __init__(self) -> None:
        """Start with nothing loaded."""
        self.calls: list[tuple[str, ...]] = []
        self.loaded: dict[str, str | None] = {}
        self.unloads = True

    def __call__(self, *args: str) -> tuple[int, str]:
        """Answer one launchctl call."""
        self.calls.append(args)
        if args[0] == "print":
            label = args[1].split("/", 1)[1]
            if label not in self.loaded:
                return 113, "Could not find service"
            text = "\tstate = running\n\tpid = 4242\n"
            if user := self.loaded[label]:
                text += f"\tusername = {user}\n"
            return 0, text
        if args[0] == "bootstrap":
            data = plistlib.loads(Path(args[2]).read_bytes())
            self.loaded[data["Label"]] = data.get("UserName")
        if args[0] == "bootout" and self.unloads:
            self.loaded.pop(args[1].split("/", 1)[1], None)
        return 0, ""


@pytest.fixture(name="helper")
def fixture_helper(tmp_path: Path) -> Any:
    """Return a helper whose home and folders are in tmp_path."""
    home = tmp_path / "home"
    (home / "logs").mkdir(parents=True)
    helper = helper_module.Helper(
        os.getuid(),
        base=str(tmp_path / "base"),
        daemons=str(tmp_path / "LaunchDaemons"),
        domain="system",
        chown=False,
    )
    helper.home = os.path.realpath(home)
    helper.launchd = FakeLaunchd()
    helper._launchctl = helper.launchd  # noqa: SLF001
    return helper


def _home(helper: Any, *parts: str) -> str:
    return str(Path(helper.home, *parts))


def _service_request(helper: Any, **overrides: Any) -> dict[str, Any]:
    request = {
        "prefix": "ha-macos",
        "name": "core",
        "program": [sys.executable, "-c", "pass"],
        "env_file": _home(helper, "launchd", "ha-macos.core.env"),
        "working_directory": helper.home,
        "stdout": _home(helper, "logs", "stdout.log"),
        "stderr": _home(helper, "logs", "stderr.log"),
        "run_at_load": True,
        "keep_alive": False,
    }
    request.update(overrides)
    return request


def _plist(helper: Any, label: str) -> dict[str, Any]:
    return plistlib.loads(Path(helper.daemons, f"{label}.plist").read_bytes())


def _write_root_daemon(helper: Any, label: str = ROOT_LABEL) -> Path:
    """Write a plist like OTBR's: a LaunchDaemon with no UserName, i.e. root."""
    Path(helper.daemons).mkdir(parents=True, exist_ok=True)
    path = Path(helper.daemons, f"{label}.plist")
    path.write_bytes(
        plistlib.dumps({"Label": label, "ProgramArguments": ["/usr/bin/true"]})
    )
    return path


def _actions(helper: Any) -> list[tuple[str, ...]]:
    return [call for call in helper.launchd.calls if call[0] != "print"]


def test_ping_reports_version_3(helper: Any) -> None:
    """Version 2 has the user services; 3 keeps root apps' tokens out of plists."""
    assert helper.handle({"command": "ping"})["version"] == 3


def test_user_service_keeps_secrets_out_of_the_plist(helper: Any) -> None:
    """The plist runs sh as the user; no environment, no logs opened by launchd."""
    request = _service_request(helper, throttle_interval=10)
    assert helper.handle({"command": "user_service", **request}) == {
        "label": "ha-macos.core"
    }
    data = _plist(helper, "ha-macos.core")
    assert data["UserName"] == helper.user
    assert data["UserName"] != "root"
    assert data["ProcessType"] == "Interactive"
    assert data["ProgramArguments"] == [
        "/bin/sh",
        "-c",
        helper_module.USER_WRAPPER,
        "ha-macos.core",
        request["env_file"],
        request["stdout"],
        request["stderr"],
        sys.executable,
        "-c",
        "pass",
    ]
    # Only HOME: the token lives in the env file, which only the user reads.
    assert data["EnvironmentVariables"] == {"HOME": helper.home}
    assert "StandardOutPath" not in data
    assert "StandardErrorPath" not in data
    assert data["RunAtLoad"] is True
    assert data["KeepAlive"] is False
    assert data["ThrottleInterval"] == 10
    mode = Path(helper.daemons, "ha-macos.core.plist").stat().st_mode & 0o777
    assert mode == 0o644


def test_the_wrapper_runs_the_program_with_the_env_file(tmp_path: Path) -> None:
    """Run the wrapper for real: environment from the file, output to the logs."""
    env_file = tmp_path / "x.env"
    env_file.write_text("SUPERVISOR_TOKEN='a b'\nTZ=UTC\n")
    out, err = tmp_path / "out", tmp_path / "err"
    script = (
        "import os, sys; print(os.environ['SUPERVISOR_TOKEN']); "
        "print('e', file=sys.stderr)"
    )
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child
        os.execv(  # noqa: S606
            "/bin/sh",
            ["/bin/sh", "-c", helper_module.USER_WRAPPER, "label", str(env_file)]
            + [str(out), str(err), sys.executable, "-c", script],
        )
    _, status = os.waitpid(pid, 0)
    assert os.WEXITSTATUS(status) == 0
    assert out.read_text() == "a b\n"
    assert err.read_text() == "e\n"


@pytest.mark.parametrize(
    "name",
    ["supervisor", "core", "app.core_matter_server", "app.local_nimbled"],
)
def test_user_service_names(helper: Any, name: str) -> None:
    """The Supervisor, Core and apps can each be a user service."""
    helper.handle({"command": "user_service", **_service_request(helper, name=name)})
    assert _plist(helper, f"ha-macos.{name}")["Label"] == f"ha-macos.{name}"


@pytest.mark.parametrize(
    ("overrides", "error"),
    [
        pytest.param({"name": "helper"}, "bad service name", id="name-helper"),
        pytest.param({"name": "app.Bad"}, "bad service name", id="name-case"),
        pytest.param({"name": "app.x/../y"}, "bad service name", id="name-path"),
        pytest.param({"name": "core\n"}, "bad service name", id="name-newline"),
        pytest.param({"prefix": "other"}, "bad prefix", id="prefix"),
        pytest.param({"prefix": "ha-macos\n"}, "bad prefix", id="prefix-newline"),
        pytest.param({"program": []}, "bad program", id="program-empty"),
        pytest.param({"program": "python"}, "bad program", id="program-string"),
        pytest.param(
            {"program": ["python3", "-V"]}, "absolute path", id="program-relative"
        ),
        pytest.param(
            {"program": ["/etc/hosts"]}, "absolute path", id="program-not-executable"
        ),
        pytest.param(
            {"working_directory": "/tmp"},  # noqa: S108
            "outside",
            id="workdir-outside",
        ),
        pytest.param({"stdout": "/var/log/x.log"}, "outside", id="log-outside"),
        pytest.param({"stderr": "/etc/sudoers.d/x"}, "outside", id="stderr-outside"),
        pytest.param({"env_file": "/etc/profile"}, "outside", id="env-outside"),
        pytest.param({"throttle_interval": True}, "throttle", id="throttle-bool"),
        pytest.param({"throttle_interval": 0}, "throttle", id="throttle-zero"),
    ],
)
def test_user_service_refuses(
    helper: Any, overrides: dict[str, Any], error: str
) -> None:
    """Bad requests are refused and write nothing."""
    with pytest.raises(helper_module.Refused, match=error):
        helper.handle(
            {"command": "user_service", **_service_request(helper, **overrides)}
        )
    daemons = Path(helper.daemons)
    assert not daemons.exists() or not any(daemons.iterdir())


def test_user_service_never_replaces_a_root_daemon(helper: Any) -> None:
    """A root daemon with the same label (an app turned privileged) stays."""
    path = _write_root_daemon(helper)
    before = path.read_bytes()
    with pytest.raises(helper_module.Refused, match="not a user service"):
        helper.handle(
            {
                "command": "user_service",
                **_service_request(helper, name="app.core_openthread_border_router"),
            }
        )
    assert path.read_bytes() == before


@pytest.mark.parametrize(
    "command", ["user_bootstrap", "user_kickstart", "user_bootout", "user_unservice"]
)
def test_user_verbs_never_touch_a_root_daemon(helper: Any, command: str) -> None:
    """No user verb loads, starts, unloads or deletes a root daemon."""
    path = _write_root_daemon(helper)
    helper.launchd.loaded[ROOT_LABEL] = None
    with pytest.raises(helper_module.Refused):
        helper.handle(
            {
                "command": command,
                "prefix": "ha-macos",
                "name": "app.core_openthread_border_router",
            }
        )
    assert path.exists()
    assert ROOT_LABEL in helper.launchd.loaded
    assert not _actions(helper)


@pytest.mark.parametrize(
    "command", ["user_bootout", "user_kickstart", "user_bootstrap"]
)
def test_user_verbs_check_who_a_loaded_job_runs_as(helper: Any, command: str) -> None:
    """Leave a root job alone even when a user plist with its label exists.

    That happens if a root app's plist was deleted while its job stayed loaded
    and a user plist was then written with the same label.
    """
    helper.handle(
        {
            "command": "user_service",
            **_service_request(helper, name="app.core_openthread_border_router"),
        }
    )
    helper.launchd.loaded[ROOT_LABEL] = None  # loaded, runs as root
    with pytest.raises(helper_module.Refused, match="runs as root"):
        helper.handle(
            {
                "command": command,
                "prefix": "ha-macos",
                "name": "app.core_openthread_border_router",
            }
        )
    assert ROOT_LABEL in helper.launchd.loaded
    assert not _actions(helper)


def test_user_service_lifecycle(helper: Any) -> None:
    """Define, load, start, unload and remove a user service."""
    request = {"prefix": "ha-macos", "name": "core"}
    helper.handle({"command": "user_service", **_service_request(helper)})
    helper.handle({"command": "user_bootstrap", **request})
    assert helper.launchd.loaded["ha-macos.core"] == helper.user
    helper.handle({"command": "user_bootstrap", **request})  # loaded: no-op
    helper.handle({"command": "user_kickstart", **request})
    helper.handle({"command": "user_bootout", **request})
    helper.handle({"command": "user_unservice", **request})
    plist = str(Path(helper.daemons, "ha-macos.core.plist"))
    assert _actions(helper) == [
        ("bootstrap", "system", plist),
        ("kickstart", "system/ha-macos.core"),
        ("bootout", "system/ha-macos.core"),
    ]
    assert not Path(plist).exists()


def test_user_kickstart_needs_a_loaded_job(helper: Any) -> None:
    """Kickstarting a defined but unloaded service is refused, not a no-op."""
    helper.handle({"command": "user_service", **_service_request(helper)})
    with pytest.raises(helper_module.Refused, match="not loaded"):
        helper.handle(
            {"command": "user_kickstart", "prefix": "ha-macos", "name": "core"}
        )


def test_user_bootout_and_unservice_of_nothing_are_no_ops(helper: Any) -> None:
    """Stopping a service that isn't there (a first switch) succeeds."""
    request = {"prefix": "ha-macos", "name": "app.local_nimbled"}
    assert helper.handle({"command": "user_bootout", **request}) == {}
    assert helper.handle({"command": "user_unservice", **request}) == {}


def test_user_unservice_keeps_the_plist_of_a_job_that_wont_unload(
    helper: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A job still loaded after bootout keeps its plist, to be managed later."""
    request = {"prefix": "ha-macos", "name": "core"}
    helper.handle({"command": "user_service", **_service_request(helper)})
    helper.handle({"command": "user_bootstrap", **request})
    helper.launchd.unloads = False
    monkeypatch.setattr(helper_module.time, "sleep", lambda _: None)
    clock = iter(range(0, 10_000, 10))
    monkeypatch.setattr(helper_module.time, "monotonic", lambda: next(clock))
    with pytest.raises(helper_module.Refused, match="still loaded"):
        helper.handle({"command": "user_unservice", **request})
    assert Path(helper.daemons, "ha-macos.core.plist").exists()


# Root apps


def _installed_app(helper: Any) -> None:
    app = Path(helper.base, "apps", "ha-macos", "otbr", "1", "app")
    app.mkdir(parents=True)
    run = app / "run.sh"
    run.write_text("#!/bin/bash\n")
    run.chmod(0o755)


SUPERVISOR_VARIABLES = {
    "SUPERVISOR": "127.0.0.1:4358",
    "HASSIO": "127.0.0.1:4358",
    "SUPERVISOR_TOKEN": "secret-token",
    "HASSIO_TOKEN": "secret-token",
    "TZ": "UTC",
    "APP_DATA": "/x/data",
    "PORT_8081_TCP": "8081",
}


def _root_service(helper: Any, env: dict[str, str]) -> dict[str, Any]:
    return helper.handle(
        {
            "command": "service",
            "prefix": "ha-macos",
            "slug": "otbr",
            "version": "1",
            "program": "app/run.sh",
            "args": ["--flag"],
            "env": env,
            "working_directory": helper.home,
            "log_file": _home(helper, "logs", "otbr.log"),
            "run_at_load": True,
        }
    )


SHELLS = [sh for sh in ("/bin/sh", "/bin/bash", "/bin/dash") if Path(sh).exists()]


def _source(env_file: Path) -> dict[str, str]:
    """Return what sh exports after sourcing env_file, as the root wrapper does.

    sh starts with an empty environment and adds only PWD and SHLVL itself, so
    anything else (PATH among it) came from the file.
    """
    script = 'set -a; . "$1"; set +a; exec /usr/bin/env -0'
    out = subprocess.run(
        ["/bin/sh", "-c", script, "sh", str(env_file)],
        env={},
        capture_output=True,
        check=True,
    ).stdout.decode()
    variables = dict(item.split("=", 1) for item in out.split("\0") if item)
    for own in ("PWD", "SHLVL", "_"):
        variables.pop(own, None)
    return variables


def test_root_service_keeps_secrets_out_of_the_plist(helper: Any) -> None:
    """The plist runs sh, which sources a root-only file; it holds no token."""
    _installed_app(helper)
    assert _root_service(helper, SUPERVISOR_VARIABLES) == {"label": "ha-macos.app.otbr"}
    label = "ha-macos.app.otbr"
    plist = Path(helper.daemons, f"{label}.plist")
    data = _plist(helper, label)
    root = os.path.realpath(Path(helper.base, "apps", "ha-macos", "otbr", "1"))
    env_file = Path(helper.base, "env", f"{label}.env")
    assert data["ProgramArguments"] == [
        "/bin/sh",
        "-c",
        helper_module.ROOT_WRAPPER,
        label,
        str(env_file),
        str(Path(root, "app", "run.sh")),
        "--flag",
    ]
    assert data["EnvironmentVariables"] == {
        "PATH": helper_module.SYSTEM_PATH,
        "HOME": root,
    }
    assert "UserName" not in data
    assert data["StandardOutPath"] == _home(helper, "logs", "otbr.log")
    assert b"secret-token" not in plist.read_bytes()
    assert env_file.stat().st_mode & 0o777 == 0o600
    assert env_file.parent.stat().st_mode & 0o777 == 0o700
    assert _source(env_file) == SUPERVISOR_VARIABLES


def test_root_service_gets_only_the_supervisors_variables(helper: Any) -> None:
    """Never pass variables a root shell acts on (BASH_ENV, SHELLOPTS, DYLD_*)."""
    _installed_app(helper)
    _root_service(
        helper,
        SUPERVISOR_VARIABLES
        | {
            "HOME": helper.home,
            "PATH": "/evil:/usr/bin",
            "BASH_ENV": _home(helper, "x.sh"),
            "ENV": _home(helper, "x.sh"),
            "SHELLOPTS": "xtrace",
            "PS4": "$(id)",
            "DYLD_INSERT_LIBRARIES": _home(helper, "x.dylib"),
        },
    )
    env_file = Path(helper.base, "env", "ha-macos.app.otbr.env")
    assert _source(env_file) == SUPERVISOR_VARIABLES
    names = [line.split("=", 1)[0] for line in env_file.read_text().splitlines()]
    assert names == sorted(SUPERVISOR_VARIABLES)
    root = os.path.realpath(Path(helper.base, "apps", "ha-macos", "otbr", "1"))
    assert _plist(helper, "ha-macos.app.otbr")["EnvironmentVariables"] == {
        "PATH": helper_module.SYSTEM_PATH,
        "HOME": root,
    }


def test_root_env_values_are_data_never_code(helper: Any, tmp_path: Path) -> None:
    """A value with quotes, $(...), backticks or a newline reaches the app as is."""
    _installed_app(helper)
    marker = tmp_path / "ran"
    tricky = {
        "APP_A": f"$(touch {marker})",
        "APP_B": f"`touch {marker}`",
        "APP_C": 'it\'s "quoted"\nsecond line',
        "APP_D": f"x'; touch {marker}; '",
        "TZ": "",
    }
    _root_service(helper, tricky)
    assert _source(Path(helper.base, "env", "ha-macos.app.otbr.env")) == tricky
    assert not marker.exists()


def test_the_root_wrapper_runs_the_program_with_the_env_file(tmp_path: Path) -> None:
    """Run the root wrapper for real: the program gets the file's variables."""
    env_file = tmp_path / "x.env"
    env_file.write_text("SUPERVISOR_TOKEN='a b'\n")
    out = tmp_path / "out"
    script = (
        "import os, sys; "
        "open(sys.argv[1], 'w').write(os.environ['SUPERVISOR_TOKEN'] + sys.argv[2])"
    )
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child
        os.execv(  # noqa: S606
            "/bin/sh",
            ["/bin/sh", "-c", helper_module.ROOT_WRAPPER, "label", str(env_file)]
            + [sys.executable, "-c", script, str(out), "!"],
        )
    _, status = os.waitpid(pid, 0)
    assert os.WEXITSTATUS(status) == 0
    assert out.read_text() == "a b!"


@pytest.mark.parametrize(
    ("env", "args"),
    [
        ({"APP_X": "a\0b"}, []),
        ({"APP_X": 1}, []),
        ({"bad-key": "x"}, []),
        ({}, ["a\0b"]),
        ({}, [1]),
    ],
)
def test_root_service_refuses_bad_input(
    helper: Any, env: dict[str, Any], args: list[Any]
) -> None:
    """Refuse what sh or launchd can't carry, and write nothing."""
    _installed_app(helper)
    with pytest.raises(helper_module.Refused):
        helper.handle(
            {
                "command": "service",
                "prefix": "ha-macos",
                "slug": "otbr",
                "version": "1",
                "program": "app/run.sh",
                "args": args,
                "env": env,
                "working_directory": helper.home,
                "log_file": _home(helper, "logs", "otbr.log"),
            }
        )
    assert not Path(helper.daemons, "ha-macos.app.otbr.plist").exists()
    assert not Path(helper.base, "env", "ha-macos.app.otbr.env").exists()


def test_root_env_folder_must_be_a_real_folder(helper: Any, tmp_path: Path) -> None:
    """A link in place of base/env is refused, not followed."""
    _installed_app(helper)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    Path(helper.base, "env").symlink_to(elsewhere)
    with pytest.raises(helper_module.Refused, match="not a folder"):
        _root_service(helper, SUPERVISOR_VARIABLES)
    assert not list(elsewhere.iterdir())


def test_root_env_folder_must_be_roots(helper: Any) -> None:
    """In production a folder root doesn't own is refused (tests run as a user)."""
    _installed_app(helper)
    helper.chown = True
    with pytest.raises(helper_module.Refused, match="not root's"):
        _root_service(helper, SUPERVISOR_VARIABLES)
    assert not list(Path(helper.base, "env").iterdir())
    assert not Path(helper.daemons, "ha-macos.app.otbr.plist").exists()


def test_root_env_folder_is_made_private(helper: Any) -> None:
    """An existing env folder others could open is closed to them."""
    _installed_app(helper)
    folder = Path(helper.base, "env")
    folder.mkdir(parents=True)
    folder.chmod(0o755)
    _root_service(helper, SUPERVISOR_VARIABLES)
    assert folder.stat().st_mode & 0o777 == 0o700


def test_unservice_removes_the_env_file(helper: Any) -> None:
    """Removing a root service leaves no token behind."""
    _installed_app(helper)
    _root_service(helper, SUPERVISOR_VARIABLES)
    env_file = Path(helper.base, "env", "ha-macos.app.otbr.env")
    assert env_file.exists()
    helper.handle({"command": "unservice", "prefix": "ha-macos", "slug": "otbr"})
    assert not env_file.exists()
    assert not Path(helper.daemons, "ha-macos.app.otbr.plist").exists()


# copy: root reads what the user can't (OTBR's data) and writes into the home


def _copy(helper: Any, source: Path, dest: Path) -> dict[str, Any]:
    return helper.handle({"command": "copy", "source": str(source), "dest": str(dest)})


def test_copy_copies_regular_files_with_their_mode(helper: Any) -> None:
    """Copy regular files with mode and times; skip links."""
    source = Path(helper.home, "src")
    source.mkdir()
    (source / "settings").write_bytes(b"x" * 3_000_000)
    (source / "settings").chmod(0o600)
    os.utime(source / "settings", (1_700_000_000, 1_700_000_000))
    (source / "link").symlink_to("/etc/master.passwd")
    dest = Path(helper.home, "dst", "sub")
    _copy(helper, source, dest)
    assert sorted(p.name for p in dest.iterdir()) == ["settings"]
    assert (dest / "settings").read_bytes() == b"x" * 3_000_000
    assert stat.S_IMODE((dest / "settings").stat().st_mode) == 0o600
    assert (dest / "settings").stat().st_mtime == 1_700_000_000


def test_copy_never_writes_through_a_link(helper: Any, tmp_path: Path) -> None:
    """Refuse a link where a copy goes; its target stays as it was."""
    source = Path(helper.home, "src")
    source.mkdir()
    (source / "x").write_text("user chosen")
    victim = tmp_path / "victim"
    victim.write_text("root's file")
    dest = Path(helper.home, "dst")
    dest.mkdir()
    (dest / "x").symlink_to(victim)
    with pytest.raises(helper_module.Refused, match="cannot write"):
        _copy(helper, source, dest)
    assert victim.read_text() == "root's file"


def test_copy_never_follows_a_linked_folder(helper: Any, tmp_path: Path) -> None:
    """Refuse a folder on the way that is a link."""
    source = Path(helper.home, "src")
    source.mkdir()
    (source / "x").write_text("data")
    outside = tmp_path / "outside"
    outside.mkdir()
    Path(helper.home, "dst").symlink_to(outside)
    with pytest.raises(helper_module.Refused):
        _copy(helper, source, Path(helper.home, "dst"))
    assert not any(outside.iterdir())


def test_copy_into_an_existing_file_keeps_it(helper: Any) -> None:
    """Fill the Supervisor's pre-created empty file rather than replace it."""
    source = Path(helper.home, "src")
    source.mkdir()
    (source / "x").write_text("secret")
    dest = Path(helper.home, "dst")
    dest.mkdir()
    (dest / "x").touch()
    inode = (dest / "x").stat().st_ino
    _copy(helper, source, dest)
    assert (dest / "x").read_text() == "secret"
    assert (dest / "x").stat().st_ino == inode


def test_copy_never_writes_through_a_hard_link(helper: Any, tmp_path: Path) -> None:
    """Refuse a destination with a second name: root would overwrite that too."""
    source = Path(helper.home, "src")
    source.mkdir()
    (source / "x").write_text("user chosen")
    victim = Path(helper.home, "victim")
    victim.write_text("root's file")
    dest = Path(helper.home, "dst")
    dest.mkdir()
    os.link(victim, dest / "x")
    with pytest.raises(helper_module.Refused, match="not a plain file"):
        _copy(helper, source, dest)
    assert victim.read_text() == "root's file"


def test_copy_skips_a_hard_linked_source(helper: Any) -> None:
    """Skip a source file with a second name: it could name any file root reads."""
    source = Path(helper.home, "src")
    source.mkdir()
    (source / "plain").write_text("ok")
    secret = Path(helper.home, "secret")
    secret.write_text("root only")
    os.link(secret, source / "linked")
    dest = Path(helper.home, "dst")
    _copy(helper, source, dest)
    assert sorted(p.name for p in dest.iterdir()) == ["plain"]


def test_copy_never_makes_a_setuid_file(helper: Any) -> None:
    """Strip setuid, setgid and sticky: root writes the copy."""
    source = Path(helper.home, "src")
    source.mkdir()
    (source / "tool").write_text("#!/bin/sh\n")
    (source / "tool").chmod(0o4755)
    dest = Path(helper.home, "dst")
    _copy(helper, source, dest)
    assert stat.S_IMODE((dest / "tool").stat().st_mode) == 0o755


def test_copy_does_not_hang_on_a_fifo(helper: Any) -> None:
    """Skip a FIFO in the source and refuse one at the destination, never block."""
    source = Path(helper.home, "src")
    source.mkdir()
    os.mkfifo(source / "pipe")
    (source / "x").write_text("data")
    dest = Path(helper.home, "dst")
    dest.mkdir()
    _copy(helper, source, dest)
    assert sorted(p.name for p in dest.iterdir()) == ["x"]
    os.mkfifo(dest / "y")
    (source / "y").write_text("data")
    # Opening a FIFO without a reader for writing fails at once (ENXIO).
    with pytest.raises(helper_module.Refused, match="cannot write|not a plain file"):
        _copy(helper, source, dest)


@pytest.mark.parametrize("shell", SHELLS)
def test_the_wrapper_reports_a_missing_env_file(tmp_path: Path, shell: str) -> None:
    """A user service without its env file exits 78 and says why in the log."""
    out, err = tmp_path / "out", tmp_path / "err"
    pid = os.fork()
    if pid == 0:  # pragma: no cover - child
        os.execv(  # noqa: S606
            shell,
            [shell, "-c", helper_module.USER_WRAPPER, "label"]
            + [str(tmp_path / "missing.env"), str(out), str(err), "/usr/bin/true"],
        )
    _, status = os.waitpid(pid, 0)
    assert os.WEXITSTATUS(status) == 78
    assert err.read_text() == f"label: cannot read {tmp_path / 'missing.env'}\n"


@pytest.mark.parametrize("shell", SHELLS)
def test_the_root_wrapper_reports_a_missing_env_file(
    tmp_path: Path, shell: str
) -> None:
    """A root service without its env file exits 78 and says why on stderr."""
    result = subprocess.run(
        [shell, "-c", helper_module.ROOT_WRAPPER, "label"]
        + [str(tmp_path / "missing.env"), "/usr/bin/true"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert result.returncode == 78
    assert result.stderr == f"label: cannot read {tmp_path / 'missing.env'}\n"


def test_user_service_refuses_a_label_a_root_job_holds(helper: Any) -> None:
    """Never write a user plist under a loaded root job's label (it'd win at boot)."""
    helper.launchd.loaded[ROOT_LABEL] = None
    with pytest.raises(helper_module.Refused, match="runs as root"):
        helper.handle(
            {
                "command": "user_service",
                **_service_request(helper, name="app.core_openthread_border_router"),
            }
        )
    assert not Path(helper.daemons, f"{ROOT_LABEL}.plist").exists()
