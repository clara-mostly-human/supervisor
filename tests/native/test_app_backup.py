"""Test backing up native apps whose data root wrote (privileged apps).

Files this user can't read are simulated with mode 000; the fake root helper
copies them as root would: into the file the Supervisor created, which keeps
that file's owner, with the original's mode (0600 for OpenThread's settings).
"""

import asyncio
import os
from pathlib import Path
import shutil
from unittest.mock import MagicMock, patch

import pytest

from supervisor.exceptions import DockerError
from supervisor.native import app as native_app, helper
from supervisor.native.app import NativeApp


def _hide(path: Path) -> None:
    """Make a file this user can't read, like root's 0600 files."""
    path.chmod(0)


def _root_copy(calls: list[tuple[str, str]], before_copy=None):
    """Return a fake helper.call whose copy works like the root helper's."""

    def copy(source: Path, dest: Path) -> None:
        if before_copy is not None:
            before_copy(len(calls))
        for entry in source.iterdir():
            if entry.is_file() and not entry.is_symlink():
                mode = entry.stat().st_mode & 0o777
                entry.chmod(0o600)  # root reads anything
                try:
                    shutil.copyfile(entry, dest / entry.name)
                finally:
                    entry.chmod(mode)
                (dest / entry.name).chmod(0o600)

    async def call(command: str, **params):
        assert command == "copy"
        calls.append((params["source"], params["dest"]))
        # Off the event loop, as the real helper's copy happens in its process
        await asyncio.to_thread(copy, Path(params["source"]), Path(params["dest"]))
        return {}

    return call


def _new_file(data: Path) -> None:
    (data / "0_new.data").write_bytes(b"new")
    _hide(data / "0_new.data")


def _native(data: Path) -> MagicMock:
    loop = asyncio.get_running_loop()
    fake = MagicMock()
    fake.app.path_data = data
    fake.app.slug = "core_openthread_border_router"
    fake.sys_run_in_executor = lambda func, *args: loop.run_in_executor(
        None, func, *args
    )
    return fake


@pytest.fixture
def data(tmp_path: Path):
    """Return an app data folder; restore permissions so tmp_path cleans up."""
    folder = tmp_path / "data"
    folder.mkdir()
    yield folder
    for path in folder.rglob("*"):
        if not path.is_symlink():
            path.chmod(0o700 if path.is_dir() else 0o600)


async def test_readable_data_is_backed_up_in_place(data: Path, tmp_path: Path):
    """Test an app whose files the Supervisor can read needs no copy."""
    (data / "options.json").write_text("{}")
    with patch.object(helper, "call", side_effect=AssertionError("no helper")):
        source = await NativeApp.backup_source(_native(data), tmp_path / "scratch")
    assert source == data


async def test_root_only_files_are_copied_readable(data: Path, tmp_path: Path):
    """Test root's files reach the backup through readable copies."""
    (data / "options.json").write_text('{"device": "/dev/cu.usbserial"}')
    (data / "0_983268fffe1bfb8e.data").write_bytes(b"thread network keys")
    _hide(data / "0_983268fffe1bfb8e.data")
    (data / "sub").mkdir()
    (data / "sub" / "deep.data").write_bytes(b"deeper")
    _hide(data / "sub" / "deep.data")
    (data / "link").symlink_to("options.json")
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    calls: list[tuple[str, str]] = []
    with patch.object(helper, "call", side_effect=_root_copy(calls)):
        source = await NativeApp.backup_source(_native(data), scratch)

    assert source == scratch / "data"
    assert (source / "0_983268fffe1bfb8e.data").read_bytes() == b"thread network keys"
    assert (source / "sub" / "deep.data").read_bytes() == b"deeper"
    assert (source / "options.json").read_text() == '{"device": "/dev/cu.usbserial"}'
    assert (source / "link").readlink() == Path("options.json")
    assert (source / "0_983268fffe1bfb8e.data").stat().st_uid == os.getuid()
    # One copy per folder holding root's files, nothing else
    assert sorted(calls) == [
        (str(data), str(source)),
        (str(data / "sub"), str(source / "sub")),
    ]


async def test_a_file_gone_before_the_copy_is_dropped(data: Path, tmp_path: Path):
    """Test a temp file renamed away mid-backup leaves no empty file behind."""
    (data / "0_983268fffe1bfb8e.data").write_bytes(b"keys")
    (data / "0_0-tmp.data").write_bytes(b"swap")
    _hide(data / "0_983268fffe1bfb8e.data")
    _hide(data / "0_0-tmp.data")
    scratch = tmp_path / "scratch"
    scratch.mkdir()

    def rename_away(_: int) -> None:
        (data / "0_0-tmp.data").unlink()

    with patch.object(
        helper, "call", side_effect=_root_copy([], before_copy=rename_away)
    ):
        source = await NativeApp.backup_source(_native(data), scratch)

    assert sorted(p.name for p in source.iterdir()) == ["0_983268fffe1bfb8e.data"]


async def test_a_file_new_during_the_copy_gets_a_second_pass(
    data: Path, tmp_path: Path
):
    """Test a file root creates mid-backup is copied readable on the retry."""
    (data / "0_983268fffe1bfb8e.data").write_bytes(b"keys")
    _hide(data / "0_983268fffe1bfb8e.data")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    real_copy = _root_copy([])
    passes = []

    async def call(command: str, **params):
        passes.append(params["source"])
        if len(passes) == 1:
            # A new file appears; root's copy creates it root-owned
            await asyncio.to_thread(_new_file, data)
            await real_copy(command, **params)
            await asyncio.to_thread((Path(params["dest"]) / "0_new.data").chmod, 0)
            return {}
        return await real_copy(command, **params)

    with patch.object(helper, "call", side_effect=call):
        source = await NativeApp.backup_source(_native(data), scratch)

    assert len(passes) == 2
    assert (source / "0_new.data").read_bytes() == b"new"


async def test_without_the_root_helper_the_backup_fails_clearly(
    data: Path, tmp_path: Path
):
    """Test a missing helper is an error naming the app, not a silent gap."""
    (data / "0_983268fffe1bfb8e.data").write_bytes(b"keys")
    _hide(data / "0_983268fffe1bfb8e.data")
    scratch = tmp_path / "scratch"
    scratch.mkdir()
    with (
        patch.object(helper, "call", side_effect=helper.HelperError("not running")),
        pytest.raises(DockerError, match="core_openthread_border_router"),
    ):
        await NativeApp.backup_source(_native(data), scratch)


async def test_an_unreadable_folder_fails_clearly(data: Path, tmp_path: Path):
    """Test a folder root locked is an error, not a backup missing it."""
    (data / "private").mkdir()
    (data / "private").chmod(0)
    with pytest.raises(DockerError, match="Can't list"):
        await NativeApp.backup_source(_native(data), tmp_path / "scratch")


def test_unreadable_files_skips_links_and_readable_files(data: Path):
    """Test only real files this user can't read count."""
    (data / "ok").write_text("x")
    (data / "dangling").symlink_to("missing")
    assert native_app._unreadable_files(data) == set()
    (data / "locked").write_text("x")
    _hide(data / "locked")
    assert native_app._unreadable_files(data) == {Path()}
