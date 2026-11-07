"""Test reading app repository files natively (macOS has no /proc)."""

import os
from pathlib import Path

import pytest

from supervisor.apps.model import _opened_path, read_in_repository
from supervisor.exceptions import StoreRepositoryFileUnsafeError


def test_reads_a_file_in_the_repository(tmp_path: Path) -> None:
    """Test icons, docs and changelogs can be read at all."""
    repository = tmp_path.resolve() / "repo"
    (repository / "app").mkdir(parents=True)
    (repository / "app" / "CHANGELOG.md").write_text("## 1.0")
    assert read_in_repository(repository / "app" / "CHANGELOG.md", repository) == (
        b"## 1.0"
    )


def test_refuses_a_symlink_out_of_the_repository(tmp_path: Path) -> None:
    """Test a link to a file outside (e.g. a token) is still refused."""
    repository = tmp_path.resolve() / "repo"
    repository.mkdir()
    secret = tmp_path / "token"
    secret.write_text("secret")
    (repository / "icon.png").symlink_to(secret)
    with pytest.raises(StoreRepositoryFileUnsafeError):
        read_in_repository(repository / "icon.png", repository)


def test_opened_path_is_the_real_file(tmp_path: Path) -> None:
    """Test the open file reports where it really is, through links."""
    real = tmp_path.resolve() / "real"
    real.mkdir()
    (real / "file").write_text("x")
    (tmp_path / "alias").symlink_to(real)
    fd = os.open(tmp_path / "alias" / "file", os.O_RDONLY)
    try:
        assert _opened_path(fd) == real / "file"
    finally:
        os.close(fd)
