"""The Home Assistant CLI (`ha`) on macOS, built from source.

On HA OS the CLI is a plugin container (home-assistant/plugin-cli) that bundles
one release of the `ha` binary (home-assistant/cli) and gets the plugin's
Supervisor token in its environment. Natively, a plugin version stands for the
`ha` release its image bundles: that release's source is built with Go into the
Supervisor's data folder, and an `ha` command in ~/.local/bin runs it with this
Supervisor's address and the plugin's token. So `ha core info` works in any
Terminal on the Mac, as it does in the HA OS console.

The CLI is a command, not a service: "running" means built and linked.
"""

from __future__ import annotations

import asyncio
import io
import json
import logging
import os
from pathlib import Path
import re
import shutil
import tarfile
import tempfile
from typing import Any
import zlib

import aiohttp
from awesomeversion import AwesomeVersion

from ..const import CLI_DOCKER_NAME
from ..docker.const import ContainerState
from ..docker.stats import DockerStats
from ..exceptions import DockerError
from ..jobs.job_group import JobGroup
from ..plugins.const import FILE_HASSIO_CLI
from .app import HOST_PATH, INSTALLED_MARKER
from .homeassistant import _run, api_address

_LOGGER: logging.Logger = logging.getLogger(__name__)

PLUGIN_DOCKERFILE = (
    "https://raw.githubusercontent.com/home-assistant/plugin-cli/{version}/Dockerfile"
)
CLI_SOURCE = "https://github.com/home-assistant/cli/archive/refs/tags/{release}.tar.gz"
_RE_CLI_RELEASE = re.compile(r"^ARG CLI_VERSION=(\d+\.\d+\.\d+)\s*$", re.MULTILINE)
_RE_PLUGIN_VERSION = re.compile(r"^\d{4}\.\d+\.\d+$")
_DOWNLOAD_TIMEOUT = aiohttp.ClientTimeout(total=120)
_MAX_SOURCE_BYTES = 50 * 1024 * 1024
_CHUNK = 64 * 1024
_MAX_UNPACKED_BYTES = 200 * 1024 * 1024
# A first build fetches Go modules; a hung download must not hold the plugin
_BUILD_TIMEOUT = 15 * 60
# Where the `ha` command goes: on the PATH of the account's login shell
USER_BIN = Path.home() / ".local" / "bin"
WRAPPER = """\
#!/bin/sh
# Home Assistant CLI for this Mac's native Supervisor (written by the Supervisor).
# Runs ha {release} (CLI plugin {version}) with the CLI plugin's token.
SUPERVISOR_TOKEN=$(/usr/bin/plutil -extract access_token raw -o - {token_file} 2>/dev/null)
if [ -z "$SUPERVISOR_TOKEN" ]; then
  echo "ha: the Supervisor hasn't started its CLI plugin yet" >&2
  exit 1
fi
SUPERVISOR_ENDPOINT={endpoint}
export SUPERVISOR_TOKEN SUPERVISOR_ENDPOINT
exec {binary} "$@"
"""


def cli_release(dockerfile: str) -> str:
    """Return the `ha` release a plugin-cli Dockerfile bundles."""
    if not (match := _RE_CLI_RELEASE.search(dockerfile)):
        raise DockerError("The CLI plugin's Dockerfile names no ha release")
    return match.group(1)


def _extract(data: bytes, target: Path) -> Path:
    """Unpack a source tarball; return its single top folder."""
    try:
        with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as archive:
            if (
                sum(member.size for member in archive.getmembers())
                > _MAX_UNPACKED_BYTES
            ):
                raise DockerError("The ha source unpacks larger than expected")
            archive.extractall(target, filter="data")
    except (tarfile.TarError, EOFError, OSError, zlib.error) as err:
        raise DockerError(f"Can't unpack the ha source: {err}") from err
    folders = [entry for entry in target.iterdir() if entry.is_dir()]
    if len(folders) != 1 or not (folders[0] / "go.mod").is_file():
        raise DockerError("The ha source has an unexpected layout")
    return folders[0]


def _shell_quote(value: str) -> str:
    return "'" + value.replace("'", "'\\''") + "'"


class NativeCli(JobGroup):
    """Replacement for DockerCli on a native host."""

    def __init__(self, coresys) -> None:
        """Initialize."""
        super().__init__(coresys, "native_cli", "cli")
        self._version: AwesomeVersion | None = None
        # One build or relink at a time: startup, repair and updates can overlap
        self._lock = asyncio.Lock()

    # Names and paths

    @property
    def name(self) -> str:
        """Return the name HA OS gives the CLI container."""
        return CLI_DOCKER_NAME

    @property
    def image(self) -> str | None:
        """Return the plugin's image name, as reported on HA OS."""
        return self.sys_plugins.cli.image

    @property
    def version(self) -> AwesomeVersion | None:
        """Return the attached plugin version."""
        return self._version

    @property
    def in_progress(self) -> bool:
        """Return True while a build or relink runs."""
        return self.active_job is not None or self._lock.locked()

    @property
    def path_root(self) -> Path:
        """Return the folder of the CLI builds."""
        return self.sys_config.path_supervisor / "cli"

    def path_version(self, version: AwesomeVersion | str) -> Path:
        """Return the build folder of a plugin version."""
        return self.path_root / "versions" / str(version)

    @property
    def path_command(self) -> Path:
        """Return the `ha` script the link in ~/.local/bin points at."""
        return self.path_root / "ha"

    def _installed(self, version: AwesomeVersion | str | None) -> bool:
        return version is not None and (
            (self.path_version(version) / INSTALLED_MARKER).is_file()
        )

    def _linked(self) -> bool:
        link = USER_BIN / "ha"
        return self.path_command.is_file() and (
            not USER_BIN.is_dir()
            or (link.is_symlink() and link.readlink() == self.path_command)
        )

    # Build

    async def _download(self, url: str) -> bytes:
        try:
            async with self.sys_websession.get(url, timeout=_DOWNLOAD_TIMEOUT) as resp:
                if resp.status != 200:
                    raise DockerError(f"Can't download {url}: HTTP {resp.status}")
                # read(n) returns what has arrived so far: read to the end
                chunks: list[bytes] = []
                size = 0
                async for chunk in resp.content.iter_chunked(_CHUNK):
                    size += len(chunk)
                    if size > _MAX_SOURCE_BYTES:
                        raise DockerError(f"{url} is larger than expected")
                    chunks.append(chunk)
        except (aiohttp.ClientError, TimeoutError) as err:
            raise DockerError(f"Can't download {url}: {err}") from err
        return b"".join(chunks)

    async def _build(self, version: AwesomeVersion) -> None:
        """Build the `ha` release a CLI plugin version bundles.

        The build lands in a staging folder that replaces the version's folder
        only once it worked, so a failed build never removes a good one.
        """
        if not _RE_PLUGIN_VERSION.match(str(version)):
            raise DockerError(f"Unexpected CLI plugin version {version}", _LOGGER.error)
        go = await self.sys_run_in_executor(lambda: shutil.which("go", path=HOST_PATH))
        if go is None:
            raise DockerError(
                "Building the ha command needs Go: brew install go", _LOGGER.error
            )
        dockerfile = await self._download(PLUGIN_DOCKERFILE.format(version=version))
        release = cli_release(dockerfile.decode(errors="replace"))
        source = await self._download(CLI_SOURCE.format(release=release))

        target = self.path_version(version)
        _LOGGER.info("Building ha %s for the CLI plugin %s", release, version)

        def prepare() -> tuple[Path, Path]:
            target.parent.mkdir(parents=True, exist_ok=True)
            self.sys_config.path_tmp.mkdir(parents=True, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f".{version}.", dir=target.parent))
            scratch = Path(
                tempfile.mkdtemp(prefix="ha-cli-", dir=self.sys_config.path_tmp)
            )
            return staging, scratch

        def finish() -> None:
            (staging / INSTALLED_MARKER).write_text(json.dumps({"release": release}))
            if target.exists():
                shutil.rmtree(target)
            staging.rename(target)

        try:
            staging, scratch = await self.sys_run_in_executor(prepare)
        except OSError as err:
            raise DockerError(f"Can't prepare the ha build: {err}") from err
        try:
            tree = await self.sys_run_in_executor(_extract, source, scratch)
            # As upstream releases it: static, stripped
            result = await asyncio.wait_for(
                _run(
                    go,
                    "build",
                    "-trimpath",
                    "-ldflags=-s -w",
                    "-o",
                    str(staging / "ha"),
                    ".",
                    cwd=tree,
                    env={
                        **os.environ,
                        # Go keeps its module and build caches under HOME
                        "HOME": os.environ.get("HOME") or str(Path.home()),
                        "PATH": HOST_PATH,
                        "CGO_ENABLED": "0",
                    },
                ),
                _BUILD_TIMEOUT,
            )
            if result.exit_code != 0:
                _LOGGER.error(
                    "Building ha %s failed:\n%s", release, "".join(result.log[-30:])
                )
                raise DockerError(f"Can't build ha {release}")
            await self.sys_run_in_executor(finish)
        except TimeoutError as err:
            raise DockerError(f"Building ha {release} took too long") from err
        except OSError as err:
            raise DockerError(f"Can't build ha {release}: {err}") from err
        finally:

            def tidy() -> None:
                shutil.rmtree(scratch, ignore_errors=True)
                shutil.rmtree(staging, ignore_errors=True)

            await self.sys_run_in_executor(tidy)
        _LOGGER.info("Built ha %s for the CLI plugin %s", release, version)

    def _release(self, version: AwesomeVersion) -> str:
        try:
            marker = json.loads(
                (self.path_version(version) / INSTALLED_MARKER).read_text()
            )
            return str(marker["release"])
        except OSError, ValueError, KeyError, TypeError:
            return "unknown"

    def _write_command(self, token_file: Path) -> None:
        """Write the `ha` script and link it into ~/.local/bin."""
        assert self._version is not None
        script = WRAPPER.format(
            release=self._release(self._version),
            version=self._version,
            token_file=_shell_quote(str(token_file)),
            endpoint=api_address(),
            binary=_shell_quote(str(self.path_version(self._version) / "ha")),
        )
        tmp = self.path_command.with_suffix(".new")
        tmp.write_text(script)
        tmp.chmod(0o700)
        tmp.replace(self.path_command)

        if not USER_BIN.is_dir():
            _LOGGER.warning("No %s; run ha as %s", USER_BIN, self.path_command)
            return
        link = USER_BIN / "ha"
        if link.is_symlink() and link.readlink() == self.path_command:
            return
        if link.exists() or link.is_symlink():
            _LOGGER.warning(
                "%s is something else; leaving it, run ha as %s",
                link,
                self.path_command,
            )
            return
        link.symlink_to(self.path_command)
        _LOGGER.info("Linked %s to the native ha command", link)

    # The interface PluginBase and PluginCli use

    async def get_latest_version(self) -> AwesomeVersion:
        """Return the newest plugin version with a build."""

        def newest() -> AwesomeVersion | None:
            folder = self.path_root / "versions"
            if not folder.is_dir():
                return None
            built = [
                AwesomeVersion(entry.name)
                for entry in folder.iterdir()
                # Dot folders are builds in progress
                if not entry.name.startswith(".")
                and (entry / INSTALLED_MARKER).is_file()
            ]
            return max(built) if built else None

        if (version := await self.sys_run_in_executor(newest)) is None:
            raise DockerError("No ha build found")
        return version

    async def attach(
        self, version: AwesomeVersion, *, skip_state_event_if_down: bool = False
    ) -> None:
        """Attach to a built version."""
        if not await self.sys_run_in_executor(self._installed, version):
            raise DockerError(f"The ha command for CLI plugin {version} isn't built")
        self._version = version

    async def check_image(self, *args: Any, **kwargs: Any) -> None:
        """Raise unless the attached version is built."""
        if not await self.sys_run_in_executor(self._installed, self._version):
            raise DockerError("The ha command isn't built")

    async def exists(self) -> bool:
        """Return True if the attached version is built."""
        return await self.sys_run_in_executor(self._installed, self._version)

    async def install(
        self, version: AwesomeVersion, image: str | None = None, **kwargs: Any
    ) -> None:
        """Build a version unless it is built."""
        async with self._lock:
            if not await self.sys_run_in_executor(self._installed, version):
                await self._build(version)
            self._version = version

    async def update(
        self, version: AwesomeVersion, image: str | None = None, **kwargs: Any
    ) -> None:
        """Build the new version; PluginCli.update starts it afterwards."""
        await self.install(version)

    async def cleanup(self, *args: Any, **kwargs: Any) -> None:
        """Remove builds of other versions."""

        def remove_others() -> None:
            folder = self.path_root / "versions"
            if not folder.is_dir():
                return
            for entry in folder.iterdir():
                if entry.name != str(self._version) and not entry.name.startswith("."):
                    shutil.rmtree(entry, ignore_errors=True)

        async with self._lock:
            await self.sys_run_in_executor(remove_others)

    async def run(self) -> None:
        """Point the `ha` command at the attached build and current token."""
        async with self._lock:
            if not await self.sys_run_in_executor(self._installed, self._version):
                raise DockerError("The ha command isn't built")
            try:
                await self.sys_run_in_executor(self._write_command, FILE_HASSIO_CLI)
            except OSError as err:
                raise DockerError(f"Can't write the ha command: {err}") from err
        _LOGGER.info("The ha command runs CLI plugin %s", self._version)

    async def stop(self, remove_container: bool = True) -> None:
        """Nothing runs between commands."""

    async def is_running(self) -> bool:
        """Return True if built and the `ha` command points at it."""
        return await self.sys_run_in_executor(
            lambda: self._installed(self._version) and self._linked()
        )

    async def current_state(self) -> ContainerState:
        """Return running when the command is ready."""
        return (
            ContainerState.RUNNING
            if await self.is_running()
            else ContainerState.STOPPED
        )

    async def is_failed(self) -> bool:
        """Return False: a command between runs never fails."""
        return False

    async def stats(self, *, one_shot: bool = False) -> DockerStats:
        """Nothing runs between commands: no usage."""
        return DockerStats({})
