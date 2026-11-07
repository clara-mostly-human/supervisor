"""Test ingress for native apps."""

from unittest.mock import MagicMock

import pytest

from supervisor.apps.app import App
from supervisor.const import ATTR_INGRESS_PORT


def _app(ports: dict[str, int | None] | None) -> MagicMock:
    app = MagicMock()
    app.with_ingress = True
    app.slug = "core_matter_server"
    app.data = {ATTR_INGRESS_PORT: 5580}
    app.ports = ports
    return app


@pytest.mark.parametrize(
    ("ports", "expected"),
    [
        pytest.param({"5580/tcp": 5580}, 5580, id="default-mapping"),
        pytest.param({"5580/tcp": 5590}, 5590, id="remapped"),
        pytest.param({"5580/tcp": None}, 5580, id="not-published"),
        pytest.param({"8081/tcp": 8081}, 5580, id="ingress-port-not-a-port"),
        pytest.param(None, 5580, id="no-ports"),
    ],
)
def test_ingress_reaches_the_port_the_app_listens_on(ports, expected):
    """Test ingress follows a remapped port, since natively that's where it listens."""
    assert App.ingress_port.fget(_app(ports)) == expected


def test_no_ingress_no_port():
    """Test an app without ingress has no ingress port."""
    app = _app({"5580/tcp": 5590})
    app.with_ingress = False
    assert App.ingress_port.fget(app) is None
