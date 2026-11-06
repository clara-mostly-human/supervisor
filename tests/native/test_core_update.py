"""Test native Core updates and rollbacks.

These run without the coresys fixture, which needs a D-Bus daemon.
"""

import asyncio
from functools import partial
import json
import logging
import os
from pathlib import Path
import stat
from unittest.mock import AsyncMock, MagicMock, patch

from awesomeversion import AwesomeVersion
import pytest

from supervisor.exceptions import DockerError
from supervisor.native import homeassistant as native_core
from supervisor.native.homeassistant import (
    ROLLBACK_SUFFIX,
    SNAPSHOT_INFO,
    STORAGE_ROLLED_BACK,
    STORAGE_SNAPSHOT,
    NativeHomeAssistant,
    StorageRestore,
    restore_storage,
    snapshot_storage,
)

OLD = AwesomeVersion("2026.10.2")
NEW = AwesomeVersion("2026.11.0")


def store(
    config: Path,
    key: str,
    version: int,
    data: object,
    *,
    minor: int = 1,
    mode: int = 0o644,
) -> None:
    """Write a .storage file the way Core's Store does."""
    path = config / ".storage" / key
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"version": version, "minor_version": minor, "key": key, "data": data}
        )
    )
    path.chmod(mode)


def entries(*items: tuple[str, str, int, str]) -> dict:
    """Return core.config_entries data for (entry_id, domain, version, token)."""
    return {
        "entries": [
            {"entry_id": i, "domain": d, "version": v, "data": {"token": t}}
            for i, d, v, t in items
        ]
    }


def read(config: Path, key: str) -> dict:
    """Return a .storage file's document."""
    return json.loads((config / ".storage" / key).read_text())


def contents(folder: Path) -> dict[str, bytes]:
    """Return {relative name: content} of every file in a folder."""
    return {
        str(p.relative_to(folder)): p.read_bytes()
        for p in sorted(folder.rglob("*"))
        if p.is_file()
    }


@pytest.fixture
def dirs(tmp_path: Path) -> tuple[Path, Path]:
    """Return (core, config) with .storage as 2026.10.2 leaves it."""
    core, config = tmp_path / "core", tmp_path / "config"
    core.mkdir()
    store(config, "http", 2, {"server_port": 8123})
    store(config, "auth", 1, {"users": ["old"]}, mode=0o600)
    store(config, "core.restore_state", 1, ["old"])
    store(config, "core.uuid", 1, {"uuid": "abc"})
    store(config, "roborock/map", 1, "old map")
    (config / ".storage" / "local_todo.list.ics").write_text("BEGIN:VCALENDAR old")
    store(
        config,
        "core.config_entries",
        1,
        entries(("e1", "met", 1, "met-old"), ("e2", "netatmo", 1, "oauth-old")),
    )
    return core, config


def migrate(config: Path) -> None:
    """Change .storage as a first start of 2026.11.0 does."""
    store(config, "http", 3, {"server_port": 8123})
    store(config, "core.restore_state", 1, ["new"])
    (config / ".storage" / "core.uuid").unlink()
    store(config, "only_in_new", 1, {})
    store(
        config,
        "core.config_entries",
        1,
        entries(("e1", "met", 2, "met-new"), ("e2", "netatmo", 1, "oauth-rotated")),
    )


def write_readable_changes(config: Path) -> None:
    """Change .storage only in ways 2026.10.2 can still read."""
    store(config, "http", 2, {"server_port": 8123}, minor=3)
    store(config, "core.restore_state", 1, ["new"])
    store(config, "only_in_new", 1, {})
    store(config, "core.label_registry", 1, {})
    (config / ".storage" / "local_todo.list.ics").write_text("BEGIN:VCALENDAR new")
    store(
        config,
        "core.config_entries",
        1,
        entries(
            ("e1", "met", 1, "met-old"),
            ("e2", "netatmo", 1, "oauth-rotated"),
            ("e3", "brand_new", 3, "added"),
        ),
    )


def make_hass(core: Path, config: Path, version: AwesomeVersion | None) -> MagicMock:
    """Return a fake Core whose install steps are mocks, with real .storage handling."""
    loop = asyncio.get_running_loop()
    hass = MagicMock()
    hass._version = version
    hass.rolling_back = False
    hass._storage_snapshot = None
    hass.path_core = core
    hass.sys_config.path_homeassistant = config
    hass.sys_run_in_executor = lambda func, *args: loop.run_in_executor(
        None, func, *args
    )
    hass._running_components = AsyncMock(return_value=["http", "zha"])
    hass._prepare = AsyncMock()
    hass._carry_over = AsyncMock()
    hass._install_integration_requirements = AsyncMock()
    hass.stop = AsyncMock()
    for name in ("_roll_back", "_snapshot_storage", "_restore_storage"):
        setattr(hass, name, partial(getattr(NativeHomeAssistant, name), hass))
    return hass


def test_rollback_puts_storage_back_whole(dirs: tuple[Path, Path]):
    """Test the 2026.11 case: everything returns to what 2026.10.2 left."""
    core, config = dirs
    before = contents(config / ".storage")
    snapshot_storage(core, config, str(OLD), str(NEW))
    migrate(config)

    result = restore_storage(core, config, str(OLD), str(NEW))

    assert result == StorageRestore(
        put_back=["core.config_entries", "core.restore_state", "core.uuid", "http"],
        moved_aside=["only_in_new"],
    )
    assert contents(config / ".storage") == before
    kept = core / STORAGE_ROLLED_BACK
    assert json.loads((kept / "http").read_text())["version"] == 3
    assert (kept / "only_in_new").exists()
    assert sorted(p.name for p in config.iterdir()) == [".storage"]


def test_readable_changes_go_back_too(dirs: tuple[Path, Path]):
    """Test minor-version rewrites (2026.11 strips names) are put back as well."""
    core, config = dirs
    before = contents(config / ".storage")
    snapshot_storage(core, config, str(OLD), str(NEW))
    write_readable_changes(config)

    result = restore_storage(core, config, str(OLD), str(NEW))

    assert result.moved_aside == ["core.label_registry", "only_in_new"]
    assert contents(config / ".storage") == before


def test_unchanged_storage_changes_nothing(dirs: tuple[Path, Path]):
    """Test a new Core that wrote nothing leaves nothing to do."""
    core, config = dirs
    snapshot_storage(core, config, str(OLD), str(NEW))

    assert restore_storage(core, config, str(OLD), str(NEW)) == StorageRestore([], [])
    assert not (core / STORAGE_ROLLED_BACK).exists()


def test_nested_folders_removed_or_created(dirs: tuple[Path, Path]):
    """Test a folder the new Core deleted comes back, and one it made goes aside."""
    core, config = dirs
    before = contents(config / ".storage")
    snapshot_storage(core, config, str(OLD), str(NEW))
    (config / ".storage" / "roborock" / "map").unlink()
    (config / ".storage" / "roborock").rmdir()
    store(config, "marketplace/repositories", 1, {})

    result = restore_storage(core, config, str(OLD), str(NEW))

    assert result == StorageRestore(["roborock/map"], ["marketplace/repositories"])
    assert contents(config / ".storage") == before
    assert not (config / ".storage" / "marketplace").exists()
    assert (core / STORAGE_ROLLED_BACK / "marketplace" / "repositories").exists()


def test_a_store_renamed_only_in_case_comes_back(dirs: tuple[Path, Path]):
    """Test a case-only rename on a case-insensitive disk doesn't lose the store."""
    core, config = dirs
    store(config, "Fan.Speed", 1, "old")
    snapshot_storage(core, config, str(OLD), str(NEW))
    (config / ".storage" / "Fan.Speed").rename(config / ".storage" / "fan.speed")
    store(config, "fan.speed", 1, "new")

    restore_storage(core, config, str(OLD), str(NEW))

    names = {p.name for p in (config / ".storage").iterdir()}
    assert "Fan.Speed" in names
    assert read(config, "Fan.Speed")["data"] == "old"


def test_failure_while_preparing_leaves_no_new_folders(dirs: tuple[Path, Path]):
    """Test a folder made for a file that never got put back is removed again."""
    core, config = dirs
    snapshot_storage(core, config, str(OLD), str(NEW))
    (config / ".storage" / "roborock" / "map").unlink()
    (config / ".storage" / "roborock").rmdir()
    write = native_core._write_private

    def failing_staging(path: Path, content: bytes, mode: int) -> None:
        if path.name == f".map{ROLLBACK_SUFFIX}":
            raise OSError("disk failed")
        write(path, content, mode)

    with (
        patch.object(native_core, "_write_private", failing_staging),
        pytest.raises(OSError, match="disk failed"),
    ):
        restore_storage(core, config, str(OLD), str(NEW))

    assert not (config / ".storage" / "roborock").exists()


def test_permissions_survive_and_flags_dont(dirs: tuple[Path, Path]):
    """Test owner-only stores stay owner-only, and folder flags aren't copied."""
    core, config = dirs
    os.chflags(config / ".storage" / "roborock", stat.UF_NODUMP)
    snapshot_storage(core, config, str(OLD), str(NEW))
    saved = core / STORAGE_SNAPSHOT / ".storage"
    assert (saved / "auth").stat().st_mode & 0o777 == 0o600
    assert not (saved / "roborock").stat().st_flags & stat.UF_NODUMP
    store(config, "auth", 2, {"users": ["new"]}, mode=0o600)

    restore_storage(core, config, str(OLD), str(NEW))

    assert (config / ".storage" / "auth").stat().st_mode & 0o777 == 0o600
    assert (core / STORAGE_ROLLED_BACK / "auth").stat().st_mode & 0o777 == 0o600


def test_a_symlink_core_wrote_through_is_not_moved_aside(
    dirs: tuple[Path, Path], tmp_path: Path
):
    """Test a store that was a symlink and became a file isn't treated as new."""
    core, config = dirs
    target = tmp_path / "linked_store"
    target.write_text('{"version": 1, "data": "old"}')
    (config / ".storage" / "linked").symlink_to(target)
    snapshot_storage(core, config, str(OLD), str(NEW))
    (config / ".storage" / "linked").unlink()
    (config / ".storage" / "linked").write_text('{"version": 1, "data": "new"}')

    result = restore_storage(core, config, str(OLD), str(NEW))

    assert "linked" not in result.moved_aside
    assert (config / ".storage" / "linked").exists()


def test_restore_writes_through_a_symlinked_storage(
    dirs: tuple[Path, Path], tmp_path: Path
):
    """Test a .storage that is a symlink stays one."""
    core, config = dirs
    real = tmp_path / "elsewhere"
    (config / ".storage").rename(real)
    (config / ".storage").symlink_to(real)
    snapshot_storage(core, config, str(OLD), str(NEW))
    migrate(config)

    restore_storage(core, config, str(OLD), str(NEW))

    assert (config / ".storage").is_symlink()
    assert json.loads((real / "http").read_text())["version"] == 2


def test_leftover_staging_files_never_count(dirs: tuple[Path, Path]):
    """Test a staged file left by a crash is neither kept nor put back."""
    core, config = dirs
    (config / ".storage" / f".http{ROLLBACK_SUFFIX}").write_text("half")
    snapshot_storage(core, config, str(OLD), str(NEW))
    assert not list((core / STORAGE_SNAPSHOT / ".storage").glob(f".*{ROLLBACK_SUFFIX}"))
    (config / ".storage" / f".http{ROLLBACK_SUFFIX}").write_text("half again")

    assert restore_storage(core, config, str(OLD), str(NEW)) == StorageRestore([], [])
    assert not list((config / ".storage").glob(f".*{ROLLBACK_SUFFIX}"))


@pytest.mark.parametrize(
    "info",
    [
        pytest.param('{"from": "2026.9.4", "to": "2026.11.0"}', id="other-from"),
        pytest.param('{"from": "2026.10.2", "to": "2026.11.1"}', id="other-to"),
        pytest.param("not json", id="corrupt"),
        pytest.param('["2026.10.2", "2026.11.0"]', id="not-an-object"),
    ],
)
def test_restore_only_from_a_snapshot_of_that_update(
    dirs: tuple[Path, Path], info: str
):
    """Test nothing changes unless the snapshot is of this very update."""
    core, config = dirs
    snapshot_storage(core, config, str(OLD), str(NEW))
    (core / STORAGE_SNAPSHOT / SNAPSHOT_INFO).write_text(info)
    migrate(config)

    assert restore_storage(core, config, str(OLD), str(NEW)) is None
    assert read(config, "http")["version"] == 3


def test_restore_without_any_snapshot_changes_nothing(dirs: tuple[Path, Path]):
    """Test a rollback after an update that kept no snapshot."""
    core, config = dirs
    migrate(config)

    assert restore_storage(core, config, str(OLD), str(NEW)) is None
    assert read(config, "http")["version"] == 3


def test_no_snapshot_of_a_missing_storage(dirs: tuple[Path, Path]):
    """Test an empty snapshot can never make a restore move everything aside."""
    core, config = dirs
    (config / ".storage").rename(config / "elsewhere")

    with pytest.raises(FileNotFoundError):
        snapshot_storage(core, config, str(OLD), str(NEW))

    assert list(core.iterdir()) == []


def test_snapshot_skips_files_that_vanish(dirs: tuple[Path, Path]):
    """Test a temp file Core renames away mid-copy doesn't fail the snapshot."""
    core, config = dirs
    (config / ".storage" / "tmpq1w2e3").write_text("{}")
    write = native_core._write_private

    def vanishing(path: Path, content: bytes, mode: int) -> None:
        if path.name.startswith("tmp"):
            raise FileNotFoundError(path)
        write(path, content, mode)

    with patch.object(native_core, "_write_private", vanishing):
        snapshot_storage(core, config, str(OLD), str(NEW))

    copied = core / STORAGE_SNAPSHOT / ".storage"
    assert not (copied / "tmpq1w2e3").exists()
    assert (copied / "http").exists()


def test_failed_snapshot_leaves_no_snapshot(dirs: tuple[Path, Path]):
    """Test an older snapshot can't survive a failed one and be restored later."""
    core, config = dirs
    snapshot_storage(core, config, str(OLD), str(NEW))

    with (
        patch.object(native_core, "_copy_tree", side_effect=OSError("full")),
        pytest.raises(OSError, match="full"),
    ):
        snapshot_storage(core, config, str(OLD), str(NEW))

    assert list(core.iterdir()) == []


def test_failure_while_preparing_changes_nothing(dirs: tuple[Path, Path]):
    """Test a failed staged write leaves .storage exactly as the new Core left it."""
    core, config = dirs
    snapshot_storage(core, config, str(OLD), str(NEW))
    migrate(config)
    after = contents(config / ".storage")
    write = native_core._write_private

    def failing_staging(path: Path, content: bytes, mode: int) -> None:
        if path.name == f".http{ROLLBACK_SUFFIX}":
            path.write_bytes(content[:3])
            raise OSError("disk failed")
        write(path, content, mode)

    with (
        patch.object(native_core, "_write_private", failing_staging),
        pytest.raises(OSError, match="disk failed"),
    ):
        restore_storage(core, config, str(OLD), str(NEW))

    assert contents(config / ".storage") == after


def test_a_store_that_became_a_folder_changes_nothing(dirs: tuple[Path, Path]):
    """Test a file the new Core turned into a folder stops the restore up front."""
    core, config = dirs
    snapshot_storage(core, config, str(OLD), str(NEW))
    migrate(config)
    (config / ".storage" / "core.restore_state").unlink()
    store(config, "core.restore_state/part", 1, [])
    after = contents(config / ".storage")

    with pytest.raises(IsADirectoryError):
        restore_storage(core, config, str(OLD), str(NEW))

    assert contents(config / ".storage") == after


@pytest.mark.parametrize("failing", [1, 3, 4])
def test_failure_while_renaming_is_undone(dirs: tuple[Path, Path], failing: int):
    """Test a rename failing half-way leaves .storage all new, not a mix.

    The fourth rename comes after core.uuid, which the new Core removed, is back.
    """
    core, config = dirs
    snapshot_storage(core, config, str(OLD), str(NEW))
    migrate(config)
    after = contents(config / ".storage")
    replace = Path.replace
    calls = []

    def nth_fails(self: Path, target: Path) -> Path:
        calls.append(self)
        if len(calls) == failing:
            raise OSError("disk failed")
        return replace(self, target)

    with (
        patch.object(Path, "replace", nth_fails),
        pytest.raises(OSError, match="disk failed"),
    ):
        restore_storage(core, config, str(OLD), str(NEW))

    assert contents(config / ".storage") == after


async def test_update_records_the_new_version_once_the_old_one_stopped(
    dirs: tuple[Path, Path],
):
    """Test the version switches after stop, and .storage is kept after that."""
    core, config = dirs
    hass = make_hass(core, config, OLD)
    loop = asyncio.get_running_loop()
    seen = {}

    async def requirements(*_: object) -> None:
        seen["requirements"] = hass._version

    async def stop() -> None:
        seen["stop"] = hass._version
        seen["snapshot"] = await loop.run_in_executor(
            None, (core / STORAGE_SNAPSHOT).exists
        )

    hass._install_integration_requirements.side_effect = requirements
    hass.stop.side_effect = stop

    await NativeHomeAssistant.update(hass, NEW)

    assert seen == {"requirements": OLD, "stop": OLD, "snapshot": False}
    assert hass._version == NEW
    assert hass._storage_snapshot == (str(OLD), str(NEW))
    info = json.loads((core / STORAGE_SNAPSHOT / SNAPSHOT_INFO).read_text())
    assert (info["from"], info["to"]) == (str(OLD), str(NEW))


@pytest.mark.parametrize(
    "step", ["_prepare", "_carry_over", "_install_integration_requirements", "stop"]
)
async def test_failed_update_step_keeps_the_running_version(
    dirs: tuple[Path, Path], step: str
):
    """Test the Supervisor never points at a version that isn't running."""
    core, config = dirs
    hass = make_hass(core, config, OLD)
    getattr(hass, step).side_effect = DockerError("no network")

    with pytest.raises(DockerError):
        await NativeHomeAssistant.update(hass, NEW)

    assert hass._version == OLD
    assert not (core / STORAGE_SNAPSHOT).exists()


@pytest.mark.parametrize(
    ("previous", "target"),
    [
        pytest.param(None, NEW, id="first"),
        pytest.param(NEW, NEW, id="same"),
        pytest.param(NEW, OLD, id="downgrade"),
    ],
)
async def test_snapshot_only_on_an_upgrade(
    dirs: tuple[Path, Path],
    previous: AwesomeVersion | None,
    target: AwesomeVersion,
):
    """Test only an upgrade replaces the snapshot of the last upgrade."""
    core, config = dirs
    await asyncio.get_running_loop().run_in_executor(
        None, snapshot_storage, core, config, str(OLD), str(NEW)
    )
    hass = make_hass(core, config, previous)

    await NativeHomeAssistant.update(hass, target)

    info = json.loads((core / STORAGE_SNAPSHOT / SNAPSHOT_INFO).read_text())
    assert (info["from"], info["to"]) == (str(OLD), str(NEW))
    assert hass._storage_snapshot is None


async def test_rollback_reuses_the_old_install_and_puts_back_storage_after_stop(
    dirs: tuple[Path, Path],
):
    """Test a rollback needs no network and starts on what the old Core left."""
    core, config = dirs
    hass = make_hass(core, config, OLD)
    await NativeHomeAssistant.update(hass, NEW)
    migrate(config)
    for step in (
        hass._running_components,
        hass._carry_over,
        hass._install_integration_requirements,
        hass.stop,
    ):
        step.reset_mock()
    loop = asyncio.get_running_loop()
    at_stop = {}

    async def stop() -> None:
        at_stop["http"] = (await loop.run_in_executor(None, read, config, "http"))[
            "version"
        ]
        at_stop["version"] = hass._version

    hass.stop.side_effect = stop

    hass.rolling_back = True
    await NativeHomeAssistant.update(hass, OLD)

    assert at_stop == {"http": 3, "version": NEW}
    hass._running_components.assert_not_awaited()
    hass._carry_over.assert_not_awaited()
    hass._install_integration_requirements.assert_not_awaited()
    hass._prepare.assert_awaited_with(OLD)
    assert hass._version == OLD
    assert read(config, "http")["version"] == 2
    assert hass._storage_snapshot is None


async def test_rollback_restores_only_a_snapshot_this_update_took(
    dirs: tuple[Path, Path], caplog: pytest.LogCaptureFixture
):
    """Test a matching snapshot left on disk by an earlier update isn't used."""
    core, config = dirs
    await asyncio.get_running_loop().run_in_executor(
        None, snapshot_storage, core, config, str(OLD), str(NEW)
    )
    migrate(config)
    hass = make_hass(core, config, NEW)
    hass.rolling_back = True

    await NativeHomeAssistant.update(hass, OLD)

    assert read(config, "http")["version"] == 3
    assert "No .storage snapshot of the update from Core 2026.10.2" in caplog.text


@pytest.mark.parametrize(
    "error", [pytest.param(PermissionError("denied")), pytest.param(TypeError("odd"))]
)
async def test_snapshot_failure_never_fails_the_update(
    dirs: tuple[Path, Path], caplog: pytest.LogCaptureFixture, error: Exception
):
    """Test the update goes on without a snapshot, and says so."""
    core, config = dirs
    hass = make_hass(core, config, OLD)
    # Left by an earlier update of the same pair; must not count for this one.
    hass._storage_snapshot = (str(OLD), str(NEW))

    with patch.object(native_core, "snapshot_storage", side_effect=error):
        await NativeHomeAssistant.update(hass, NEW)

    assert hass._version == NEW
    assert hass._storage_snapshot is None
    assert "Can't keep the .storage of Core 2026.10.2" in caplog.text


@pytest.mark.parametrize(
    ("restore", "message", "level"),
    [
        pytest.param(
            {"side_effect": OSError("full")},
            "Can't put back the .storage of Core 2026.10.2",
            logging.ERROR,
            id="restore-fails",
        ),
        pytest.param(
            {"side_effect": TypeError("odd")},
            "Can't put back the .storage of Core 2026.10.2",
            logging.ERROR,
            id="restore-raises-anything",
        ),
        pytest.param(
            {"return_value": None},
            "No .storage snapshot of the update from Core 2026.10.2 to 2026.11.0",
            logging.WARNING,
            id="snapshot-gone",
        ),
        pytest.param(
            {"return_value": StorageRestore([], [])},
            "Core 2026.11.0 left .storage as Core 2026.10.2 had it",
            logging.INFO,
            id="unchanged",
        ),
        pytest.param(
            {"return_value": StorageRestore(["http"], ["only_in_new"])},
            "Put .storage back as Core 2026.10.2 left it: 1 files put back, 1 files",
            logging.WARNING,
            id="put-back",
        ),
    ],
)
async def test_storage_never_stops_a_rollback(
    dirs: tuple[Path, Path],
    caplog: pytest.LogCaptureFixture,
    restore: dict,
    message: str,
    level: int,
):
    """Test the old version still gets switched to, with the outcome logged."""
    caplog.set_level(logging.INFO)
    core, config = dirs
    hass = make_hass(core, config, NEW)
    hass._storage_snapshot = (str(OLD), str(NEW))
    hass.rolling_back = True

    with patch.object(native_core, "restore_storage", **restore):
        await NativeHomeAssistant.update(hass, OLD)

    assert hass._version == OLD
    hass.stop.assert_awaited_once()
    assert [r.levelno for r in caplog.records if message in r.getMessage()] == [level]


async def test_install_still_records_the_version_at_once(dirs: tuple[Path, Path]):
    """Test first install and repair keep their behavior."""
    core, config = dirs
    hass = make_hass(core, config, None)

    await NativeHomeAssistant.install(hass, NEW)

    hass._prepare.assert_awaited_once_with(NEW)
    assert hass._version == NEW
