"""Test the natively built Home Assistant CLI (`ha`)."""

import asyncio
import io
import json
from pathlib import Path
import subprocess
import tarfile
from unittest.mock import AsyncMock, MagicMock, patch

from awesomeversion import AwesomeVersion
import pytest

from supervisor.docker.const import ContainerState
from supervisor.docker.manager import CommandReturn
from supervisor.exceptions import DockerError
from supervisor.native import cli as native_cli
from supervisor.native.cli import NativeCli, cli_release
from supervisor.plugins import cli as plugin_cli
from supervisor.plugins.cli import PluginCli

DOCKERFILE = """\
FROM ghcr.io/home-assistant/base:3.24-2026.10.1@sha256:992dc6a5
# Install CLI (version updated by Renovate)
ARG CLI_VERSION=5.6.0
ARG TARGETARCH
"""
VERSION = AwesomeVersion("2026.10.1")
STAND_IN = (
    '#!/bin/sh\necho "token=$SUPERVISOR_TOKEN endpoint=$SUPERVISOR_ENDPOINT args=$*"\n'
)


def _sh(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(args, capture_output=True, text=True, check=False)


def _source(top: str = "cli-5.6.0") -> bytes:
    """Return a gzipped tarball shaped like GitHub's source archive."""
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as archive:
        for name, data in ((f"{top}/go.mod", b"module x\n"), (f"{top}/main.go", b"")):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


@pytest.fixture
async def native(tmp_path: Path):
    """Return a NativeCli whose data and ~/.local/bin live in tmp_path."""
    loop = asyncio.get_running_loop()
    coresys = MagicMock()
    coresys.config.path_supervisor = tmp_path / "data"
    coresys.config.path_tmp = tmp_path / "tmp"
    (tmp_path / "tmp").mkdir()
    (tmp_path / "bin").mkdir()
    coresys.run_in_executor = lambda func, *args: loop.run_in_executor(
        None, func, *args
    )
    instance = NativeCli(coresys)
    with (
        patch.object(native_cli, "USER_BIN", tmp_path / "bin"),
        patch.object(native_cli, "FILE_HASSIO_CLI", tmp_path / "data" / "cli.json"),
        patch.object(native_cli, "api_address", return_value="127.0.0.1:4358"),
    ):
        yield instance


def _fake_build(calls: list):
    """Return a fake _run for go build that writes a stand-in ha."""

    async def run(*args: str, cwd: Path | None = None, env=None) -> CommandReturn:
        calls.append((args, cwd, env))
        out = Path(args[args.index("-o") + 1])
        await asyncio.to_thread(out.write_text, STAND_IN)
        await asyncio.to_thread(out.chmod, 0o755)
        return CommandReturn(0, [])

    return run


def _downloads(native: NativeCli, answers: dict[str, bytes]) -> None:
    async def download(url: str) -> bytes:
        return answers[url]

    native._download = download  # type: ignore[method-assign]


def _answers() -> dict[str, bytes]:
    return {
        native_cli.PLUGIN_DOCKERFILE.format(version=VERSION): DOCKERFILE.encode(),
        native_cli.CLI_SOURCE.format(release="5.6.0"): _source(),
    }


def test_cli_release() -> None:
    """Test the bundled ha release comes from the plugin's Dockerfile."""
    assert cli_release(DOCKERFILE) == "5.6.0"
    with pytest.raises(DockerError, match="names no ha release"):
        cli_release("ARG CLI_VERSION=../../evil\n")


async def test_build_and_link(native: NativeCli, tmp_path: Path) -> None:
    """Test a version builds with Go like upstream and the ha command runs it."""
    calls: list = []
    _downloads(native, _answers())
    with (
        patch.object(native_cli, "_run", side_effect=_fake_build(calls)),
        patch.object(native_cli.shutil, "which", return_value="/opt/homebrew/bin/go"),
    ):
        await native.install(VERSION)
    assert native.version == VERSION
    ((args, cwd, env),) = calls
    assert args[:4] == ("/opt/homebrew/bin/go", "build", "-trimpath", "-ldflags=-s -w")
    assert env["CGO_ENABLED"] == "0"
    assert (cwd / "go.mod").is_file() is False  # scratch folder is gone afterwards
    built = tmp_path / "data" / "cli" / "versions" / str(VERSION)
    marker = await asyncio.to_thread((built / ".installed").read_text)
    assert json.loads(marker) == {"release": "5.6.0"}
    assert await native.exists()
    assert not await native.is_running()  # built, not linked yet

    await asyncio.to_thread(
        (tmp_path / "data" / "cli.json").write_text, json.dumps({"access_token": "t0k"})
    )
    await native.run()
    link = tmp_path / "bin" / "ha"
    assert await asyncio.to_thread(link.readlink) == native.path_command
    assert await native.is_running()
    assert await native.current_state() == ContainerState.RUNNING

    out = (await asyncio.to_thread(_sh, str(link), "core", "info")).stdout
    assert out.strip() == "token=t0k endpoint=127.0.0.1:4358 args=core info"


async def test_command_without_token(native: NativeCli, tmp_path: Path) -> None:
    """Test the ha command explains a missing token instead of failing oddly."""
    _downloads(native, _answers())
    with (
        patch.object(native_cli, "_run", side_effect=_fake_build([])),
        patch.object(native_cli.shutil, "which", return_value="/usr/bin/go"),
    ):
        await native.install(VERSION)
    await native.run()
    result = await asyncio.to_thread(_sh, str(native.path_command), "info")
    assert result.returncode == 1
    assert "hasn't started its CLI plugin" in result.stderr


async def test_link_never_replaces_a_foreign_ha(
    native: NativeCli, tmp_path: Path
) -> None:
    """Test an existing ha in ~/.local/bin is left alone."""
    foreign = tmp_path / "bin" / "ha"
    await asyncio.to_thread(foreign.write_text, "#!/bin/sh\necho mine\n")
    _downloads(native, _answers())
    with (
        patch.object(native_cli, "_run", side_effect=_fake_build([])),
        patch.object(native_cli.shutil, "which", return_value="/usr/bin/go"),
    ):
        await native.install(VERSION)
    await native.run()
    assert await asyncio.to_thread(foreign.read_text) == "#!/bin/sh\necho mine\n"
    assert native.path_command.is_file()


async def test_build_errors(native: NativeCli) -> None:
    """Test missing Go, odd versions and failed builds are clear errors."""
    with (
        patch.object(native_cli.shutil, "which", return_value=None),
        pytest.raises(DockerError, match="needs Go"),
    ):
        await native.install(VERSION)
    with pytest.raises(DockerError, match="Unexpected CLI plugin version"):
        await native.install(AwesomeVersion("../x"))

    _downloads(native, _answers())

    async def fail(*args, **kwargs) -> CommandReturn:
        return CommandReturn(1, ["compile error\n"])

    with (
        patch.object(native_cli, "_run", side_effect=fail),
        patch.object(native_cli.shutil, "which", return_value="/usr/bin/go"),
        pytest.raises(DockerError, match="Can't build ha 5.6.0"),
    ):
        await native.install(VERSION)
    assert not await native.exists()


async def test_unexpected_source_layout(native: NativeCli) -> None:
    """Test a source archive without one go module folder is refused."""
    answers = _answers()
    answers[native_cli.CLI_SOURCE.format(release="5.6.0")] = _source(top="")
    _downloads(native, answers)
    with (
        patch.object(native_cli.shutil, "which", return_value="/usr/bin/go"),
        pytest.raises(DockerError, match="unexpected layout"),
    ):
        await native.install(VERSION)


async def test_versions_attach_and_cleanup(native: NativeCli, tmp_path: Path) -> None:
    """Test the newest build is found, attach needs a build, cleanup keeps one."""
    with pytest.raises(DockerError, match="No ha build"):
        await native.get_latest_version()

    def make(version: str) -> None:
        folder = tmp_path / "data" / "cli" / "versions" / version
        folder.mkdir(parents=True)
        (folder / ".installed").write_text('{"release": "5.5.0"}')

    for version in ("2026.9.0", "2026.10.1"):
        await asyncio.to_thread(make, version)
    assert await native.get_latest_version() == AwesomeVersion("2026.10.1")
    with pytest.raises(DockerError, match="isn't built"):
        await native.attach(AwesomeVersion("2026.11.0"))
    await native.attach(AwesomeVersion("2026.10.1"))
    await native.cleanup()
    left = await asyncio.to_thread(
        lambda: [p.name for p in (tmp_path / "data" / "cli" / "versions").iterdir()]
    )
    assert left == ["2026.10.1"]
    assert (await native.stats()).memory_usage == 0


async def test_plugin_load_builds_in_the_background() -> None:
    """Test startup never waits for a first native build."""
    coresys = MagicMock()
    plugin = PluginCli(coresys)
    instance = MagicMock()
    instance.exists = AsyncMock(return_value=False)
    instance.get_latest_version = AsyncMock(side_effect=DockerError("none"))
    plugin.instance = instance
    with (
        patch.object(plugin_cli, "IS_MACOS", True),
        patch.object(plugin_cli.PluginBase, "load", new=AsyncMock()) as base_load,
        patch.object(PluginCli, "enabled", new=True),
    ):
        await plugin.load()
    base_load.assert_not_awaited()
    coresys.create_task.assert_called_once()
    coresys.create_task.call_args.args[0].close()


async def test_plugin_load_attaches_an_existing_build() -> None:
    """Test a built CLI loads the usual way."""
    coresys = MagicMock()
    plugin = PluginCli(coresys)
    instance = MagicMock()
    instance.exists = AsyncMock(return_value=True)
    plugin.instance = instance
    with (
        patch.object(plugin_cli, "IS_MACOS", True),
        patch.object(plugin_cli.PluginBase, "load", new=AsyncMock()) as base_load,
        patch.object(PluginCli, "enabled", new=True),
    ):
        await plugin.load()
    base_load.assert_awaited_once()
    coresys.create_task.assert_not_called()


class _Content:
    def __init__(self, chunks: list[bytes]) -> None:
        self._chunks = chunks

    async def iter_chunked(self, size: int):
        for chunk in self._chunks:
            yield chunk


class _Response:
    def __init__(self, status: int, chunks: list[bytes]) -> None:
        self.status = status
        self.content = _Content(chunks)

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args) -> None:
        return None


async def test_download_reads_every_chunk(native: NativeCli) -> None:
    """Test a body arriving in pieces is read whole, and size and errors checked."""
    native.coresys.websession.get = MagicMock(
        return_value=_Response(200, [b"ab", b"cd", b"ef"])
    )
    assert await native._download("https://example/x") == b"abcdef"
    native.coresys.websession.get = MagicMock(return_value=_Response(404, []))
    with pytest.raises(DockerError, match="HTTP 404"):
        await native._download("https://example/x")
    with patch.object(native_cli, "_MAX_SOURCE_BYTES", 3):
        native.coresys.websession.get = MagicMock(
            return_value=_Response(200, [b"ab", b"cd"])
        )
        with pytest.raises(DockerError, match="larger than expected"):
            await native._download("https://example/x")


async def test_truncated_source_is_a_build_error(native: NativeCli) -> None:
    """Test a cut-off tarball is a clear error, not an EOFError."""
    answers = _answers()
    source = native_cli.CLI_SOURCE.format(release="5.6.0")
    answers[source] = answers[source][:40]
    _downloads(native, answers)
    with (
        patch.object(native_cli.shutil, "which", return_value="/usr/bin/go"),
        pytest.raises(DockerError, match="Can't unpack the ha source"),
    ):
        await native.install(VERSION)


async def test_overlapping_installs_build_once(native: NativeCli) -> None:
    """Test startup, repair and update installing at once share one build."""
    calls: list = []
    _downloads(native, _answers())
    with (
        patch.object(native_cli, "_run", side_effect=_fake_build(calls)),
        patch.object(native_cli.shutil, "which", return_value="/usr/bin/go"),
    ):
        await asyncio.gather(native.install(VERSION), native.install(VERSION))
    assert len(calls) == 1
    assert await native.exists()


async def test_failed_build_keeps_what_was_there(
    native: NativeCli, tmp_path: Path
) -> None:
    """Test a failing build never removes the version's folder."""
    target = tmp_path / "data" / "cli" / "versions" / str(VERSION)
    await asyncio.to_thread(target.mkdir, parents=True)
    await asyncio.to_thread((target / "ha").write_text, "previous")
    _downloads(native, _answers())

    async def fail(*args, **kwargs) -> CommandReturn:
        return CommandReturn(1, ["boom\n"])

    with (
        patch.object(native_cli, "_run", side_effect=fail),
        patch.object(native_cli.shutil, "which", return_value="/usr/bin/go"),
        pytest.raises(DockerError, match="Can't build"),
    ):
        await native.install(VERSION)
    assert await asyncio.to_thread((target / "ha").read_text) == "previous"
    leftovers = await asyncio.to_thread(
        lambda: sorted(p.name for p in target.parent.iterdir())
    )
    assert leftovers == [str(VERSION)]


async def test_hung_build_times_out(native: NativeCli) -> None:
    """Test a go build that never ends is stopped and reported."""
    _downloads(native, _answers())

    async def hang(*args, **kwargs) -> CommandReturn:
        await asyncio.sleep(10)
        return CommandReturn(0, [])

    with (
        patch.object(native_cli, "_run", side_effect=hang),
        patch.object(native_cli, "_BUILD_TIMEOUT", 0.05),
        patch.object(native_cli.shutil, "which", return_value="/usr/bin/go"),
        pytest.raises(DockerError, match="took too long"),
    ):
        await native.install(VERSION)


async def test_file_errors_are_docker_errors(native: NativeCli, tmp_path: Path) -> None:
    """Test a failing link step is a DockerError the plugin handles."""
    _downloads(native, _answers())
    with (
        patch.object(native_cli, "_run", side_effect=_fake_build([])),
        patch.object(native_cli.shutil, "which", return_value="/usr/bin/go"),
    ):
        await native.install(VERSION)
    with (
        patch.object(
            NativeCli, "_write_command", side_effect=PermissionError("read-only")
        ),
        pytest.raises(DockerError, match="Can't write the ha command"),
    ):
        await native.run()


async def test_background_build_retries_after_any_error() -> None:
    """Test the first native build keeps trying after unexpected errors."""
    coresys = MagicMock()
    plugin = PluginCli(coresys)
    instance = MagicMock()
    instance.exists = AsyncMock(return_value=False)
    plugin.instance = instance
    plugin._install = AsyncMock(side_effect=[RuntimeError("odd"), None])
    plugin.start = AsyncMock()
    with patch.object(plugin_cli, "NATIVE_BUILD_RETRY_SECONDS", 0):
        await plugin._build_natively()
    assert plugin._install.await_count == 2
    plugin.start.assert_awaited_once()
