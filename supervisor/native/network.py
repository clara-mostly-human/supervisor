"""Host network on macOS, read from SystemConfiguration.

Linux reads the interfaces from NetworkManager over D-Bus. On macOS the
configuration lives in SystemConfiguration's preferences file, which System
Settings writes and any user may read: the services System Settings lists,
whether each one is switched on, DHCP or manual, its own DNS servers. The live
state comes from configd's store through `scutil`: router and DNS servers from
DHCP, and the joined Wi-Fi network's security from its last association
(macOS hides the network's name from processes without Location Services
permission). Addresses, MAC address and link state come from ifconfig.

Read only, and never a Wi-Fi scan: macOS owns the network settings (System
Settings > Network), and scans from background tools have wedged this Mac's
Wi-Fi link before.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterable
from dataclasses import dataclass, field
from ipaddress import IPv4Address, IPv4Interface, IPv6Address, IPv6Interface, ip_address
import logging
from pathlib import Path
import plistlib
import subprocess
from typing import Any
from xml.parsers.expat import ExpatError

from ..exceptions import APIError, HostNetworkNotFound
from ..host.configuration import (
    AccessPoint,
    Interface,
    Ip6Setting,
    IpConfig,
    IpSetting,
    ResolvedInterface,
    WifiConfig,
)
from ..host.const import (
    AuthMethod,
    InterfaceMethod,
    InterfaceType,
    MulticastDnsMode,
    WifiMode,
)
from ..host.network import NetworkManager

_LOGGER: logging.Logger = logging.getLogger(__name__)

_PREFERENCES = Path("/Library/Preferences/SystemConfiguration/preferences.plist")
_SCUTIL = "/usr/sbin/scutil"
_IFCONFIG = "/sbin/ifconfig"
# Each answers in milliseconds; a hung configd must not hold up startup long
_TIMEOUT = 5

_NO_KEY = "No such key"
_CONTAINERS: dict[str, type] = {"<dictionary> {": dict, "<array> {": list}
_DATA = "<data> 0x"
_INACTIVE = "__INACTIVE__"

# Live state per service, in this order
_STATE_KEYS = (
    "State:/Network/Service/{}/IPv4",
    "State:/Network/Service/{}/IPv6",
    "State:/Network/Service/{}/DNS",
)

_IPV4_METHODS = {
    "DHCP": InterfaceMethod.AUTO,
    "BOOTP": InterfaceMethod.AUTO,
    "Manual": InterfaceMethod.STATIC,
    # A manual address, the rest (router, DNS) from DHCP
    "INFORM": InterfaceMethod.STATIC,
}
# Link-local only maps to disabled, as NetworkManager's link-local does
_IPV6_METHODS = {
    "Automatic": InterfaceMethod.AUTO,
    "RouterAdvertisement": InterfaceMethod.AUTO,
    "Manual": InterfaceMethod.STATIC,
}

# RSN/WPA authentication suites that use a pre-shared key: PSK, FT-PSK, PSK-SHA256
_PSK_SUITES = {2, 4, 6}
# 802.11 capability bit for privacy: WEP when there is no RSN or WPA element
_CAP_PRIVACY = 0x10
_ARCHIVE_DEPTH = 8

SETTINGS_ON_MACOS = (
    "macOS manages this Mac's network: change it in System Settings > Network"
)
HIDDEN_SSID = "Wi-Fi (name hidden by macOS)"
NO_WIFI_SCAN = (
    "Searching for Wi-Fi networks is off on macOS: join networks in "
    "System Settings > Wi-Fi"
)


class MacOSOwnsNetworkError(APIError):
    """A change only macOS's own settings make: refused, not a failure."""


def _value(text: str) -> Any:
    if text.startswith(_DATA):
        try:
            return bytes.fromhex(text.removeprefix(_DATA))
        except ValueError:
            return None
    if text in ("TRUE", "FALSE"):
        return text == "TRUE"
    return text


def _put(container: dict[str, Any] | list[Any], key: str, value: Any) -> None:
    if isinstance(container, dict):
        container[key] = value
    else:
        container.append(value)


def parse_scutil(text: str) -> list[dict[str, Any] | None]:
    """Return each `show` answer of a scutil session, None for a missing key.

    Anything unexpected between answers raises: an answer that closed early
    would otherwise shift the rest onto the wrong keys.
    """
    answers: list[dict[str, Any] | None] = []
    stack: list[dict[str, Any] | list[Any]] = []
    for raw in text.splitlines():
        line = raw.strip()
        if not line:
            continue
        if not stack:
            if line == _NO_KEY:
                answers.append(None)
            elif _CONTAINERS.get(line) is dict:
                answer: dict[str, Any] = {}
                answers.append(answer)
                stack.append(answer)
            else:
                raise ValueError(f"unexpected scutil line {line[:40]!r}")
            continue
        if line == "}":
            stack.pop()
            continue
        if line.endswith(" :"):
            key, value = line[:-2], ""
        else:
            key, _, value = line.partition(" : ")
        key = key.strip()
        if (kind := _CONTAINERS.get(value)) is not None:
            child = kind()
            _put(stack[-1], key, child)
            stack.append(child)
        else:
            _put(stack[-1], key, _value(value))
    if stack:
        raise ValueError("scutil output ended inside an answer")
    return answers


def _run(args: list[str], stdin: str | None = None) -> str | None:
    try:
        return subprocess.run(
            args,
            input=stdin,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            check=True,
            timeout=_TIMEOUT,
        ).stdout
    except (OSError, subprocess.SubprocessError) as err:
        _LOGGER.warning("Can't run %s: %s", args[0], err)
        return None


def _show(keys: list[str]) -> list[dict[str, Any] | None] | None:
    """Return the store's answer for each key, None if scutil failed."""
    if not keys:
        return []
    output = _run([_SCUTIL], "".join(f"show {key}\n" for key in keys))
    if output is None:
        return None
    try:
        answers = parse_scutil(output)
    except ValueError as err:
        _LOGGER.warning("Can't read the network state: %s", err)
        return None
    if len(answers) != len(keys):
        _LOGGER.warning(
            "Can't read the network state: %d answers for %d keys",
            len(answers),
            len(keys),
        )
        return None
    return answers


@dataclass(slots=True)
class Link:
    """What ifconfig reports for one device."""

    up: bool = False
    active: bool = False
    mac: str = ""
    ipv4: list[IPv4Interface] = field(default_factory=list)
    ipv6: list[IPv6Interface] = field(default_factory=list)


def _after(words: list[str], word: str) -> str | None:
    try:
        return words[words.index(word) + 1]
    except ValueError, IndexError:
        return None


def parse_ifconfig(text: str) -> dict[str, Link]:
    """Return the link state and addresses of each device in ifconfig's output."""
    links: dict[str, Link] = {}
    link: Link | None = None
    for line in text.splitlines():
        if line and not line[0].isspace():
            name, _, rest = line.partition(": ")
            flags = rest.partition("<")[2].partition(">")[0].split(",")
            link = links[name] = Link(up="UP" in flags)
            continue
        words = line.split()
        if link is None or not words:
            continue
        try:
            match words[0]:
                case "ether" if len(words) > 1:
                    link.mac = words[1]
                case "status:" if len(words) > 1:
                    link.active = words[1] == "active"
                case "inet" if (mask := _after(words, "netmask")) is not None:
                    prefix = bin(int(mask, 16)).count("1")
                    link.ipv4.append(IPv4Interface(f"{words[1]}/{prefix}"))
                case "inet6" if (prefix6 := _after(words, "prefixlen")) is not None:
                    # Not usable yet, or found to be a duplicate
                    if {"tentative", "duplicated", "detached"} & set(words):
                        continue
                    address = words[1].partition("%")[0]
                    link.ipv6.append(IPv6Interface(f"{address}/{prefix6}"))
        except ValueError:
            continue
    return links


def _addresses(values: Any) -> list[IPv4Address | IPv6Address]:
    found: list[IPv4Address | IPv6Address] = []
    for value in values if isinstance(values, list) else []:
        try:
            found.append(ip_address(str(value).partition("%")[0]))
        except ValueError:
            continue
    return found


def _gateway4(value: Any) -> IPv4Address | None:
    found = [ip for ip in _addresses([value]) if isinstance(ip, IPv4Address)]
    return found[0] if found else None


def _gateway6(value: Any) -> IPv6Address | None:
    found = [ip for ip in _addresses([value]) if isinstance(ip, IPv6Address)]
    return found[0] if found else None


def _static(
    config: dict[str, Any], masks_key: str, version: int
) -> list[IPv4Interface | IPv6Interface]:
    addresses = config.get("Addresses")
    masks = config.get(masks_key)
    if not isinstance(addresses, list) or not isinstance(masks, list):
        return []
    found: list[IPv4Interface | IPv6Interface] = []
    for address, mask in zip(addresses, masks, strict=False):
        try:
            if version == 4:
                found.append(IPv4Interface(f"{address}/{mask}"))
            else:
                found.append(IPv6Interface(f"{address}/{mask}"))
        except ValueError:
            continue
    return found


def _unarchive(record: bytes) -> dict[str, Any]:
    """Return the top object of an NSKeyedArchiver plist as plain values."""
    archive = plistlib.loads(record)
    objects = archive["$objects"]

    def resolve(item: Any, depth: int) -> Any:
        if depth > _ARCHIVE_DEPTH:
            return None
        if isinstance(item, plistlib.UID):
            item = objects[item.data]
        if isinstance(item, dict) and "NS.keys" in item:
            return {
                resolve(key, depth + 1): resolve(value, depth + 1)
                for key, value in zip(item["NS.keys"], item["NS.objects"], strict=False)
            }
        if isinstance(item, dict) and "NS.objects" in item:
            return [resolve(value, depth + 1) for value in item["NS.objects"]]
        return item

    top = resolve(archive["$top"]["root"], 0)
    if not isinstance(top, dict):
        raise TypeError("not a dictionary")
    return top


def _auth_from(fields: dict[str, Any]) -> AuthMethod:
    for element, suites_key in (
        ("RSN_IE", "IE_KEY_RSN_AUTHSELS"),
        ("WPA_IE", "IE_KEY_WPA_AUTHSELS"),
    ):
        info = fields.get(element)
        if isinstance(info, dict):
            suites = info.get(suites_key)
            if isinstance(suites, list) and _PSK_SUITES & {
                suite for suite in suites if isinstance(suite, int)
            }:
                return AuthMethod.WPA_PSK
            # WPA3-only (SAE) or enterprise, as NetworkManager reports them
            return AuthMethod.UNSUPPORTED
    capabilities = fields.get("CAPABILITIES")
    if isinstance(capabilities, int):
        return AuthMethod.WEP if capabilities & _CAP_PRIVACY else AuthMethod.OPEN
    return AuthMethod.UNSUPPORTED


def wifi_auth(record: Any) -> AuthMethod:
    """Return the joined network's security from its cached scan record."""
    if not isinstance(record, bytes):
        return AuthMethod.UNSUPPORTED
    try:
        return _auth_from(_unarchive(record))
    except Exception:  # pylint: disable=broad-except
        # Any malformed record (binary or XML plist) means unknown
        return AuthMethod.UNSUPPORTED


def _wifi(airport: dict[str, Any] | None) -> WifiConfig | None:
    if not airport or airport.get("Power Status") is False:
        return None
    name = airport.get("SSID_STR")
    active: bytes | None
    if isinstance(name, str) and name:
        ssid = airport.get("SSID")
        if not isinstance(ssid, bytes) or not ssid:
            ssid = name.encode()
        active = ssid
    else:
        # Redacted for processes without Location Services permission. The
        # frontend hides the IP settings of a Wi-Fi interface without an SSID.
        ssid = HIDDEN_SSID.encode()
        active = None
    return WifiConfig(
        mode=WifiMode.INFRASTRUCTURE,
        ssid=ssid,
        auth=wifi_auth(airport.get("CachedScanRecord")),
        psk=None,
        # Only an active Wi-Fi client (CoreWLAN) can read the signal
        signal=None,
        active_ssid=active,
    )


def _read_preferences() -> dict[str, Any] | None:
    try:
        with _PREFERENCES.open("rb") as file:
            preferences = plistlib.load(file)
    except (OSError, ValueError, ExpatError) as err:
        _LOGGER.warning("Can't read the network settings: %s", err)
        return None
    return preferences if isinstance(preferences, dict) else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _entity(service: dict[str, Any], name: str) -> dict[str, Any]:
    """Return a service's IPv4, IPv6 or DNS settings; empty when switched off."""
    entity = _dict(service.get(name))
    return {} if entity.get(_INACTIVE) else entity


@dataclass(slots=True)
class _Service:
    id: str
    device: str
    wireless: bool
    enabled: bool
    setup4: dict[str, Any]
    setup6: dict[str, Any]
    setup_dns: dict[str, Any]


def configured_services(preferences: dict[str, Any]) -> list[_Service] | None:
    """Return the current set's Ethernet and Wi-Fi services, first per device.

    None when the file has no current set, so the caller keeps what it had.
    """
    current = preferences.get("CurrentSet")
    services = _dict(preferences.get("NetworkServices"))
    if not isinstance(current, str):
        return None
    network = _dict(
        _dict(_dict(preferences.get("Sets")).get(current.rpartition("/")[2])).get(
            "Network"
        )
    )
    linked = list(_dict(network.get("Service")))
    if not linked:
        return None
    order = _dict(_dict(network.get("Global")).get("IPv4")).get("ServiceOrder")
    order = [sid for sid in order if sid in linked] if isinstance(order, list) else []
    found: dict[str, _Service] = {}
    for sid in order + [sid for sid in linked if sid not in order]:
        service = _dict(services.get(sid))
        interface = _dict(service.get("Interface"))
        device = interface.get("DeviceName")
        hardware = interface.get("Hardware")
        if (
            not isinstance(device, str)
            or interface.get("Type") != "Ethernet"
            or hardware not in ("Ethernet", "AirPort")
            # Internal adapters System Settings doesn't list
            or interface.get("HiddenConfiguration")
            # Thunderbolt Bridge: NetworkManager's bridges aren't listed either
            or device.startswith("bridge")
            or device in found
        ):
            continue
        found[device] = _Service(
            id=sid,
            device=device,
            wireless=hardware == "AirPort",
            enabled=not service.get(_INACTIVE),
            setup4=_entity(service, "IPv4"),
            setup6=_entity(service, "IPv6"),
            setup_dns=_entity(service, "DNS"),
        )
    return list(found.values())


def _interface(
    service: _Service,
    state: tuple[dict[str, Any] | None, dict[str, Any] | None, dict[str, Any] | None],
    link: Link,
    primary: bool,
    airport: dict[str, Any] | None,
) -> Interface:
    state4, state6, state_dns = state
    set_dns = _addresses(service.setup_dns.get("ServerAddresses"))
    # Servers set in System Settings replace the ones from DHCP
    live_dns = set_dns or _addresses(_dict(state_dns).get("ServerAddresses"))
    method4 = (
        _IPV4_METHODS.get(
            str(service.setup4.get("ConfigMethod")), InterfaceMethod.DISABLED
        )
        if service.enabled
        else InterfaceMethod.DISABLED
    )
    method6 = (
        _IPV6_METHODS.get(
            str(service.setup6.get("ConfigMethod")), InterfaceMethod.DISABLED
        )
        if service.enabled
        else InterfaceMethod.DISABLED
    )
    static4 = method4 == InterfaceMethod.STATIC
    static6 = method6 == InterfaceMethod.STATIC
    ready4 = service.enabled and state4 is not None
    ready6 = service.enabled and state6 is not None
    active = service.enabled and link.active
    return Interface(
        name=service.device,
        mac=link.mac,
        path=service.device,
        enabled=active,
        connected=active and (ready4 or ready6),
        primary=primary,
        type=InterfaceType.WIRELESS if service.wireless else InterfaceType.ETHERNET,
        ipv4=IpConfig(
            address=list(link.ipv4),
            gateway=_gateway4(_dict(state4).get("Router")),
            nameservers=[ip for ip in live_dns if isinstance(ip, IPv4Address)],
            ready=ready4,
        ),
        ipv4setting=IpSetting(
            method=method4,
            address=_static(service.setup4, "SubnetMasks", 4) if static4 else [],
            gateway=_gateway4(service.setup4.get("Router")) if static4 else None,
            route_metric=None,
            nameservers=[ip for ip in set_dns if isinstance(ip, IPv4Address)],
        ),
        ipv6=IpConfig(
            address=list(link.ipv6),
            gateway=_gateway6(_dict(state6).get("Router")),
            nameservers=[ip for ip in live_dns if isinstance(ip, IPv6Address)],
            ready=ready6,
        ),
        ipv6setting=Ip6Setting(
            method=method6,
            address=_static(service.setup6, "PrefixLength", 6) if static6 else [],
            gateway=_gateway6(service.setup6.get("Router")) if static6 else None,
            route_metric=None,
            nameservers=[ip for ip in set_dns if isinstance(ip, IPv6Address)],
        ),
        wifi=_wifi(airport) if service.wireless and active else None,
        vlan=None,
        # mDNSResponder answers and resolves on every interface; no LLMNR
        mdns=MulticastDnsMode.ANNOUNCE,
        llmnr=MulticastDnsMode.OFF,
    )


def read_interfaces() -> list[tuple[Interface, bool]] | None:
    """Return each Ethernet and Wi-Fi interface and whether its service is on.

    None when macOS didn't answer, so the caller keeps what it had.
    """
    preferences = _read_preferences()
    if preferences is None:
        return None
    services = configured_services(preferences)
    if services is None:
        _LOGGER.warning("Can't read the network settings: no current set")
        return None

    wireless = [service.device for service in services if service.wireless]
    keys = [
        "State:/Network/Global/IPv4",
        "State:/Network/Global/IPv6",
        *(key.format(service.id) for service in services for key in _STATE_KEYS),
        *(f"State:/Network/Interface/{device}/AirPort" for device in wireless),
    ]
    answers = _show(keys)
    if answers is None:
        return None
    ifconfig = _run([_IFCONFIG])
    if ifconfig is None:
        return None
    links = parse_ifconfig(ifconfig)

    global4, global6 = _dict(answers[0]), _dict(answers[1])
    primary = global4.get("PrimaryInterface") or global6.get("PrimaryInterface")
    width = len(_STATE_KEYS)
    states = answers[2 : 2 + width * len(services)]
    airport_of = dict(zip(wireless, answers[2 + width * len(services) :], strict=True))
    return [
        (
            _interface(
                service,
                (
                    states[index * width],
                    states[index * width + 1],
                    states[index * width + 2],
                ),
                links.get(service.device, Link()),
                service.device == primary,
                airport_of.get(service.device),
            ),
            service.enabled,
        )
        for index, service in enumerate(services)
    ]


def _unique(values: Iterable[str]) -> list[str]:
    return list(dict.fromkeys(values))


class NativeNetworkManager(NetworkManager):
    """NetworkManager fed from macOS, which owns the network settings."""

    def __init__(self, coresys) -> None:
        """Initialize."""
        super().__init__(coresys)
        self._native: list[tuple[Interface, bool]] = []
        self._reading = asyncio.Lock()

    @property
    def interfaces(self) -> list[Interface]:
        """Return the Ethernet and Wi-Fi interfaces."""
        return [interface for interface, _ in self._native]

    @property
    def dns_servers(self) -> list[str]:
        """Return the DNS servers of connected interfaces, the primary's first."""
        connected = sorted(
            (inet for inet in self.interfaces if inet.connected),
            key=lambda inet: not inet.primary,
        )
        return _unique(
            str(server)
            for inet in connected
            for config in (inet.ipv4, inet.ipv6)
            if config
            for server in config.nameservers
        )

    def get(self, inet_name: str) -> Interface:
        """Return interface from interface name."""
        for interface in self.interfaces:
            if interface.name == inet_name:
                return interface
        raise HostNetworkNotFound

    async def get_with_config(self, inet_name: str) -> ResolvedInterface:
        """Return the interface; its macOS service is its profile."""
        for interface, enabled in self._native:
            if interface.name == inet_name:
                return ResolvedInterface(interface, has_profile=True, enabled=enabled)
        raise HostNetworkNotFound

    async def interfaces_with_config(self) -> list[ResolvedInterface]:
        """Return all interfaces with their services."""
        return [
            ResolvedInterface(interface, has_profile=True, enabled=enabled)
            for interface, enabled in self._native
        ]

    async def load(self) -> None:
        """Read the interfaces; macOS has no settings to reapply."""
        await self.update()

    async def update(self, *, force_connectivity_check: bool = False) -> None:
        """Read the interfaces again from macOS."""
        _LOGGER.debug("Updating local network information")
        # One read at a time, so an older read never lands after a newer one
        async with self._reading:
            try:
                found = await self.sys_run_in_executor(read_interfaces)
            except Exception as err:  # pylint: disable=broad-except
                # The network page must never stop the Supervisor from starting
                _LOGGER.exception("Can't read the network interfaces: %s", err)
                found = None
            if found is not None:
                self._native = found
        await self.check_connectivity(force=force_connectivity_check)

    async def check_connectivity(self, *, force: bool = False) -> None:
        """No host connectivity check without NetworkManager: unknown."""
        self.connectivity = None

    async def resolve_ssid(self, inet_name: str, ssid: str) -> bytes:
        """Wi-Fi settings are macOS's too; refuse before any lookup."""
        raise MacOSOwnsNetworkError(SETTINGS_ON_MACOS, _LOGGER.info)

    async def apply_changes(self, interface: Interface, **_: Any) -> str | None:
        """MacOS owns the network settings."""
        raise MacOSOwnsNetworkError(SETTINGS_ON_MACOS, _LOGGER.info)

    async def apply_changes_v2(self, interface: Interface) -> str | None:
        """MacOS owns the network settings."""
        raise MacOSOwnsNetworkError(SETTINGS_ON_MACOS, _LOGGER.info)

    async def create_vlan(self, interface: Interface) -> None:
        """MacOS owns the network settings."""
        raise MacOSOwnsNetworkError(SETTINGS_ON_MACOS, _LOGGER.info)

    async def scan_wifi(self, interface: Interface) -> list[AccessPoint]:
        """Never scan: a scan can wedge this Mac's Wi-Fi link."""
        raise MacOSOwnsNetworkError(NO_WIFI_SCAN, _LOGGER.info)
