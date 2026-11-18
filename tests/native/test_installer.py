"""Test the installer (macos/install/install.py): what it plans, refuses and runs."""

import hashlib
import importlib.util
import json
from pathlib import Path
import plistlib
import sys
from types import ModuleType
from typing import Any

import pytest

INSTALLER_PATH = Path(__file__).parents[2] / "macos" / "install" / "install.py"


def _load() -> ModuleType:
    spec = importlib.util.spec_from_file_location("ha_macos_install", INSTALLER_PATH)
    assert spec
    assert spec.loader
    module = importlib.util.module_from_spec(spec)
    sys.modules["ha_macos_install"] = module
    spec.loader.exec_module(module)
    return module


install = _load()

SERIAL_A = "0" * 31 + "1"
SERIAL_B = "0" * 31 + "2"


def _usb(name: str, vid: int, pid: int, children: list, serial: str = "") -> dict:
    return {
        "IOObjectClass": "IOUSBHostDevice",
        "USB Product Name": name,
        "idVendor": vid,
        "idProduct": pid,
        "USB Serial Number": serial,
        "IORegistryEntryChildren": children,
    }


def _port(path: str) -> dict:
    """Return an interface, its driver and the serial client, as macOS nests them."""
    return {
        "IOObjectClass": "IOUSBHostInterface",
        "IORegistryEntryChildren": [
            {
                "IOObjectClass": "AppleUSBSLCOM",
                "IORegistryEntryChildren": [
                    {
                        "IOObjectClass": "IOSerialBSDClient",
                        "IOCalloutDevice": path,
                        "IODialinDevice": path.replace("/cu.", "/tty."),
                    }
                ],
            }
        ],
    }


def _mg24(path: str, serial: str) -> dict:
    return _usb("SONOFF Dongle Plus MG24", 0x10C4, 0xEA60, [_port(path)], serial)


IOREG = plistlib.dumps(
    [
        _usb(
            "USB2.1 Hub",
            0x05E3,
            0x0610,
            [
                _usb(
                    "USB2.1 Hub",
                    0x05E3,
                    0x0610,
                    [_mg24("/dev/cu.usbserial-1340", SERIAL_A)],
                ),
                _mg24("/dev/cu.usbserial-140", SERIAL_B),
            ],
        ),
        _usb("SlimFit Cam", 0x04E8, 0x20D3, []),
        _usb(
            "Other serial",
            0x1A86,
            0x55D4,
            [_port("/dev/cu.usbmodem-1")],
            "x",
        ),
    ]
)


def test_dongles_are_found_under_hubs() -> None:
    """Each dongle gets its own port; hubs and devices without one are skipped."""
    dongles = install.parse_ioreg(IOREG)
    assert [(d.device, d.serial, d.mg24) for d in dongles] == [
        ("/dev/cu.usbmodem-1", "x", False),
        ("/dev/cu.usbserial-1340", SERIAL_A, True),
        ("/dev/cu.usbserial-140", SERIAL_B, True),
    ]
    assert install.parse_ioreg(b"") == []


@pytest.mark.parametrize(
    ("output", "kind"),
    [
        (
            "Detected ApplicationType.EZSP, version '7.4.5.0 build 0' at 115200 "
            "baudrate (bootloader baudrate None)",
            "ezsp",
        ),
        ("Detected ApplicationType.CPC, version '4.4.0' at 460800 baudrate", "cpc"),
        ("Detected ApplicationType.SPINEL, version 'SL-OPENTHREAD'", "spinel"),
        ("Detected ApplicationType.GECKO_BOOTLOADER, version '2.4.2'", "bootloader"),
        ("Failed to probe running application type", None),
    ],
)
def test_probe_output(output: str, kind: str | None) -> None:
    """The bootloader baud rate in an EZSP line doesn't make it a bootloader."""
    assert install.parse_probe(output) == kind


MANIFEST = {
    "zigbee-ble": {"version": "1", "url": "https://x/zb.gbl", "sha256": "a" * 64},
    "thread-rcp": {"version": "1", "url": "https://x/rcp.gbl", "sha256": "b" * 64},
    "thread-ble": {"version": None, "url": None, "sha256": None},
}


def _plan(**radios: str) -> Any:
    dongles = install.parse_ioreg(IOREG)
    names = {
        "a": "/dev/cu.usbserial-1340",
        "b": "/dev/cu.usbserial-140",
        "c": "/dev/cu.usbmodem-1",
    }
    return install.Plan(
        root=Path("/x/ha"),
        prefix="ha-macos",
        source="src",
        branch="main",
        radios={names[k]: v for k, v in radios.items()},
        dongles=dongles,
    )


def test_bluetooth_on_a_dongle_means_the_system_layout() -> None:
    """Zigbee + Bluetooth: no login needed, nimbled and the bridge, the helper."""
    plan = _plan(a="zigbee-ble", b="thread-rcp")
    plan.check()
    assert plan.layout == "system"
    assert plan.bluetooth_dongle == "/dev/cu.usbserial-1340"
    assert plan.apps == [
        "core_zigbee_ble_bridge",
        "core_nimbled",
        "core_openthread_border_router",
        "core_matter_server",
    ]
    assert plan.needs_helper


def test_without_a_bluetooth_dongle_it_runs_in_the_session() -> None:
    """Only the Mac's radio: a login item, and no helper unless a root app needs it."""
    keep = _plan(a="keep", b="keep")
    assert keep.layout == "session"
    assert keep.apps == []
    assert not keep.needs_helper
    thread = _plan(a="keep", b="thread-rcp")
    assert thread.layout == "session"
    assert thread.needs_helper  # the border router runs as root


@pytest.mark.parametrize(
    ("radios", "message"),
    [
        ({"a": "zigbee-ble", "b": "thread-ble"}, "only one dongle can carry Bluetooth"),
        ({"a": "thread-rcp", "b": "thread-rcp"}, "only one dongle can carry Thread"),
        ({"c": "zigbee-ble"}, "not a SONOFF Dongle Plus MG24"),
    ],
)
def test_plans_it_refuses(radios: dict[str, str], message: str) -> None:
    """Refuse two Bluetooth or two Thread dongles, and flashing an unknown one."""
    with pytest.raises(install.InstallError, match=message):
        _plan(**radios).check()


def test_choices_offer_only_published_images() -> None:
    """A choice without an image isn't offered; asking, presets and --yes."""
    dongles = install.parse_ioreg(IOREG)
    assert install.available_choices(MANIFEST) == ["zigbee-ble", "thread-rcp"]
    asked = []

    def ask(label: str, titles: list[str]) -> int:
        asked.append((label, titles))
        return 0

    radios = install.choose_radios(dongles, MANIFEST, ask, False, {})
    assert radios == {
        "/dev/cu.usbserial-1340": "zigbee-ble",
        "/dev/cu.usbserial-140": "zigbee-ble",
    }
    assert asked[0][1] == [
        "Zigbee + Bluetooth",
        "Thread (border router)",
        "Leave it as it is",
    ]
    assert install.choose_radios(dongles, MANIFEST, ask, True, {}) == {
        "/dev/cu.usbserial-1340": "keep",
        "/dev/cu.usbserial-140": "keep",
    }
    with pytest.raises(install.InstallError, match="not available"):
        install.choose_radios(
            dongles, MANIFEST, ask, True, {"/dev/cu.usbserial-140": "thread-ble"}
        )


def test_manifest_needs_a_checksum(tmp_path: Path) -> None:
    """An image with a url but no SHA-256, or an unknown choice, is refused."""
    path = tmp_path / "firmware.json"
    path.write_text(json.dumps({"zigbee-ble": {"url": "https://x/a.gbl"}}))
    with pytest.raises(install.InstallError, match="no sha256"):
        install.load_manifest(path)
    path.write_text(json.dumps({"bluetooth-only": {}}))
    with pytest.raises(install.InstallError, match="unknown choice"):
        install.load_manifest(path)
    shipped = install.load_manifest()
    assert set(shipped) <= set(install.CHOICES)


def test_an_image_with_the_wrong_checksum_is_never_flashed(tmp_path: Path) -> None:
    """The download is checked against the pinned SHA-256 and removed if it differs."""
    source = tmp_path / "zb.gbl"
    source.write_bytes(b"firmware")
    runner = install.Runner(dry_run=False)
    good = {"url": source.as_uri(), "sha256": hashlib.sha256(b"firmware").hexdigest()}
    assert (
        install.fetch_image(runner, good, tmp_path / "dl").read_bytes() == b"firmware"
    )
    bad = {"url": source.as_uri(), "sha256": "0" * 64}
    with pytest.raises(install.InstallError, match="SHA-256 mismatch"):
        install.fetch_image(runner, bad, tmp_path / "dl2")
    assert not (tmp_path / "dl2" / "zb.gbl").exists()


class FakeRunner:
    """Records commands; answers probes from a queue."""

    dry_run = False

    def __init__(self, probes: list[str]) -> None:
        """Start with the probe answers to give, in order."""
        self.commands: list[list[str]] = []
        self.probes = probes

    def say(self, text: str) -> None:
        """Ignore progress lines."""

    def run(self, cmd: list[str], check: bool = True, capture: bool = False, **_: Any):
        """Record cmd; a probe gets the next answer."""
        self.commands.append(cmd)
        out = self.probes.pop(0) if cmd[-1] == "probe" else ""
        return install.subprocess.CompletedProcess(cmd, 0, out, "")


def _installer(plan: Any, runner: Any, tmp_path: Path) -> Any:
    plan.root = tmp_path
    image = tmp_path / "firmware" / "zb.gbl"
    image.parent.mkdir()
    image.write_bytes(b"fw")
    manifest = {
        "zigbee-ble": {
            "url": "https://x/zb.gbl",
            "sha256": hashlib.sha256(b"fw").hexdigest(),
        }
    }
    return install.Installer(plan, runner, manifest)


def test_flash_backs_up_a_zigbee_network_first(tmp_path: Path) -> None:
    """EZSP found: network backup, then flash with the bootloader reset, then probe."""
    runner = FakeRunner(
        [
            "Detected ApplicationType.EZSP, version '7.4.5.0' (bootloader baudrate None)",
            "Detected ApplicationType.CPC, version '4.4.0'",
        ]
    )
    installer = _installer(_plan(a="zigbee-ble"), runner, tmp_path)
    installer.flash("/dev/cu.usbserial-1340", "zigbee-ble")
    names = [Path(cmd[0]).name for cmd in runner.commands]
    assert names == [
        "universal-silabs-flasher",
        "zigpy",
        "universal-silabs-flasher",
        "universal-silabs-flasher",
    ]
    backup = runner.commands[1]
    assert backup[backup.index("backup") + 1].startswith(
        str(tmp_path / "backups" / f"zigbee-{SERIAL_A}-")
    )
    flash = runner.commands[2]
    assert flash[flash.index("--bootloader-reset") + 1] == "rts_dtr"
    assert flash[flash.index("--firmware") + 1] == str(tmp_path / "firmware" / "zb.gbl")


def test_flash_that_ends_in_the_wrong_firmware_is_an_error(tmp_path: Path) -> None:
    """After flashing, the probe must find the expected firmware."""
    runner = FakeRunner(
        [
            "Detected ApplicationType.CPC, version '4.4.0'",
            "Detected ApplicationType.GECKO_BOOTLOADER",
        ]
    )
    installer = _installer(_plan(a="zigbee-ble"), runner, tmp_path)
    with pytest.raises(install.InstallError, match="runs bootloader, expected cpc"):
        installer.flash("/dev/cu.usbserial-1340", "zigbee-ble")
    assert not any(Path(cmd[0]).name == "zigpy" for cmd in runner.commands)


def test_steps_follow_the_plan(tmp_path: Path) -> None:
    """Zigbee + Bluetooth: tools, helper, flash, then the system layout and macscan."""
    installer = _installer(_plan(a="zigbee-ble"), FakeRunner([]), tmp_path)
    assert [name for name, _, _ in installer.steps()] == [
        "check",
        "folders",
        "source",
        "tools",
        "helper",
        "flash",
        "supervisor",
        "apps",
        "layout",
        "macscan",
    ]
    session = install.Installer(_plan(a="keep", b="keep"), FakeRunner([]), {})
    assert [name for name, _, _ in session.steps()] == [
        "check",
        "folders",
        "source",
        "supervisor",
        "apps",
    ]


def test_the_supervisor_plist_holds_no_secret() -> None:
    """The login item carries paths and ports only; tokens stay in the data folder."""
    installer = install.Installer(_plan(a="keep"), FakeRunner([]), {}, api_port=4364)
    data = installer.agent_plist()
    env = data["EnvironmentVariables"]
    assert data["Label"] == "ha-macos.supervisor"
    assert data["ProgramArguments"] == [
        "/x/ha/supervisor/bin/python",
        "-m",
        "supervisor",
    ]
    assert env["SUPERVISOR_API_PORT"] == "4364"
    assert env["SUPERVISOR_DATA"] == "/x/ha/data"
    assert not any("TOKEN" in key for key in env)
    assert data["ProcessType"] == "Interactive"


def test_border_router_options_use_the_by_id_link(tmp_path: Path) -> None:
    """Apps get the Supervisor's by-id link for their dongle and the backbone."""
    installer = _installer(_plan(b="thread-rcp"), FakeRunner([]), tmp_path)
    installer.plan.backbone = "en1"
    link = f"/x/data/dev/serial/by-id/usb-SONOFF_SONOFF_Dongle_Plus_MG24_{SERIAL_B}-if00-port0"
    installer.api = lambda method, path, body=None: {
        "devices": [
            {
                "dev_path": "/dev/cu.usbserial-1340",
                "by_id": link.replace(SERIAL_B, SERIAL_A),
            },
            {"dev_path": "/dev/cu.usbserial-140", "by_id": link},
        ]
    }
    assert installer.app_options("core_openthread_border_router") == {
        "backbone_interface": "en1",
        "device": link,
    }


def test_dry_run_changes_nothing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture
) -> None:
    """--dry-run prints the plan and every step, and writes nothing."""
    monkeypatch.setattr(install, "find_dongles", lambda: install.parse_ioreg(IOREG))
    monkeypatch.setattr(install, "default_interface", lambda: "en0")
    root = tmp_path / "ha"
    assert (
        install.main(["--dry-run", "--root", str(root), "--prefix", "ha-macos-test"])
        == 0
    )
    out = capsys.readouterr().out
    assert "Mac's own Bluetooth" in out
    assert "== 1. check the Mac" in out
    assert not root.exists()


def test_a_folder_with_spaces_is_refused(tmp_path: Path) -> None:
    """Refuse a folder with spaces: uv splits paths on them."""
    assert install.main(["--dry-run", "--root", str(tmp_path / "Home Assistant")]) == 2
