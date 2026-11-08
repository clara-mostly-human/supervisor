"""Test the host network read from macOS (preferences, scutil and ifconfig)."""

import asyncio
import copy
from ipaddress import IPv4Address, IPv4Interface, IPv6Interface
import json
from pathlib import Path
import plistlib
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from supervisor.api.network import APINetwork
from supervisor.exceptions import HostNetworkNotFound
from supervisor.host.const import AuthMethod, InterfaceMethod, InterfaceType
from supervisor.native import network
from supervisor.native.network import (
    HIDDEN_SSID,
    MacOSOwnsNetworkError,
    NativeNetworkManager,
    configured_services,
    parse_ifconfig,
    parse_scutil,
    read_interfaces,
    wifi_auth,
)

# Verbatim from this Mac (Router hardware address and hashes trimmed)
SCUTIL_TEXT = """<dictionary> {
  Addresses : <array> {
    0 : 192.0.2.136
  }
  AdditionalRoutes : <array> {
    0 : <dictionary> {
      DestinationAddress : 192.0.2.136
      SubnetMask : 255.255.255.255
    }
  }
  ConfirmedInterfaceName : en1
  Router : 192.0.2.1
  SubnetMasks : <array> {
    0 : 255.255.255.0
  }
}
  No such key
<dictionary> {
  BluetoothPagingInProgress  : FALSE
  Power Status : TRUE
  SSID : <data> 0x00
  SSID_STR :
}
"""

IFCONFIG = """lo0: flags=8049<UP,LOOPBACK,RUNNING,MULTICAST> mtu 16384
\tinet 127.0.0.1 netmask 0xff000000
\tinet6 ::1 prefixlen 128
en0: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\toptions=50b<RXCSUM,TXCSUM,VLAN_HWTAGGING,AV,CHANNEL_IO>
\tether 02:00:00:00:00:01
\tmedia: autoselect (none)
\tstatus: inactive
en1: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 02:00:00:00:00:02
\tinet6 fe80::1871:4c46:6549:f266%en1 prefixlen 64 secured scopeid 0xf
\tinet 192.0.2.136 netmask 0xffffff00 broadcast 192.0.2.255
\tinet6 fd7b:c57e:2e33:1b61:c12:4908:f779:85da prefixlen 64 autoconf secured
\tinet6 fd00::99 prefixlen 64 tentative
\tnd6 options=201<PERFORMNUD,DAD>
\tmedia: autoselect
\tstatus: active
en9: flags=8863<UP,BROADCAST,SMART,RUNNING,SIMPLEX,MULTICAST> mtu 1500
\tether 02:00:00:00:00:03
\tinet 10.0.0.5 netmask 0xffffff00 broadcast 10.0.0.255
\tstatus: active
"""


def _render(value: Any, indent: int = 0) -> str:
    """Render a value the way scutil prints it."""
    pad = "  " * (indent + 1)
    if isinstance(value, dict):
        items = value.items()
        kind = "<dictionary> {"
    else:
        items = enumerate(value)
        kind = "<array> {"
    lines = [kind]
    for key, item in items:
        if isinstance(item, (dict, list)):
            lines.append(f"{pad}{key} : {_render(item, indent + 1)}")
        elif isinstance(item, bool):
            lines.append(f"{pad}{key} : {'TRUE' if item else 'FALSE'}")
        elif isinstance(item, bytes):
            lines.append(f"{pad}{key} : <data> 0x{item.hex()}")
        else:
            lines.append(f"{pad}{key} : {item}")
    lines.append("  " * indent + "}")
    return "\n".join(lines)


def _archive(fields: dict[str, Any]) -> bytes:
    """Return an NSKeyedArchiver plist holding fields, like CachedScanRecord."""
    objects: list[Any] = ["$null"]

    def add(item: Any) -> plistlib.UID:
        if isinstance(item, (dict, list)):
            entry: dict[str, Any] = {}
            objects.append(entry)
            index = len(objects) - 1
            if isinstance(item, dict):
                entry["NS.keys"] = [add(key) for key in item]
                entry["NS.objects"] = [add(value) for value in item.values()]
            else:
                entry["NS.objects"] = [add(value) for value in item]
            return plistlib.UID(index)
        objects.append(item)
        return plistlib.UID(len(objects) - 1)

    root = add(fields)
    return plistlib.dumps(
        {
            "$archiver": "NSKeyedArchiver",
            "$version": 100000,
            "$objects": objects,
            "$top": {"root": root},
        },
        fmt=plistlib.FMT_BINARY,
    )


RSN_RECORD = _archive(
    {
        "CAPABILITIES": 5169,
        "RSN_IE": {"IE_KEY_RSN_AUTHSELS": [2, 8], "IE_KEY_RSN_MCIPHER": 4},
        "SSID_STR": "not-used",
    }
)

SET = "00000000-0000-4000-8000-000000000001"
WIFI = "00000000-0000-4000-8000-000000000002"
ETH = "00000000-0000-4000-8000-000000000003"
BRIDGE = "00000000-0000-4000-8000-000000000004"
HIDDEN = "00000000-0000-4000-8000-000000000005"
VPN = "00000000-0000-4000-8000-000000000006"
USB = "11111111-2222-3333-4444-555555555555"


def _iface(device: str, hardware: str = "Ethernet", **extra: Any) -> dict[str, Any]:
    return {"DeviceName": device, "Hardware": hardware, "Type": "Ethernet", **extra}


# This Mac's preferences, plus a statically configured USB adapter
PREFERENCES: dict[str, Any] = {
    "CurrentSet": f"/Sets/{SET}",
    "NetworkServices": {
        ETH: {
            "UserDefinedName": "Ethernet",
            "Interface": _iface("en0"),
            "IPv4": {"ConfigMethod": "DHCP"},
            "IPv6": {"ConfigMethod": "Automatic"},
        },
        HIDDEN: {"Interface": _iface("en5", HiddenConfiguration=True)},
        BRIDGE: {"Interface": _iface("bridge0")},
        WIFI: {
            "UserDefinedName": "Wi-Fi",
            "Interface": _iface("en1", "AirPort"),
            "IPv4": {"ConfigMethod": "DHCP"},
            "IPv6": {"ConfigMethod": "Automatic"},
        },
        VPN: {
            "Interface": {"SubType": "io.tailscale.ipn.macsys", "Type": "VPN"},
            "DNS": {"__INACTIVE__": True},
        },
        USB: {
            "UserDefinedName": "USB LAN",
            "Interface": _iface("en9"),
            "IPv4": {
                "ConfigMethod": "Manual",
                "Addresses": ["10.0.0.5"],
                "SubnetMasks": ["255.255.255.0"],
                "Router": "10.0.0.1",
            },
            "IPv6": {"ConfigMethod": "LinkLocal"},
            "DNS": {"ServerAddresses": ["10.0.0.53"]},
        },
    },
    "Sets": {
        SET: {
            "Network": {
                "Global": {"IPv4": {"ServiceOrder": [ETH, HIDDEN, BRIDGE, WIFI, VPN]}},
                "Service": {
                    sid: {"__LINK__": f"/NetworkServices/{sid}"}
                    for sid in (ETH, HIDDEN, BRIDGE, WIFI, VPN, USB)
                },
            }
        }
    },
}

STATE: dict[str, Any] = {
    "State:/Network/Global/IPv4": {
        "PrimaryInterface": "en1",
        "PrimaryService": WIFI,
        "Router": "192.0.2.1",
    },
    f"State:/Network/Service/{WIFI}/IPv4": {
        "Addresses": ["192.0.2.136"],
        "Router": "192.0.2.1",
        "SubnetMasks": ["255.255.255.0"],
    },
    f"State:/Network/Service/{WIFI}/IPv6": {
        "Addresses": ["fd7b:c57e:2e33:1b61:c12:4908:f779:85da"],
        "PrefixLength": ["64"],
    },
    f"State:/Network/Service/{WIFI}/DNS": {
        "DomainName": "localdomain",
        "ServerAddresses": ["192.0.2.1", "fd00::1"],
    },
    f"State:/Network/Service/{USB}/IPv4": {
        "Addresses": ["10.0.0.5"],
        "Router": "10.0.0.1",
        "SubnetMasks": ["255.255.255.0"],
    },
    f"State:/Network/Service/{USB}/DNS": {"ServerAddresses": ["10.0.0.99"]},
    "State:/Network/Interface/en1/AirPort": {
        "Power Status": True,
        "SSID": b"\x00",
        "SSID_STR": "",
        "CHANNEL": "37",
        "CachedScanRecord": RSN_RECORD,
    },
}


def _fake_run(state: dict[str, Any], ifconfig: str | None = IFCONFIG, calls=None):
    """Return a fake _run answering scutil from state and ifconfig from text."""

    def run(args: list[str], stdin: str | None = None) -> str | None:
        if calls is not None:
            calls.append((args[0], stdin))
        if args[0] == network._IFCONFIG:
            return ifconfig
        assert args == [network._SCUTIL]
        out = []
        for line in (stdin or "").splitlines():
            command, _, key = line.partition(" ")
            assert command == "show", "only reads"
            out.append(_render(state[key]) if key in state else "  No such key")
        return "\n".join(out) + "\n"

    return run


def _patched(
    preferences: dict[str, Any] | None = None,
    state: dict[str, Any] | None = None,
    ifconfig: str | None = IFCONFIG,
    calls=None,
):
    chosen = PREFERENCES if preferences is None else preferences
    return (
        patch.object(network, "_read_preferences", return_value=copy.deepcopy(chosen)),
        patch.object(
            network,
            "_run",
            side_effect=_fake_run(STATE if state is None else state, ifconfig, calls),
        ),
    )


def _read(**kwargs: Any) -> dict[str, tuple[Any, bool]]:
    prefs, run = _patched(**kwargs)
    with prefs, run:
        result = read_interfaces()
    assert result is not None
    return {inet.name: (inet, enabled) for inet, enabled in result}


def test_parse_scutil_this_macs_text() -> None:
    """Test scutil's nested answers, missing keys, data and booleans."""
    first, missing, airport = parse_scutil(SCUTIL_TEXT)
    assert first == {
        "Addresses": ["192.0.2.136"],
        "AdditionalRoutes": [
            {"DestinationAddress": "192.0.2.136", "SubnetMask": "255.255.255.255"}
        ],
        "ConfirmedInterfaceName": "en1",
        "Router": "192.0.2.1",
        "SubnetMasks": ["255.255.255.0"],
    }
    assert missing is None
    assert airport == {
        "BluetoothPagingInProgress": False,
        "Power Status": True,
        "SSID": b"\x00",
        "SSID_STR": "",
    }


@pytest.mark.parametrize(
    "text",
    [
        pytest.param("<dictionary> {\n  Router : 192.0.2.1\n", id="cut-off"),
        pytest.param(
            "<dictionary> {\n  DomainName : a\n}\n  Router : 1.2.3.4\n}\n",
            id="closed-early",
        ),
        pytest.param("garbage\n", id="garbage"),
    ],
)
def test_parse_scutil_rejects_misaligned_output(text: str) -> None:
    """Test output that could shift answers onto the wrong keys is an error."""
    with pytest.raises(ValueError, match="scutil"):
        parse_scutil(text)


def test_parse_ifconfig() -> None:
    """Test MAC, link state and addresses; tentative addresses are left out."""
    links = parse_ifconfig(IFCONFIG)
    assert links["en0"].mac == "02:00:00:00:00:01"
    assert links["en0"].active is False
    assert links["en0"].ipv4 == []
    en1 = links["en1"]
    assert en1.up is True
    assert en1.active is True
    assert en1.ipv4 == [IPv4Interface("192.0.2.136/24")]
    assert en1.ipv6 == [
        IPv6Interface("fe80::1871:4c46:6549:f266/64"),
        IPv6Interface("fd7b:c57e:2e33:1b61:c12:4908:f779:85da/64"),
    ]
    assert links["lo0"].ipv4 == [IPv4Interface("127.0.0.1/8")]


@pytest.mark.parametrize(
    ("record", "auth"),
    [
        pytest.param(RSN_RECORD, AuthMethod.WPA_PSK, id="wpa2-wpa3-transition"),
        pytest.param(
            _archive({"CAPABILITIES": 0x1431, "RSN_IE": {"IE_KEY_RSN_AUTHSELS": [8]}}),
            AuthMethod.UNSUPPORTED,
            id="wpa3-only",
        ),
        pytest.param(
            _archive({"CAPABILITIES": 0x11, "WPA_IE": {"IE_KEY_WPA_AUTHSELS": [2]}}),
            AuthMethod.WPA_PSK,
            id="wpa1",
        ),
        pytest.param(_archive({"CAPABILITIES": 0x11}), AuthMethod.WEP, id="wep"),
        pytest.param(_archive({"CAPABILITIES": 0x01}), AuthMethod.OPEN, id="open"),
        pytest.param(
            _archive({"RSN_IE": {"IE_KEY_RSN_AUTHSELS": [[2], {"a": 1}]}}),
            AuthMethod.UNSUPPORTED,
            id="odd-suites",
        ),
        pytest.param(
            b"<?xml version='1.0'?><plist><dict>",
            AuthMethod.UNSUPPORTED,
            id="cut-xml",
        ),
        pytest.param(b"not a plist", AuthMethod.UNSUPPORTED, id="garbage"),
        pytest.param(None, AuthMethod.UNSUPPORTED, id="missing"),
    ],
)
def test_wifi_auth(record: Any, auth: AuthMethod) -> None:
    """Test the security comes from the cached record; bad records are unknown."""
    assert wifi_auth(record) == auth


def test_services_order_and_filter() -> None:
    """Test Ethernet and Wi-Fi services in System Settings order, others left out."""
    services = configured_services(copy.deepcopy(PREFERENCES))
    assert services is not None
    # The USB adapter isn't in ServiceOrder yet: after the ordered ones
    assert [service.device for service in services] == ["en0", "en1", "en9"]


def test_services_without_order_or_set() -> None:
    """Test a missing order still lists services; a missing set is unknown."""
    preferences = copy.deepcopy(PREFERENCES)
    del preferences["Sets"][SET]["Network"]["Global"]
    services = configured_services(preferences)
    assert services is not None
    assert {service.device for service in services} == {"en0", "en1", "en9"}
    assert configured_services({"NetworkServices": {}}) is None


def test_two_services_on_one_device() -> None:
    """Test the first service in order wins a device."""
    preferences = copy.deepcopy(PREFERENCES)
    second = "99999999-0000-0000-0000-000000000000"
    preferences["NetworkServices"][second] = {
        "Interface": _iface("en1", "AirPort"),
        "IPv4": {"ConfigMethod": "Manual"},
    }
    preferences["Sets"][SET]["Network"]["Service"][second] = {}
    services = configured_services(preferences)
    assert services is not None
    assert [s.id for s in services if s.device == "en1"] == [WIFI]


def test_read_interfaces_this_mac() -> None:
    """Test this Mac's interfaces come out as the Linux path fills them."""
    found = _read()
    assert list(found) == ["en0", "en1", "en9"]

    en0, enabled = found["en0"]
    assert enabled is True
    assert en0.type == InterfaceType.ETHERNET
    assert (en0.enabled, en0.connected, en0.primary) == (False, False, False)
    assert en0.mac == "02:00:00:00:00:01"
    assert en0.ipv4setting.method == InterfaceMethod.AUTO
    assert en0.ipv4.ready is False
    assert en0.wifi is None

    en1, _ = found["en1"]
    assert en1.type == InterfaceType.WIRELESS
    assert (en1.enabled, en1.connected, en1.primary) == (True, True, True)
    assert en1.mac == "02:00:00:00:00:02"
    assert en1.ipv4.address == [IPv4Interface("192.0.2.136/24")]
    assert en1.ipv4.gateway == IPv4Address("192.0.2.1")
    assert en1.ipv4.nameservers == [IPv4Address("192.0.2.1")]
    assert [str(ns) for ns in en1.ipv6.nameservers] == ["fd00::1"]
    assert en1.ipv4.ready is True
    assert en1.ipv4setting.method == InterfaceMethod.AUTO
    assert en1.ipv4setting.address == []
    assert en1.ipv6setting.method == InterfaceMethod.AUTO
    assert len(en1.ipv6.address) == 2
    assert en1.wifi is not None
    assert en1.wifi.ssid == HIDDEN_SSID.encode()
    # Not a real network name, so not reported as the connected one
    assert en1.wifi.active_ssid is None
    assert en1.wifi.auth == AuthMethod.WPA_PSK
    assert en1.wifi.signal is None


def test_static_service() -> None:
    """Test a manual service reports its settings; its own DNS is in effect."""
    en9, _ = _read()["en9"]
    assert en9.connected is True
    assert en9.ipv4setting.method == InterfaceMethod.STATIC
    assert en9.ipv4setting.address == [IPv4Interface("10.0.0.5/24")]
    assert en9.ipv4setting.gateway == IPv4Address("10.0.0.1")
    assert en9.ipv4setting.nameservers == [IPv4Address("10.0.0.53")]
    assert en9.ipv4.nameservers == [IPv4Address("10.0.0.53")]
    # Link-local only, like NetworkManager's link-local
    assert en9.ipv6setting.method == InterfaceMethod.DISABLED


def test_disabled_service() -> None:
    """Test a service switched off in System Settings is listed as disabled."""
    preferences = copy.deepcopy(PREFERENCES)
    preferences["NetworkServices"][USB]["__INACTIVE__"] = True
    en9, enabled = _read(preferences=preferences)["en9"]
    assert enabled is False
    assert (en9.enabled, en9.connected) == (False, False)
    assert en9.ipv4setting.method == InterfaceMethod.DISABLED
    assert en9.ipv4.ready is False


def test_switched_off_dns_is_ignored() -> None:
    """Test DNS settings marked inactive don't count."""
    preferences = copy.deepcopy(PREFERENCES)
    preferences["NetworkServices"][USB]["DNS"]["__INACTIVE__"] = True
    en9, _ = _read(preferences=preferences)["en9"]
    assert en9.ipv4setting.nameservers == []
    assert en9.ipv4.nameservers == [IPv4Address("10.0.0.99")]


def test_wifi_name_when_macos_gives_it() -> None:
    """Test the real SSID is used when macOS doesn't redact it."""
    state = dict(STATE)
    state["State:/Network/Interface/en1/AirPort"] = {
        "Power Status": True,
        "SSID": b"home\xff",
        "SSID_STR": "home?",
    }
    en1, _ = _read(state=state)["en1"]
    assert en1.wifi is not None
    assert en1.wifi.ssid == b"home\xff"
    assert en1.wifi.active_ssid == b"home\xff"
    assert en1.wifi.auth == AuthMethod.UNSUPPORTED


def test_wifi_off() -> None:
    """Test Wi-Fi switched off has no Wi-Fi config."""
    state = dict(STATE)
    state["State:/Network/Interface/en1/AirPort"] = {"Power Status": False}
    ifconfig = IFCONFIG.replace("status: active\nen9", "status: inactive\nen9")
    en1, _ = _read(state=state, ifconfig=ifconfig)["en1"]
    assert en1.wifi is None
    assert en1.connected is False


@pytest.mark.parametrize(
    "failure",
    [
        pytest.param({"preferences": {}}, id="no-current-set"),
        pytest.param({"ifconfig": None}, id="ifconfig"),
    ],
)
def test_partial_failure_is_none(failure: dict[str, Any]) -> None:
    """Test any failed source returns None so the manager keeps what it had."""
    prefs, run = _patched(**failure)
    with prefs, run:
        assert read_interfaces() is None


def test_scutil_failure_is_none() -> None:
    """Test a failed scutil returns None."""
    prefs, _ = _patched()
    with prefs, patch.object(network, "_run", return_value=None):
        assert read_interfaces() is None


def test_unreadable_preferences_is_none(tmp_path: Path) -> None:
    """Test a missing or broken preferences file returns None."""
    with patch.object(network, "_PREFERENCES", tmp_path / "missing.plist"):
        assert read_interfaces() is None
    broken = tmp_path / "broken.plist"
    broken.write_bytes(b"<?xml version='1.0'?><plist><dict>")
    with patch.object(network, "_PREFERENCES", broken):
        assert read_interfaces() is None


def test_reads_never_write_or_scan() -> None:
    """Test every scutil command is a show of live state and nothing else runs."""
    calls: list[tuple[str, str | None]] = []
    prefs, run = _patched(calls=calls)
    with prefs, run:
        read_interfaces()
    assert {program for program, _ in calls} == {network._SCUTIL, network._IFCONFIG}
    for program, stdin in calls:
        if program == network._SCUTIL:
            assert stdin
            assert all(line.startswith("show State:") for line in stdin.splitlines())


def test_this_mac_answers() -> None:
    """Test the real sources answer on the host running the tests."""
    assert read_interfaces() is not None


def _manager() -> NativeNetworkManager:
    loop = asyncio.get_running_loop()
    coresys = MagicMock()
    coresys.run_in_executor = lambda func, *args: loop.run_in_executor(
        None, func, *args
    )
    return NativeNetworkManager(coresys)


async def _loaded() -> NativeNetworkManager:
    manager = _manager()
    prefs, run = _patched()
    with prefs, run:
        await manager.load()
    return manager


async def test_manager_serves_interfaces() -> None:
    """Test the manager reads on update and answers lookups."""
    manager = await _loaded()
    assert [inet.name for inet in manager.interfaces] == ["en0", "en1", "en9"]
    assert manager.get("en1").primary is True
    resolved = await manager.get_with_config("en0")
    assert (resolved.has_profile, resolved.enabled) == (True, True)
    assert len(await manager.interfaces_with_config()) == 3
    # Connected interfaces only, the primary's first
    assert manager.dns_servers == ["192.0.2.1", "fd00::1", "10.0.0.53"]
    assert manager.connectivity is None
    with pytest.raises(HostNetworkNotFound):
        manager.get("en7")
    with pytest.raises(HostNetworkNotFound):
        await manager.get_with_config("en7")


async def test_manager_keeps_interfaces_when_a_read_fails() -> None:
    """Test a failed read doesn't empty the page."""
    manager = await _loaded()
    with patch.object(network, "read_interfaces", return_value=None):
        await manager.update()
    assert len(manager.interfaces) == 3


async def test_manager_survives_a_reader_bug() -> None:
    """Test an unexpected error is logged, not raised into Supervisor startup."""
    manager = await _loaded()
    with patch.object(network, "read_interfaces", side_effect=RuntimeError("bug")):
        await manager.load()
    assert len(manager.interfaces) == 3


async def test_manager_reads_one_at_a_time() -> None:
    """Test overlapping refreshes don't run reads side by side."""
    manager = _manager()
    running = 0
    most = 0

    def read() -> list:
        nonlocal running, most
        running += 1
        most = max(most, running)
        try:
            # Long enough for the other refreshes to start meanwhile
            asyncio.run(asyncio.sleep(0.05))
        finally:
            running -= 1
        return []

    with patch.object(network, "read_interfaces", side_effect=read):
        await asyncio.gather(manager.update(), manager.update(), manager.update())
    assert most == 1


async def test_manager_refuses_changes_and_scans() -> None:
    """Test settings changes and Wi-Fi scans are refused without running anything."""
    manager = await _loaded()
    en1 = manager.get("en1")
    with patch.object(network.subprocess, "run", side_effect=AssertionError("ran")):
        with pytest.raises(MacOSOwnsNetworkError, match="System Settings > Network"):
            await manager.apply_changes(en1, update_only=True)
        with pytest.raises(MacOSOwnsNetworkError, match="System Settings > Network"):
            await manager.apply_changes_v2(en1)
        with pytest.raises(MacOSOwnsNetworkError, match="System Settings > Network"):
            await manager.create_vlan(en1)
        with pytest.raises(MacOSOwnsNetworkError, match="System Settings > Network"):
            await manager.resolve_ssid("en1", "caf�")
        with pytest.raises(MacOSOwnsNetworkError, match="Wi-Fi networks is off"):
            await manager.scan_wifi(en1)


def _api(manager: NativeNetworkManager) -> APINetwork:
    api = APINetwork()
    api.coresys = MagicMock()
    api.coresys.host.network = manager
    api.coresys.supervisor.connectivity = True
    api.coresys.docker.network.gateway = "127.0.0.1"
    api.coresys.docker.network.dns = "127.0.0.1"
    return api


async def test_api_info_reads_again() -> None:
    """Test /network/info reads macOS on each call and reports the interfaces."""
    api = _api(_manager())
    calls: list[tuple[str, str | None]] = []
    prefs, run = _patched(calls=calls)
    with prefs, run:
        response = await api.info_v1(MagicMock())
    body = json.loads(response.body)
    assert calls, "read on request"
    interfaces = {inet["interface"]: inet for inet in body["data"]["interfaces"]}
    assert set(interfaces) == {"en0", "en1", "en9"}
    en1 = interfaces["en1"]
    assert en1["type"] == "wireless"
    assert en1["primary"] is True
    assert en1["ipv4"]["method"] == "auto"
    assert en1["ipv4"]["address"] == ["192.0.2.136/24"]
    assert en1["ipv4"]["gateway"] == "192.0.2.1"
    assert en1["wifi"] == {
        "mode": "infrastructure",
        "auth": "wpa-psk",
        "ssid": HIDDEN_SSID,
        "signal": None,
    }
    assert body["data"]["host_internet"] is None


async def test_api_v2_info() -> None:
    """Test the v2 info lists interfaces with their services as profiles."""
    api = _api(_manager())
    prefs, run = _patched()
    with prefs, run:
        response = await api.info(MagicMock())
    body = json.loads(response.body)
    interfaces = {inet["name"]: inet for inet in body["data"]["interfaces"]}
    assert set(interfaces) == {"en0", "en1", "en9"}
    assert interfaces["en1"]["state"]["connected"] is True
    assert interfaces["en1"]["config"] is not None


async def test_api_refusals_are_plain_errors(caplog: pytest.LogCaptureFixture) -> None:
    """Test a refused scan is a 400 with the reason, not an unexpected error."""
    api = _api(await _loaded())
    request = MagicMock()
    request.match_info = {"interface": "en1"}
    response = await api.scan_accesspoints(request)
    assert response.status == 400
    assert "Wi-Fi networks is off" in json.loads(response.body)["message"]
    assert "Unexpected error" not in caplog.text
