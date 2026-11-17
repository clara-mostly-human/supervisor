"""Run Home Assistant Core natively on macOS.

Each Core version is installed into its own virtual environment under
``<data>/core/versions/<version>``, and runs as a per-user launchd service.
Installing a new version never touches the running one, so a failed update
rolls back by starting the previous version again, like switching images.

Optional files under ``<data>/core`` shape every install:

* ``pinned-requirements.txt``: a complete pinned package set, installed
  without dependency resolution, e.g. to clone an existing installation.
* ``extra-requirements.txt``: packages installed next to Core, e.g. an
  editable development checkout of a library a custom integration uses.
* ``patches/*.patch``: unified diffs applied to site-packages after install,
  for fixes not yet released upstream. An install fails if a patch does not
  apply, so a patch never silently goes missing on update.

A newer Core migrates ``.storage`` on its first start, and the previous version
refuses the newer formats. So an update keeps the ``.storage`` the previous
version left in ``<data>/core/storage-snapshot``, and a rollback puts it back.
"""

from __future__ import annotations

import asyncio
from collections import deque
from contextlib import suppress
import hashlib
from ipaddress import IPv4Address
import json
import logging
import os
from pathlib import Path
import re
import shutil
import stat
import time
from typing import TYPE_CHECKING, Any, NamedTuple

from awesomeversion import AwesomeVersion, AwesomeVersionCompareException

from ..const import SOCKET_CORE, SOCKET_NAME_SUPERVISOR, BusEvent
from ..docker.const import (
    ENV_CORE_API_SOCKET,
    ENV_DUPLICATE_LOG_FILE,
    ENV_SUPERVISOR_SOCKET,
    ENV_TIME,
    ENV_TOKEN,
    ENV_TOKEN_OLD,
    ContainerState,
)
from ..docker.manager import CommandReturn
from ..docker.monitor import DockerContainerStateEvent
from ..docker.stats import DockerStats
from ..exceptions import (
    DockerContainerNotRunningError,
    DockerError,
    HomeAssistantAPIError,
)
from ..homeassistant.const import LANDINGPAGE
from ..jobs.job_group import JobGroup
from .docker import LOCALHOST
from .hardware import NATIVE_BY_ID
from .launchd import LaunchdError, service
from .stats import process_stats

if TYPE_CHECKING:
    from ..coresys import CoreSys

_LOGGER: logging.Logger = logging.getLogger(__name__)

HASS_NAME = "homeassistant"
ENV_RESTORE_JOB_ID = "SUPERVISOR_RESTORE_JOB_ID"
# Where the Supervisor keeps the /dev/serial/by-id links macOS has no udev for;
# Core's carried serial-by-id patch names serial ports by them, as on Linux
ENV_SERIAL_BY_ID_DIR = "SUPERVISOR_SERIAL_BY_ID_DIR"
STOP_TIMEOUT = 240
HUNG_STOP_TIMEOUT = 60
POLL_SECONDS = 5
LOG_TAIL_LINES = 1000
HANG_SAMPLES_KEPT = 10
_RE_REQ_NAME = re.compile(r"[\s\[=<>!~;@]")


def _python() -> str:
    """Return the Python used to create Core virtual environments."""
    return os.environ.get("SUPERVISOR_CORE_PYTHON", "/opt/homebrew/bin/python3.14")


def _launchd_prefix() -> str:
    return os.environ.get("SUPERVISOR_LAUNCHD_PREFIX", "ha-macos")


def api_address() -> str:
    """Return host:port of the Supervisor API as Core must reach it."""
    port = os.environ.get("SUPERVISOR_API_PORT", "80")
    return f"{LOCALHOST}:{port}" if port != "80" else str(LOCALHOST)


async def _run(
    *args: str, cwd: Path | None = None, env: dict[str, str] | None = None
) -> CommandReturn:
    """Run a command and collect its output."""
    proc = await asyncio.create_subprocess_exec(
        *args,
        cwd=cwd,
        env=env,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.STDOUT,
    )
    try:
        out, _ = await proc.communicate()
    except asyncio.CancelledError:
        proc.kill()
        raise
    return CommandReturn(
        proc.returncode or 0, out.decode(errors="replace").splitlines(True)
    )


class InstallError(Exception):
    """Installing a Core version failed."""


INSTALLED_MARKER = ".installed"


def version_path(path_core: Path, version: AwesomeVersion | str) -> Path:
    """Return the virtual environment of a Core version."""
    return path_core / "versions" / str(version)


def install_fingerprint(path_core: Path) -> str:
    """Return a hash of everything besides the version that shapes an install."""
    digest = hashlib.sha256()
    files = [
        path_core / "pinned-requirements.txt",
        path_core / "extra-requirements.txt",
        *sorted((path_core / "patches").glob("*.patch")),
    ]
    for file in files:
        if file.is_file():
            digest.update(file.name.encode())
            digest.update(file.read_bytes())
    return digest.hexdigest()


def is_installed(path_core: Path, version: AwesomeVersion | str) -> bool:
    """Return True if a Core version finished installing (it can run)."""
    return (version_path(path_core, version) / INSTALLED_MARKER).exists()


def is_current(path_core: Path, version: AwesomeVersion | str) -> bool:
    """Return True if a Core version is installed with the current patches.

    An install made with other patches or requirements is rebuilt before it
    is used for an update, instead of being reused.
    """
    marker = version_path(path_core, version) / INSTALLED_MARKER
    try:
        return marker.read_text().strip() == install_fingerprint(path_core)
    except FileNotFoundError:
        return False


async def install_core(path_core: Path, version: AwesomeVersion | str) -> None:
    """Install a Core version, its extra requirements and carried patches.

    The venv is built in place (a venv is not relocatable) and only counts as
    installed once the marker file is written, so a half-finished install is
    never used and is rebuilt on the next attempt.
    """
    venv = version_path(path_core, version)
    loop = asyncio.get_running_loop()
    if venv.exists():
        await loop.run_in_executor(None, shutil.rmtree, venv)

    _LOGGER.info("Installing Home Assistant Core %s into %s", version, venv)
    pip = str(venv / "bin" / "pip")
    steps: list[tuple[str, ...]] = [
        (_python(), "-m", "venv", str(venv)),
        (pip, "install", "--upgrade", "pip"),
        (pip, "install", f"homeassistant=={version}"),
    ]
    pinned = path_core / "pinned-requirements.txt"
    if pinned.exists():
        # A complete pinned set (e.g. `pip freeze` of an existing install),
        # installed as is to reproduce that environment exactly; Core itself
        # installs integration requirements without resolving conflicts.
        steps.append((pip, "install", "--no-deps", "-r", str(pinned)))
    extra = path_core / "extra-requirements.txt"
    if extra.exists():
        steps.append((pip, "install", "-r", str(extra)))

    for step in steps:
        result = await _run(*step)
        if result.exit_code != 0:
            _LOGGER.error(
                "Installing Core %s failed at %s:\n%s",
                version,
                " ".join(step[:4]),
                "".join(result.log[-40:]),
            )
            raise InstallError(f"Can't install Home Assistant Core {version}")

    patches = path_core / "patches"
    if patches.is_dir():
        site = next((venv / "lib").glob("python3*/site-packages"))
        for patch in sorted(patches.glob("*.patch")):
            result = await _run(
                "/usr/bin/patch",
                "-p1",
                "--forward",
                "--batch",
                "-i",
                str(patch),
                cwd=site,
            )
            if result.exit_code != 0:
                _LOGGER.error(
                    "Patch %s does not apply to Core %s:\n%s",
                    patch.name,
                    version,
                    "".join(result.log),
                )
                raise InstallError(f"Patch {patch.name} does not apply to {version}")
            _LOGGER.info("Applied %s to Core %s", patch.name, version)

    (venv / INSTALLED_MARKER).write_text(install_fingerprint(path_core))
    _LOGGER.info("Home Assistant Core %s installed", version)


def _site_packages(venv: Path) -> Path:
    return next((venv / "lib").glob("python3*/site-packages"))


def integration_requirements(
    venv: Path, config: Path, components: list[str]
) -> list[str]:
    """Return the requirements of the given loaded components.

    Components are Core's loaded list ("zha", "sensor.aranet"); manifests come
    from the new install and from custom_components in the config.
    """
    site = _site_packages(venv)
    domains = {part for component in components for part in component.split(".")}
    requirements: set[str] = set()
    for domain in domains:
        for manifest in (
            site / "homeassistant" / "components" / domain / "manifest.json",
            config / "custom_components" / domain / "manifest.json",
        ):
            if manifest.is_file():
                requirements.update(
                    json.loads(manifest.read_text()).get("requirements", [])
                )
    return sorted(requirements)


def _normalize(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).lower()


async def _editable(venv: Path) -> set[str]:
    """Return the normalized names of the editable installs in a venv."""
    result = await _run(
        str(venv / "bin" / "python"),
        "-m",
        "pip",
        "list",
        "--editable",
        "--format",
        "json",
    )
    try:
        return {_normalize(p["name"]) for p in json.loads("".join(result.log))}
    except ValueError, KeyError, TypeError:
        return set()


async def install_requirements(venv: Path, requirements: list[str]) -> CommandReturn:
    """Install requirements into a Core venv the way Core itself does (uv).

    Each requirement is reinstalled even if present: a carried-over package
    can overwrite another's files (old pyipp ships the module aioipp
    provides), and the integration's own requirement must win.
    """
    constraints = _site_packages(venv) / "homeassistant" / "package_constraints.txt"
    # Editable installs (a development checkout, see extra-requirements.txt)
    # stay as they are.
    editable = await _editable(venv)
    reinstall = [
        arg
        for requirement in requirements
        if _normalize(name := _RE_REQ_NAME.split(requirement, 1)[0]) not in editable
        for arg in ("--reinstall-package", name)
    ]
    return await _run(
        str(venv / "bin" / "python"),
        "-m",
        "uv",
        "pip",
        "install",
        "--quiet",
        "--index-strategy",
        "unsafe-first-match",
        "--constraint",
        str(constraints),
        *reinstall,
        *requirements,
        env={**os.environ, "VIRTUAL_ENV": str(venv)},
    )


async def _freeze(venv: Path) -> dict[str, str]:
    """Return {normalized name: freeze line} of a venv, editable installs excluded."""
    result = await _run(str(venv / "bin" / "python"), "-m", "pip", "freeze", "--all")
    packages = {}
    for line in result.log:
        line = line.strip()
        if not line or line.startswith(("-e ", "#")) or "==" not in line:
            continue
        name = line.split("==", 1)[0].lower().replace("_", "-").replace(".", "-")
        packages[name] = line
    return packages


async def carry_over_packages(old_venv: Path, new_venv: Path) -> CommandReturn | None:
    """Install the packages the old install had beyond the new one's, as pinned.

    Core installs integration requirements on demand (discovery, MFA, ...);
    a new install would otherwise only get them on first use, and some are
    needed before Core can start. They go in --no-deps at the old versions;
    the loaded integrations' requirements are resolved afterwards.
    """
    old, new = await asyncio.gather(_freeze(old_venv), _freeze(new_venv))
    missing = [line for name, line in old.items() if name not in new]
    if not missing:
        return None
    _LOGGER.info("Carrying %d packages over from %s", len(missing), old_venv.name)
    return await _run(
        str(new_venv / "bin" / "python"),
        "-m",
        "uv",
        "pip",
        "install",
        "--quiet",
        "--no-deps",
        *missing,
        env={**os.environ, "VIRTUAL_ENV": str(new_venv)},
    )


STORAGE_SNAPSHOT = "storage-snapshot"
STORAGE_ROLLED_BACK = "storage-rolled-back"
SNAPSHOT_INFO = "snapshot.json"
ROLLBACK_SUFFIX = ".supervisor-rollback"


def _write_private(path: Path, content: bytes, mode: int) -> None:
    """Write a file that is never readable by others before it gets its mode."""
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "wb") as file:
        file.write(content)
        file.flush()
        os.fsync(file.fileno())
    path.chmod(mode)


def _copy_unless_vanished(src: str, dst: str) -> None:
    """Copy a file's content and permissions, or nothing if it went away.

    Auth stores are owner-only. macOS file flags are not copied, so a locked
    file can't make the copy undeletable.
    """
    with suppress(FileNotFoundError):
        source = Path(src)
        _write_private(
            Path(dst), source.read_bytes(), stat.S_IMODE(source.stat().st_mode)
        )


def _copy_tree(src: Path, dst: Path) -> None:
    """Copy a folder's files and permissions, not flags; symlinks stay links."""
    if not src.is_dir():
        raise FileNotFoundError(src)
    dst.mkdir(parents=True)
    for path in sorted(src.rglob("*")):
        if path.name.endswith(ROLLBACK_SUFFIX):
            continue
        target = dst / path.relative_to(src)
        if path.is_symlink():
            target.symlink_to(path.readlink())
        elif path.is_dir():
            target.mkdir()
        else:
            _copy_unless_vanished(str(path), str(target))


def snapshot_storage(
    path_core: Path, config: Path, from_version: str, to_version: str
) -> None:
    """Keep the .storage that from_version leaves, for a rollback from to_version.

    Run while no Core is running. On failure no snapshot is left at all, so an
    older one can never be restored by mistake.
    """
    snapshot = path_core / STORAGE_SNAPSHOT
    staging = path_core / f"{STORAGE_SNAPSHOT}.new"
    shutil.rmtree(staging, ignore_errors=True)
    try:
        _copy_tree(config / ".storage", staging / ".storage")
        (staging / SNAPSHOT_INFO).write_text(
            json.dumps(
                {
                    "from": from_version,
                    "to": to_version,
                    "taken": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                }
            )
        )
        shutil.rmtree(snapshot, ignore_errors=True)
        staging.rename(snapshot)
    except OSError:
        shutil.rmtree(staging, ignore_errors=True)
        shutil.rmtree(snapshot, ignore_errors=True)
        raise


def _parse(content: bytes) -> Any:
    try:
        return json.loads(content)
    except ValueError:
        return None


def _read_json(path: Path) -> Any:
    try:
        return _parse(path.read_bytes())
    except OSError:
        return None


def _files(folder: Path) -> dict[Path, Path]:
    """Return {path relative to folder: path} of a folder's regular files."""
    return {
        path.relative_to(folder): path
        for path in folder.rglob("*")
        if path.is_file()
        and not path.is_symlink()
        and not path.name.endswith(ROLLBACK_SUFFIX)
    }


def _links(folder: Path) -> set[Path]:
    """Return the paths relative to folder of a folder's symlinks."""
    return {path.relative_to(folder) for path in folder.rglob("*") if path.is_symlink()}


def _staged(path: Path) -> Path:
    return path.with_name(f".{path.name}{ROLLBACK_SUFFIX}")


def _make_parents(path: Path) -> list[Path]:
    """Create path's missing parent folders; return them, deepest first."""
    missing = []
    parent = path.parent
    while not parent.exists():
        missing.append(parent)
        parent = parent.parent
    path.parent.mkdir(parents=True, exist_ok=True)
    return missing


def _remove_empty(folders: list[Path]) -> None:
    """Remove the given folders, deepest first, where they are empty."""
    for folder in sorted(folders, key=lambda f: len(f.parts), reverse=True):
        with suppress(OSError):
            folder.rmdir()


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def _is_upgrade(previous: AwesomeVersion, version: AwesomeVersion) -> bool:
    """Return True unless version is the same as or older than previous.

    A downgrade keeps the snapshot of the last upgrade: the older Core reads
    the newer formats no better than before.
    """
    try:
        return version > previous
    except AwesomeVersionCompareException:
        return str(version) != str(previous)


class StorageRestore(NamedTuple):
    """What a rollback did to .storage."""

    put_back: list[str]
    moved_aside: list[str]


def restore_storage(
    path_core: Path, config: Path, to_version: str, from_version: str
) -> StorageRestore | None:
    """Put .storage back exactly as to_version left it before from_version ran.

    A newer Core changes stores in ways the older one can't use, and not only
    by raising major versions: minor migrations rename and re-key entries, and
    the stores refer to each other. So a rollback returns all of it. What the
    newer Core wrote is kept in ``<data>/core/storage-rolled-back``.

    Every copy and staged file is written before anything in .storage changes;
    then only renames and removals run, and if one fails, those already done
    are undone. Return None, changing nothing, unless the snapshot is of that
    update.
    """
    snapshot = path_core / STORAGE_SNAPSHOT
    info = _read_json(snapshot / SNAPSHOT_INFO)
    if (
        not isinstance(info, dict)
        or info.get("from") != to_version
        or info.get("to") != from_version
        or not (snapshot / ".storage").is_dir()
    ):
        return None

    storage = config / ".storage"
    for leftover in storage.rglob(f".*{ROLLBACK_SUFFIX}"):
        leftover.unlink(missing_ok=True)
    saved, current = _files(snapshot / ".storage"), _files(storage)
    changed = [
        name
        for name in sorted(saved)
        if name not in current or current[name].read_bytes() != saved[name].read_bytes()
    ]
    # A symlink Core wrote through became a file; it isn't new.
    created = sorted(current.keys() - saved.keys() - _links(snapshot / ".storage"))
    if not changed and not created:
        return StorageRestore([], [])

    kept = path_core / STORAGE_ROLLED_BACK
    shutil.rmtree(kept, ignore_errors=True)
    staged: list[Path] = []
    made: list[Path] = []
    try:
        for name in changed + created:
            if name in current:
                (kept / name).parent.mkdir(parents=True, exist_ok=True)
                _write_private(
                    kept / name,
                    current[name].read_bytes(),
                    stat.S_IMODE(current[name].stat().st_mode),
                )
        for name in changed:
            dst = storage / name
            if dst.is_dir() and not dst.is_symlink():
                raise IsADirectoryError(f"{dst} is a folder now")
            made += _make_parents(dst)
            staged.append(_staged(dst))
            _write_private(
                staged[-1],
                saved[name].read_bytes(),
                stat.S_IMODE(saved[name].stat().st_mode),
            )
    except OSError:
        for path in staged:
            path.unlink(missing_ok=True)
        _remove_empty(made)
        raise

    done: list[Path] = []
    try:
        # Removals first: on a case-insensitive disk a created "foo" is the
        # same file as a "Foo" put back, and removing it after would lose it.
        for name in created:
            (storage / name).unlink()
            done.append(name)
        for name in changed:
            _staged(storage / name).replace(storage / name)
            done.append(name)
        _fsync_dir(storage)
    except OSError:
        _undo_restore(storage, kept, done, current)
        for path in staged:
            path.unlink(missing_ok=True)
        _remove_empty(made)
        raise
    _remove_empty(
        [
            storage / folder
            for name in created
            for folder in name.parents
            if folder.parts and not (snapshot / ".storage" / folder).is_dir()
        ]
    )
    return StorageRestore([str(n) for n in changed], [str(n) for n in created])


def _undo_restore(
    storage: Path, kept: Path, done: list[Path], current: dict[Path, Path]
) -> None:
    """Return the files a failed restore already changed to the newer Core's."""
    for name in reversed(done):
        dst = storage / name
        try:
            if name in current:
                kept_file = kept / name
                _write_private(
                    _staged(dst),
                    kept_file.read_bytes(),
                    stat.S_IMODE(kept_file.stat().st_mode),
                )
                _staged(dst).replace(dst)
            else:
                dst.unlink(missing_ok=True)
        except OSError as err:
            _LOGGER.critical(
                "%s is left as the older Core had it; the newer Core's copy is %s: %s",
                dst,
                kept / name,
                err,
            )


class NativeHomeAssistant(JobGroup):
    """Replacement for DockerHomeAssistant on a native host."""

    def __init__(self, coresys: CoreSys) -> None:
        """Initialize."""
        super().__init__(coresys, "native_home_assistant", HASS_NAME)
        self._version: AwesomeVersion | None = None
        # Set by HomeAssistantCore.update while it rolls back a failed update.
        self.rolling_back: bool = False
        # (from, to) of the .storage snapshot this process took for the update
        # in progress; a rollback restores only that one.
        self._storage_snapshot: tuple[str, str] | None = None
        # Set by the watchdog: a Core that doesn't answer at all won't stop cleanly
        # (its event loop handles SIGTERM), so don't wait the full timeout. A slow
        # Core can take over 30 s to stop, which HUNG_STOP_TIMEOUT has to allow.
        self.hung: bool = False
        self._environment: dict[str, str] = {}
        self._watch_task: asyncio.Task | None = None
        self._last_state: ContainerState | None = None
        self.agent = service(f"{_launchd_prefix()}.core")

    # Paths

    @property
    def path_core(self) -> Path:
        """Return the root of the native Core installs."""
        return self.sys_config.path_supervisor / "core"

    def path_version(self, version: AwesomeVersion) -> Path:
        """Return the virtual environment of a Core version."""
        return version_path(self.path_core, version)

    @property
    def path_logs(self) -> Path:
        """Return the folder for Core's stdout/stderr."""
        return self.path_core / "logs"

    # DockerInterface compatible properties

    @property
    def name(self) -> str:
        """Return the service name used in state events."""
        return HASS_NAME

    @property
    def timeout(self) -> int:
        """Return the stop timeout."""
        return HUNG_STOP_TIMEOUT if self.hung else STOP_TIMEOUT

    @property
    def attached(self) -> bool:
        """Return True once a version is attached."""
        return self._version is not None

    @property
    def meta_config(self) -> dict[str, Any]:
        """Return the service environment in Docker's metadata shape."""
        return {"Env": [f"{key}={value}" for key, value in self._environment.items()]}

    @property
    def version(self) -> AwesomeVersion | None:
        """Return the attached Core version."""
        return self._version

    @property
    def image(self) -> str | None:
        """Natively the 'image' is the Core venv; keep the configured name."""
        return self.sys_homeassistant.default_image

    @property
    def machine(self) -> str | None:
        """Return the machine type."""
        return self.sys_machine

    @property
    def arch(self) -> str | None:
        """Return the native architecture."""
        return self.sys_arch.default

    @property
    def ip_address(self) -> IPv4Address:
        """Core listens on the host."""
        return LOCALHOST

    @property
    def in_progress(self) -> bool:
        """Return True if a job of this runtime is running."""
        return self.active_job is not None

    @property
    def restart_policy(self) -> None:
        """The Supervisor's watchdog restarts Core, not launchd."""
        return None

    # Install

    def _installed(self, version: AwesomeVersion) -> bool:
        return is_installed(self.path_core, version)

    async def _install(self, version: AwesomeVersion) -> None:
        """Install a Core version into its own virtual environment."""
        if version == LANDINGPAGE:
            raise DockerError("There is no native landing page", _LOGGER.debug)
        if str(version) == str(self._version) and await self.is_running():
            # Rebuilding deletes the venv first; never pull it from under Core.
            raise DockerError(
                f"Not reinstalling Core {version} while it is running", _LOGGER.error
            )
        try:
            await install_core(self.path_core, version)
        except InstallError as err:
            raise DockerError(str(err), _LOGGER.error) from err

    async def install(
        self,
        version: AwesomeVersion,
        image: str | None = None,
        latest: bool = False,
        **kwargs: Any,
    ) -> None:
        """Install a Core version unless a usable install exists, and use it."""
        await self._prepare(version)
        self._version = version

    async def _prepare(self, version: AwesomeVersion) -> None:
        """Install a Core version unless a usable install exists.

        The version being rolled back to is reused as is: it was running a
        moment ago, and rebuilding it would cost time and could fail.
        """
        reuse = await self.sys_run_in_executor(
            is_installed if self.rolling_back else is_current, self.path_core, version
        )
        if not reuse:
            await self._install(version)

    async def update(
        self, version: AwesomeVersion, image: str | None = None, latest: bool = False
    ) -> None:
        """Install the new version while the old one runs, then stop the old one.

        The new version is recorded only once the old one has stopped, so a
        failure on the way leaves the Supervisor on the version that still runs.
        """
        previous = self._version
        if self.rolling_back:
            await self._roll_back(version)
            return
        self._storage_snapshot = None
        components = await self._running_components()
        await self._prepare(version)
        if previous and str(previous) != str(version):
            await self._carry_over(previous, version)
        if components:
            await self._install_integration_requirements(version, components)
        await self.stop()
        self._version = version
        if previous and _is_upgrade(previous, version):
            await self._snapshot_storage(previous, version)

    async def _roll_back(self, version: AwesomeVersion) -> None:
        """Switch back to the previous install exactly as it ran.

        No requirements are installed: that needs the network, and must not
        stand between a broken Core and its rollback.
        """
        rolled_back_from = self._version
        await self._prepare(version)
        await self.stop()
        self._version = version
        if rolled_back_from and str(rolled_back_from) != str(version):
            await self._restore_storage(rolled_back_from, version)

    async def _snapshot_storage(
        self, previous: AwesomeVersion, version: AwesomeVersion
    ) -> None:
        """Keep previous's .storage for a rollback; never fail the update."""
        try:
            await self.sys_run_in_executor(
                snapshot_storage,
                self.path_core,
                self.sys_config.path_homeassistant,
                str(previous),
                str(version),
            )
        except Exception as err:  # pylint: disable=broad-except
            # A safety net for the rollback; it must never fail the update.
            _LOGGER.error(
                "Can't keep the .storage of Core %s; a rollback from %s may not start: %s",
                previous,
                version,
                err,
            )
            return
        self._storage_snapshot = (str(previous), str(version))
        _LOGGER.info("Kept the .storage of Core %s for a rollback", previous)

    async def _restore_storage(
        self, rolled_back_from: AwesomeVersion, version: AwesomeVersion
    ) -> None:
        """Give version back the .storage it left before rolled_back_from ran."""
        taken, self._storage_snapshot = self._storage_snapshot, None
        restored = None
        if taken == (str(version), str(rolled_back_from)):
            try:
                restored = await self.sys_run_in_executor(
                    restore_storage,
                    self.path_core,
                    self.sys_config.path_homeassistant,
                    str(version),
                    str(rolled_back_from),
                )
            except Exception as err:  # pylint: disable=broad-except
                # The rollback must go on whatever happens here.
                _LOGGER.error(
                    "Can't put back the .storage of Core %s: %s", version, err
                )
                return
        if restored is None:
            _LOGGER.warning(
                "No .storage snapshot of the update from Core %s to %s; "
                "Core %s starts on the .storage %s left",
                version,
                rolled_back_from,
                version,
                rolled_back_from,
            )
        elif restored.put_back or restored.moved_aside:
            _LOGGER.warning(
                "Put .storage back as Core %s left it: %d files put back, %d files "
                "Core %s created moved aside; Core %s's copies are in %s",
                version,
                len(restored.put_back),
                len(restored.moved_aside),
                rolled_back_from,
                rolled_back_from,
                self.path_core / STORAGE_ROLLED_BACK,
            )
        else:
            _LOGGER.info(
                "Core %s left .storage as Core %s had it; nothing put back",
                rolled_back_from,
                version,
            )

    async def _carry_over(
        self, previous: AwesomeVersion, version: AwesomeVersion
    ) -> None:
        """Bring the previous install's on-demand packages into the new one."""
        old_venv = self.path_version(previous)
        if not await self.sys_run_in_executor(is_installed, self.path_core, previous):
            return
        result = await carry_over_packages(old_venv, self.path_version(version))
        if result and result.exit_code != 0:
            raise DockerError(
                f"Can't carry packages over into Core {version}:\n"
                + "".join(result.log[-30:]),
                _LOGGER.error,
            )

    async def _running_components(self) -> list[str]:
        """Return the integrations the running Core has loaded, if it runs."""
        if not await self.is_running():
            return []
        try:
            return list((await self.sys_homeassistant.api.get_config())["components"])
        except (HomeAssistantAPIError, KeyError) as err:
            _LOGGER.warning("Can't read Core's loaded integrations: %s", err)
            return []

    async def _install_integration_requirements(
        self, version: AwesomeVersion, components: list[str]
    ) -> None:
        """Install the requirements of the loaded integrations into a new install.

        Core would install them on its first start, after the old version is
        already stopped; doing it now keeps the switchover short and lets a
        failure abort the update while the old version still runs.
        """
        venv = self.path_version(version)
        config = self.sys_config.path_homeassistant

        requirements = await self.sys_run_in_executor(
            integration_requirements, venv, config, components
        )
        if not requirements:
            return
        _LOGGER.info(
            "Installing %d integration requirements into Core %s",
            len(requirements),
            version,
        )
        result = await install_requirements(venv, requirements)
        if result.exit_code != 0:
            raise DockerError(
                f"Can't install integration requirements into Core {version}:\n"
                + "".join(result.log[-30:]),
                _LOGGER.error,
            )

    async def check_image(self, *args: Any, **kwargs: Any) -> None:
        """Nothing to check natively."""

    async def get_latest_version(self) -> AwesomeVersion:
        """Return the latest Core release known to the updater."""
        if version := self.sys_updater.version_homeassistant:
            return version
        raise DockerError("No Home Assistant version information", _LOGGER.warning)

    async def cleanup(
        self,
        old_image: str | None = None,
        image: str | None = None,
        version: AwesomeVersion | None = None,
    ) -> None:
        """Keep the current version and the newest older one (for rollback)."""
        current = str(version or self._version or "")
        versions = self.path_core / "versions"

        def _cleanup() -> None:
            if not versions.is_dir():
                return
            older = sorted(
                (p for p in versions.iterdir() if p.is_dir() and p.name != current),
                key=lambda p: p.stat().st_mtime,
            )
            for path in older[:-1]:
                _LOGGER.info("Removing old Home Assistant Core %s", path.name)
                shutil.rmtree(path, ignore_errors=True)

        await self.sys_run_in_executor(_cleanup)

    # Lifecycle

    async def attach(
        self, version: AwesomeVersion, *, skip_state_event_if_down: bool = False
    ) -> None:
        """Attach to an installed version and the service if it runs."""
        if not await self.sys_run_in_executor(self._installed, version):
            raise DockerError(f"Home Assistant Core {version} is not installed")
        self._version = version
        if plist := await self.sys_run_in_executor(self.agent.read_plist):
            self._environment = dict(plist.get("EnvironmentVariables", {}))
        self._start_watch()
        # With boot off, a Core that is down after a Supervisor restart stays
        # down. Once it has run, the watchdog looks after it (see _fire).
        self.stop_requested = not self.sys_homeassistant.boot
        if not skip_state_event_if_down or await self.is_running():
            self._fire(await self.current_state())

    def _program(self) -> list[str]:
        assert self._version
        return [
            str(self.path_version(self._version) / "bin" / "hass"),
            "-c",
            str(self.sys_config.path_homeassistant),
        ]

    async def is_initialize(self) -> bool:
        """Return True if the service is defined for the current version."""
        plist = await self.sys_run_in_executor(self.agent.read_plist)
        if not plist or not self._version:
            return False
        return plist.get("ProgramArguments") == self._program()

    async def exists(self) -> bool:
        """Return True if the attached version is installed."""
        return self._version is not None and await self.sys_run_in_executor(
            self._installed, self._version
        )

    async def run(self, *, restore_job_id: str | None = None) -> None:
        """Define the service for the current version and start it."""
        if not self._version:
            raise DockerError("No Home Assistant Core version to run")
        environment = {
            "SUPERVISOR": api_address(),
            "HASSIO": api_address(),
            ENV_TIME: self.sys_timezone,
            ENV_TOKEN: self.sys_homeassistant.supervisor_token or "",
            ENV_TOKEN_OLD: self.sys_homeassistant.supervisor_token or "",
            # Natively there is no container log to read, so keep Core's
            # usual home-assistant.log in the config folder as well.
            ENV_DUPLICATE_LOG_FILE: "1",
            "HOME": str(Path.home()),
            "PATH": "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin",
            ENV_SERIAL_BY_ID_DIR: str(self.sys_config.path_supervisor / NATIVE_BY_ID),
        }
        if restore_job_id:
            environment[ENV_RESTORE_JOB_ID] = restore_job_id
        if self.sys_homeassistant.api.supports_unix_socket:
            await self.sys_run_in_executor(
                lambda: SOCKET_CORE.parent.mkdir(parents=True, exist_ok=True)
            )
            environment[ENV_CORE_API_SOCKET] = str(SOCKET_CORE)
            # Where the Supervisor listens for Core, as on HA OS
            environment[ENV_SUPERVISOR_SOCKET] = str(
                self.sys_homeassistant.path_run_supervisor / SOCKET_NAME_SUPERVISOR
            )

        await self.stop()

        def _write() -> None:
            self.path_logs.mkdir(parents=True, exist_ok=True)
            self.agent.write_plist(
                self._program(),
                environment=environment,
                working_directory=self.sys_config.path_homeassistant,
                stdout=self.path_logs / "stdout.log",
                stderr=self.path_logs / "stderr.log",
            )

        await self.sys_run_in_executor(_write)
        self._environment = environment
        await self.start()

    async def start(self) -> None:
        """Start the defined service."""
        self.stop_requested = False
        try:
            if (await self.agent.status()).loaded:
                await self.agent.kickstart()
            else:
                await self.agent.bootstrap()
        except LaunchdError as err:
            raise DockerError(f"Can't start Home Assistant Core: {err}") from err
        self._start_watch()

    async def stop(self, remove_container: bool = True) -> None:
        """Stop the service; remove its definition if asked."""
        self._expected_down = True
        try:
            # A Core stuck past the timeout gets SIGABRT: Core's fault handler
            # writes every thread's Python stack to home-assistant.log.fault.
            await self.agent.bootout(self.timeout, abort_first=True)
        except LaunchdError as err:
            raise DockerError(f"Can't stop Home Assistant Core: {err}") from err
        finally:
            self._expected_down = False
        if remove_container:
            await self.sys_run_in_executor(self.agent.remove_plist)

    async def capture_stacks(self) -> None:
        """Save the native stacks of a Core that stopped answering.

        Restarting destroys the only evidence of why it hung. Failing to
        capture must never get in the way of that restart.
        """
        name = f"hang-{time.strftime('%Y%m%d-%H%M%S')}.sample.txt"
        path = self.path_logs / name

        def prune() -> None:
            self.path_logs.mkdir(parents=True, exist_ok=True)
            samples = sorted(self.path_logs.glob("hang-*.sample.txt"))
            for old in samples[: -(HANG_SAMPLES_KEPT - 1)]:
                old.unlink(missing_ok=True)

        try:
            await self.sys_run_in_executor(prune)
            if await self.agent.sample(path):
                _LOGGER.warning("Saved Home Assistant Core's stacks to %s", path)
            else:
                _LOGGER.warning("Could not sample Home Assistant Core's stacks")
        except (OSError, LaunchdError) as err:
            _LOGGER.warning("Could not sample Home Assistant Core's stacks: %s", err)

    async def restart(self) -> None:
        """Restart the service."""
        await self.stop(remove_container=False)
        await self.start()

    async def remove(self, *, remove_image: bool = True) -> None:
        """Remove the service and, if asked, the installed version."""
        await self.stop()
        if remove_image and self._version:
            await self.sys_run_in_executor(
                shutil.rmtree, self.path_version(self._version), True
            )

    async def is_running(self) -> bool:
        """Return True if Core is running."""
        return (await self.agent.status()).running

    async def current_state(self) -> ContainerState:
        """Map the launchd state to a container state."""
        status = await self.agent.status()
        if status.running:
            return ContainerState.RUNNING
        if status.loaded and status.last_exit_code not in (None, 0):
            self._last_exit_code = status.last_exit_code
            return ContainerState.FAILED
        return ContainerState.STOPPED

    async def is_failed(self) -> bool:
        """Return True if Core exited with an error."""
        return await self.current_state() == ContainerState.FAILED

    # Watchdog support: launchd has no event stream we can subscribe to, so
    # poll the service and fire the same events the Docker monitor fires.

    _expected_down: bool = False
    # Someone asked for Core to stay down, so the watchdog leaves it alone.
    stop_requested: bool = False
    _last_exit_code: int | None = None

    def _fire(self, state: ContainerState) -> None:
        self._last_state = state
        if state == ContainerState.RUNNING:
            self.stop_requested = False
        # The watchdog logs the exit code of a failure as a number.
        status_exit = (
            self._last_exit_code or 1 if state == ContainerState.FAILED else None
        )
        self.sys_bus.fire_event(
            BusEvent.DOCKER_CONTAINER_STATE_CHANGE,
            DockerContainerStateEvent(
                name=self.name,
                state=state,
                id=self.agent.label,
                time=int(time.time()),
                exit_code=status_exit,
            ),
        )

    def _start_watch(self) -> None:
        if self._watch_task is None or self._watch_task.done():
            self._watch_task = self.sys_create_task(self._watch())

    async def _watch(self) -> None:
        while True:
            await asyncio.sleep(POLL_SECONDS)
            if self._expected_down:
                continue
            state = await self.current_state()
            # A stop may have started during that await.
            if state != self._last_state and not self._expected_down:
                self._fire(state)

    # Commands and logs

    async def execute_command(self, command: list[str]) -> CommandReturn:
        """Run a Core command (only 'python3 -m homeassistant ...' is used)."""
        if not self._version:
            raise DockerError("No Home Assistant Core installed")
        python = str(self.path_version(self._version) / "bin" / "python3")
        args = [python if arg == "python3" else arg for arg in command]
        args = [
            str(self.sys_config.path_homeassistant) if arg == "/config" else arg
            for arg in args
        ]
        return await _run(*args, env={**os.environ, ENV_TIME: self.sys_timezone})

    async def logs(self) -> list[str]:
        """Return the tail of Core's output."""

        def _tail() -> list[str]:
            lines: deque[str] = deque(maxlen=LOG_TAIL_LINES)
            for name in ("stdout.log", "stderr.log"):
                path = self.path_logs / name
                if path.exists():
                    with path.open(errors="replace") as file:
                        lines.extend(file)
            return list(lines)

        return await self.sys_run_in_executor(_tail)

    async def stats(self, *, one_shot: bool = False) -> DockerStats:
        """Return Core's CPU and memory use."""
        status = await self.agent.status()
        if not status.running or status.pid is None:
            raise DockerContainerNotRunningError(name=self.name)
        return await process_stats(status.pid, name=self.name, one_shot=one_shot)

    def __getattr__(self, name: str) -> Any:
        """Fail loudly on DockerInterface members not provided natively."""
        raise AttributeError(f"NativeHomeAssistant has no attribute {name!r}")
