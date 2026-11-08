"""Test backing up and restoring a Core config folder that is a symlink.

On this Mac the Supervisor's homeassistant folder links to the config folder
Core used before the Supervisor ran it. A backup added that folder as a link
(`data` -> the live config path) ahead of its files, so a restore refused
every file as outside the destination: no Core backup could be restored.
"""

import asyncio
import io
import json
from pathlib import Path
import tarfile
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from securetar import SecureTarArchive, SecureTarFile

from supervisor.exceptions import BackupInvalidError, HomeAssistantError
from supervisor.homeassistant.module import HomeAssistant

META = {"version": "2026.11.0", "port": 8126, "watchdog": True}


def _core(path_homeassistant: Path, tmp: Path) -> MagicMock:
    loop = asyncio.get_running_loop()
    fake = MagicMock()
    fake.sys_config.path_homeassistant = path_homeassistant
    fake.sys_config.path_tmp = tmp
    fake.sys_config.path_supervisor = tmp.parent
    fake.sys_run_in_executor = lambda func, *args: loop.run_in_executor(
        None, func, *args
    )
    fake.begin_backup = AsyncMock()
    fake.end_backup = AsyncMock()
    fake._data = dict(META)
    return fake


@pytest.fixture
def layout(tmp_path: Path) -> dict[str, Path]:
    """Return a config folder reached through a link, like production's."""
    real = tmp_path / "homeassistant-config"
    (real / ".storage").mkdir(parents=True)
    (real / "configuration.yaml").write_text("default_config:\n")
    (real / ".storage" / "core.config").write_text('{"data": {}}')
    (real / "custom_components").mkdir()
    elsewhere = tmp_path / "integration-checkout"
    elsewhere.mkdir()
    (elsewhere / "__init__.py").write_text("# not the config's to delete\n")
    (real / "custom_components" / "dev_link").symlink_to(elsewhere)
    supervisor_data = tmp_path / "supervisor-data"
    supervisor_data.mkdir()
    (supervisor_data / "homeassistant").symlink_to(real)
    tmp = supervisor_data / "tmp"
    tmp.mkdir()
    return {
        "real": real,
        "linked": supervisor_data / "homeassistant",
        "elsewhere": elsewhere,
        "tmp": tmp,
    }


def _core_part(archive: Path) -> bytes:
    """Return the Core part of a backup written by SecureTarArchive."""
    with tarfile.open(archive) as outer:
        (name,) = [n for n in outer.getnames() if n.endswith("homeassistant.tar.gz")]
        return outer.extractfile(name).read()


def _inner_members(archive: Path) -> dict[str, tarfile.TarInfo]:
    with tarfile.open(fileobj=io.BytesIO(_core_part(archive)), mode="r:gz") as inner:
        return {member.name: member for member in inner.getmembers()}


def _old_backup(path: Path, victim: Path) -> Path:
    """Write a Core part like the ones taken before the fix: data is a link."""
    inner = path / "homeassistant.tar.gz"
    with tarfile.open(inner, "w:gz") as tar:

        def add(name: str, data: bytes | None = None, link: str | None = None):
            info = tarfile.TarInfo(name)
            if link is not None:
                info.type, info.linkname = tarfile.SYMTYPE, link
            elif data is None:
                info.type, info.mode = tarfile.DIRTYPE, 0o755
            else:
                info.size = len(data)
            tar.addfile(info, io.BytesIO(data) if data is not None else None)

        add(".")
        add("./homeassistant.json", json.dumps(META).encode())
        add("data", link=str(victim))
        add("data/configuration.yaml", b"restored: true\n")
        add("data/.storage")
        add("data/.storage/core.config", b'{"data": {"restored": true}}')
    return inner


async def test_backup_adds_the_linked_folder_as_a_folder(
    layout: dict[str, Path], tmp_path: Path
) -> None:
    """Test the backup holds data/ as a folder, with links inside kept as links."""
    archive = tmp_path / "backup.tar"
    with SecureTarArchive(archive, "w") as outer:
        inner = outer.create_tar("./homeassistant.tar.gz", gzip=True)
        await HomeAssistant.backup(_core(layout["linked"], layout["tmp"]), inner)

    members = _inner_members(archive)
    assert members["data"].isdir()
    assert members["data/configuration.yaml"].isfile()
    assert members["data/.storage/core.config"].isfile()
    link = members["data/custom_components/dev_link"]
    assert link.issym()
    assert link.linkname == str(layout["elsewhere"])


async def test_backup_then_restore_round_trip(
    layout: dict[str, Path], tmp_path: Path
) -> None:
    """Test a backup of the linked folder restores into a fresh install."""
    archive = tmp_path / "backup.tar"
    with SecureTarArchive(archive, "w") as outer:
        inner = outer.create_tar("./homeassistant.tar.gz", gzip=True)
        await HomeAssistant.backup(_core(layout["linked"], layout["tmp"]), inner)
    raw = _core_part(archive)

    fresh = tmp_path / "fresh" / "homeassistant"
    fresh.mkdir(parents=True)
    tmp = tmp_path / "fresh" / "tmp"
    tmp.mkdir()
    restore = HomeAssistant.restore.__wrapped__  # type: ignore[attr-defined]
    await restore(_core(fresh, tmp), SecureTarFile(fileobj=io.BytesIO(raw)))

    assert (fresh / "configuration.yaml").read_text() == "default_config:\n"
    assert (fresh / ".storage" / "core.config").read_text() == '{"data": {}}'
    assert (fresh / "custom_components" / "dev_link").readlink() == layout["elsewhere"]


async def test_restore_of_an_old_backup_skips_the_linked_root(
    tmp_path: Path,
) -> None:
    """Test a backup taken before the fix restores, and its link target is safe.

    The link points at the live config folder; a restore must neither follow
    it nor give up on the files after it.
    """
    victim = tmp_path / "live-config"
    victim.mkdir()
    (victim / "configuration.yaml").write_text("live\n")
    inner = _old_backup(tmp_path, victim)

    fresh = tmp_path / "fresh" / "homeassistant"
    fresh.mkdir(parents=True)
    (fresh / "stale.yaml").write_text("stale\n")
    tmp = tmp_path / "fresh" / "tmp"
    tmp.mkdir()
    core = _core(fresh, tmp)
    core._data = {}
    restore = HomeAssistant.restore.__wrapped__  # type: ignore[attr-defined]
    with inner.open("rb") as file:
        await restore(core, SecureTarFile(fileobj=file))

    assert not fresh.is_symlink()
    assert (fresh / "configuration.yaml").read_text() == "restored: true\n"
    assert json.loads((fresh / ".storage" / "core.config").read_text()) == {
        "data": {"restored": True}
    }
    assert not (fresh / "stale.yaml").exists()
    assert sorted(p.name for p in victim.iterdir()) == ["configuration.yaml"]
    assert (victim / "configuration.yaml").read_text() == "live\n"
    assert core._data["port"] == 8126


@pytest.mark.parametrize("exclude_database", [False, True])
async def test_restore_into_a_linked_folder_replaces_its_contents(
    layout: dict[str, Path], tmp_path: Path, exclude_database: bool
) -> None:
    """Test a restore over production's layout empties the real folder first.

    As on Linux; it used to leave files the backup doesn't have, since find
    doesn't follow the link it is given. The link itself stays.
    """
    (layout["real"] / "stale.yaml").write_text("stale\n")
    (layout["real"] / "home-assistant_v2.db").write_bytes(b"live database")
    inner = _old_backup(tmp_path, tmp_path / "nowhere")

    restore = HomeAssistant.restore.__wrapped__  # type: ignore[attr-defined]
    with inner.open("rb") as file:
        await restore(
            _core(layout["linked"], layout["tmp"]),
            SecureTarFile(fileobj=file),
            exclude_database,
        )

    assert layout["linked"].readlink() == layout["real"]
    real = layout["real"]
    assert (real / "configuration.yaml").read_text() == "restored: true\n"
    assert not (real / "stale.yaml").exists()
    assert not (real / "custom_components").exists()
    assert (real / "home-assistant_v2.db").exists() == exclude_database
    assert [p.name for p in layout["elsewhere"].iterdir()] == ["__init__.py"]


def _crafted(path: Path, members: list[tuple[str, bytes | None, str | None]]) -> Path:
    """Write a Core part from (name, data, link target) members."""
    inner = path / "crafted.tar.gz"
    with tarfile.open(inner, "w:gz") as tar:
        for name, data, link in members:
            info = tarfile.TarInfo(name)
            if link is not None:
                info.type, info.linkname = tarfile.SYMTYPE, link
            else:
                info.size = len(data or b"")
            tar.addfile(info, io.BytesIO(data or b"") if link is None else None)
    return inner


@pytest.mark.parametrize(
    "members",
    [
        pytest.param([("../../outside/passwd", b"x", None)], id="traversal"),
        pytest.param(
            [
                ("data/escape", None, "../../outside"),
                ("data/escape/passwd", b"x", None),
            ],
            id="link-inside-data",
        ),
        pytest.param(
            [("./data", None, "/"), ("data/../../outside/passwd", b"x", None)],
            id="root-link-then-traversal",
        ),
    ],
)
async def test_restore_still_refuses_crafted_backups(
    tmp_path: Path, members: list
) -> None:
    """Test only the root config link is skipped; every other escape still fails."""
    outside = tmp_path / "outside"
    outside.mkdir()
    fresh = tmp_path / "a" / "b" / "homeassistant"
    fresh.mkdir(parents=True)
    tmp = tmp_path / "a" / "b" / "tmp"
    tmp.mkdir()
    inner = _crafted(tmp_path, members)
    restore = HomeAssistant.restore.__wrapped__  # type: ignore[attr-defined]
    with inner.open("rb") as file, pytest.raises(BackupInvalidError):
        await restore(_core(fresh, tmp), SecureTarFile(fileobj=file))
    assert list(outside.iterdir()) == []


@pytest.mark.parametrize(
    "target",
    ["home", "supervisor-parent", "not-a-config", "dangling"],
)
async def test_restore_refuses_a_link_to_anything_but_a_config_folder(
    tmp_path: Path, target: str
) -> None:
    """Test a link the restore would empty must point at a Core config folder."""
    supervisor_data = tmp_path / "srv" / "supervisor-data"
    tmp = supervisor_data / "tmp"
    tmp.mkdir(parents=True)
    other = tmp_path / "projects"
    other.mkdir()
    (other / "keep.txt").write_text("keep\n")
    real = {
        "home": Path.home(),
        "supervisor-parent": tmp_path / "srv",
        "not-a-config": other,
        "dangling": tmp_path / "gone",
    }[target]
    (supervisor_data / "homeassistant").symlink_to(real)
    inner = _old_backup(tmp_path, tmp_path / "nowhere")

    restore = HomeAssistant.restore.__wrapped__  # type: ignore[attr-defined]
    with (
        patch("supervisor.homeassistant.module.remove_folder") as remove,
        inner.open("rb") as file,
        pytest.raises(HomeAssistantError, match="isn't a Core config folder"),
    ):
        await restore(
            _core(supervisor_data / "homeassistant", tmp),
            SecureTarFile(fileobj=file),
        )
    remove.assert_not_called()
    assert (other / "keep.txt").read_text() == "keep\n"


async def test_restore_into_a_link_to_an_empty_folder(tmp_path: Path) -> None:
    """Test a fresh, empty config folder behind a link is fine to restore into."""
    real = tmp_path / "new-config"
    real.mkdir()
    supervisor_data = tmp_path / "supervisor-data"
    (supervisor_data / "tmp").mkdir(parents=True)
    (supervisor_data / "homeassistant").symlink_to(real)
    inner = _old_backup(tmp_path, tmp_path / "nowhere")
    restore = HomeAssistant.restore.__wrapped__  # type: ignore[attr-defined]
    with inner.open("rb") as file:
        await restore(
            _core(supervisor_data / "homeassistant", supervisor_data / "tmp"),
            SecureTarFile(fileobj=file),
        )
    assert (real / "configuration.yaml").read_text() == "restored: true\n"


async def test_restore_stops_if_a_link_survives_the_delete(
    layout: dict[str, Path], tmp_path: Path
) -> None:
    """Test a link the delete left would never be copied through."""
    inner = _old_backup(tmp_path, tmp_path / "nowhere")
    restore = HomeAssistant.restore.__wrapped__  # type: ignore[attr-defined]
    with (
        patch("supervisor.homeassistant.module.remove_folder"),
        inner.open("rb") as file,
        pytest.raises(HomeAssistantError, match="is still there"),
    ):
        await restore(
            _core(layout["linked"], layout["tmp"]), SecureTarFile(fileobj=file)
        )
    assert [p.name for p in layout["elsewhere"].iterdir()] == ["__init__.py"]
    assert (layout["real"] / "configuration.yaml").read_text() == "default_config:\n"
