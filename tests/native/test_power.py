"""Test the native power assertion that keeps the Mac awake."""

from unittest.mock import MagicMock

import pytest

from supervisor.native import power


@pytest.fixture(autouse=True)
def reset_assertion():
    """Each test starts without a held assertion."""
    power._assertion_id = None  # pylint: disable=protected-access
    yield
    power._assertion_id = None  # pylint: disable=protected-access


def _fake(result: int = 0, assertion_id: int = 7, cfstring: int | None = 1):
    iokit, corefoundation = MagicMock(), MagicMock()
    corefoundation.CFStringCreateWithCString.return_value = cfstring

    def create(kind, level, name, out):
        out.contents.value = assertion_id
        return result

    iokit.IOPMAssertionCreateWithName.side_effect = create
    return iokit, corefoundation


def test_holds_prevent_idle_sleep() -> None:
    """The assertion blocks idle sleep at full level and frees its strings."""
    iokit, corefoundation = _fake()
    assert power.hold_awake((iokit, corefoundation)) == 7
    kinds = [c.args[1] for c in corefoundation.CFStringCreateWithCString.call_args_list]
    assert kinds == [b"PreventUserIdleSystemSleep", power.ASSERTION_NAME.encode()]
    assert iokit.IOPMAssertionCreateWithName.call_args.args[1] == 255
    assert corefoundation.CFRelease.call_count == 2


def test_second_call_keeps_the_first() -> None:
    """A second call doesn't stack another assertion."""
    iokit, corefoundation = _fake()
    power.hold_awake((iokit, corefoundation))
    assert power.hold_awake((iokit, corefoundation)) == 7
    assert iokit.IOPMAssertionCreateWithName.call_count == 1


@pytest.mark.parametrize(
    ("result", "cfstring"),
    [
        pytest.param(0xE00002C2, 1, id="iokit-error"),
        pytest.param(0, None, id="no-cfstring"),
    ],
)
def test_failure_is_logged_not_raised(
    result: int, cfstring: int | None, caplog: pytest.LogCaptureFixture
) -> None:
    """A failure never stops the Supervisor and is logged as an error."""
    iokit, corefoundation = _fake(result=result, cfstring=cfstring)
    assert power.hold_awake((iokit, corefoundation)) is None
    assert "Can't keep the Mac awake" in caplog.text


def test_missing_frameworks_are_logged(caplog: pytest.LogCaptureFixture) -> None:
    """Even loading the frameworks failing is only logged."""
    broken = MagicMock()
    broken.CFStringCreateWithCString.side_effect = OSError("no CoreFoundation")
    assert power.hold_awake((MagicMock(), broken)) is None
    assert "Can't keep the Mac awake" in caplog.text
