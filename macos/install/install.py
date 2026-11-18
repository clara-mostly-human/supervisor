#!/usr/bin/python3
"""Install Home Assistant natively on this Mac: Supervisor, Core and apps.

What it does, in order:

1. Checks the Mac: Apple silicon, macOS 15 or later, Homebrew's Python, free
   ports, and no existing install under the same prefix.
2. Finds radio dongles (read only, through the I/O Registry) and asks what
   each SONOFF Dongle Plus MG24 should run. The choices are the firmware
   images in firmware.json:
   - Zigbee + Bluetooth: ZHA through the zigbee-ble bridge app, Bluetooth
     through nimbled.
   - Thread: the OpenThread Border Router app.
   - Thread + Bluetooth: offered once its image is published.
   - Leave it as it is.
3. Bluetooth decides where Home Assistant runs:
   - With Bluetooth on a dongle, nothing needs a login: the Supervisor, Core
     and apps run as system services (the "system" layout).
   - Without one, Home Assistant uses the Mac's own Bluetooth, which macOS
     only gives to programs in a logged-in session. It then runs as a login
     item (the "session" layout) and stops at logout. The installer says so
     and asks first.
4. Flashes the chosen firmware safely: it checks the image's SHA-256, probes
   what the dongle runs now, keeps a network backup if it runs Zigbee (EZSP),
   flashes with universal-silabs-flasher and probes again. The application
   flash leaves the dongle's network settings (NVM3) and its bootloader alone.
5. Clones the Supervisor, makes its Python environment, installs the root
   helper when it is needed (sudo, once), starts the Supervisor, waits for
   Core, and installs and starts the apps for the chosen radios.

    install.sh [--dry-run] [--yes] [--root DIR] [--source PATH] ...

--dry-run prints the plan and changes nothing. Runs on Apple's
/usr/bin/python3 (standard library only), so it works before Homebrew's
Python is installed.
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field
import hashlib
import json
import os
from pathlib import Path
import platform
import plistlib
import re
import shlex
import shutil
import socket
import subprocess
import sys
import time
import urllib.request
import uuid

VERSION = "0.1.0"
HERE = Path(__file__).resolve().parent
FORK_URL = "https://github.com/clara-mostly-human/supervisor"
FORK_BRANCH = "macos_support-2026.11"
PREFIX = "ha-macos"
API_PORT = 4358
CORE_PORT = 8123
PYTHON = "/opt/homebrew/opt/python@3.14/bin/python3.14"
BREW = "/opt/homebrew/bin/brew"
MIN_MACOS = (15, 0)
FLASHER = "universal-silabs-flasher==1.3.0"
ZIGPY_CLI = "zigpy-cli==2.0.0"
SYSTEM_PATH = "/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin"

# USB dongles this installer knows how to flash: (vendor id, product id,
# product name). Others are listed and left alone.
MG24 = (0x10C4, 0xEA60, "SONOFF Dongle Plus MG24")

# What each firmware choice needs. "bluetooth": it gives nimbled a controller.
CHOICES = {
    "zigbee-ble": {
        "title": "Zigbee + Bluetooth",
        "bluetooth": True,
        "apps": ["core_zigbee_ble_bridge", "core_nimbled"],
        "probe": "cpc:460800,ezsp:115200,spinel:460800,bootloader:115200",
        "expect": "cpc",
    },
    "thread-rcp": {
        "title": "Thread (border router)",
        "bluetooth": False,
        "apps": ["core_openthread_border_router", "core_matter_server"],
        "probe": "spinel:460800,cpc:460800,ezsp:115200,bootloader:115200",
        "expect": "spinel",
    },
    "thread-ble": {
        "title": "Thread + Bluetooth",
        "bluetooth": True,
        "apps": [
            "core_thread_ble_bridge",
            "core_openthread_border_router",
            "core_matter_server",
            "core_nimbled",
        ],
        "probe": "cpc:460800,spinel:460800,ezsp:115200,bootloader:115200",
        "expect": "cpc",
    },
}
KEEP = "keep"

MAC_BLUETOOTH_WARNING = """\
Without a Bluetooth dongle, Home Assistant uses this Mac's own Bluetooth.
macOS gives Bluetooth only to programs running in a logged-in user's session,
so Home Assistant will run as a login item of this account:
  - it starts when you log in and stops when you log out;
  - after a restart or power cut it waits until someone logs in.
To keep it running, leave this account logged in (System Settings > Users &
Groups > "Automatically log in as" is only offered with FileVault off).
A SONOFF Dongle Plus MG24 with Zigbee + Bluetooth or Thread + Bluetooth
firmware removes this limit: Home Assistant then needs nobody logged in."""


class InstallError(Exception):
    """The install can't go on; the message says why and what to do."""


# Facts about the Mac


@dataclass
class Dongle:
    """A USB serial device."""

    product: str
    vendor_id: int
    product_id: int
    serial: str
    device: str  # /dev/cu.*

    @property
    def mg24(self) -> bool:
        return (self.vendor_id, self.product_id, self.product) == MG24

    @property
    def label(self) -> str:
        return "%s (serial %s, %s)" % (self.product, self.serial or "?", self.device)


def _callouts(entry: dict) -> list[str]:
    """Return the serial ports under entry, not descending into other USB devices."""
    found = []
    for child in entry.get("IORegistryEntryChildren", []):
        if child.get("IOObjectClass") == "IOUSBHostDevice":
            continue
        if child.get("IOCalloutDevice"):
            found.append(child["IOCalloutDevice"])
        found += _callouts(child)
    return found


def _usb_devices(entries: list[dict]):
    for entry in entries:
        if entry.get("IOObjectClass") == "IOUSBHostDevice":
            yield entry
        yield from _usb_devices(entry.get("IORegistryEntryChildren", []))


def parse_ioreg(data: bytes) -> list[Dongle]:
    """Return the USB devices that have a serial port, from `ioreg -a` output.

    Devices sit under hubs; each device's own subtree (its interfaces and
    drivers) holds its serial port. A hub's subtree also holds the devices
    plugged into it, which are skipped when collecting the hub's ports.
    """
    dongles = []
    roots = plistlib.loads(data) if data.strip() else []
    seen = set()
    for entry in _usb_devices(roots):
        callouts = sorted(d for d in _callouts(entry) if d.startswith("/dev/cu."))
        if not callouts or callouts[0] in seen:
            continue
        seen.add(callouts[0])
        dongles.append(
            Dongle(
                product=str(entry.get("USB Product Name") or entry.get("kUSBProductString") or ""),
                vendor_id=int(entry.get("idVendor", 0)),
                product_id=int(entry.get("idProduct", 0)),
                serial=str(entry.get("USB Serial Number") or entry.get("kUSBSerialNumberString") or ""),
                device=callouts[0],
            )
        )
    return sorted(dongles, key=lambda d: d.device)


def find_dongles(run=subprocess.run) -> list[Dongle]:
    """Ask the I/O Registry for USB devices with serial ports (no port is opened)."""
    out = run(
        ["/usr/sbin/ioreg", "-r", "-c", "IOUSBHostDevice", "-l", "-a"],
        capture_output=True,
        check=False,
    ).stdout
    return parse_ioreg(out or b"")


def default_interface(run=subprocess.run) -> str:
    """Return the interface of the default route (the border router's backbone)."""
    out = run(
        ["/sbin/route", "-n", "get", "default"], capture_output=True, text=True, check=False
    ).stdout
    for line in out.splitlines():
        key, _, value = line.strip().partition(":")
        if key == "interface" and value.strip():
            return value.strip()
    return "en0"


def port_free(port: int) -> bool:
    with socket.socket() as sock:
        sock.settimeout(0.5)
        return sock.connect_ex(("127.0.0.1", port)) != 0


def load_manifest(path: Path = HERE / "firmware.json") -> dict:
    """Return the firmware images by choice; a choice without a url isn't offered."""
    data = json.loads(path.read_text())
    for name, image in data.items():
        if name not in CHOICES:
            raise InstallError("firmware.json: unknown choice %r" % name)
        if image.get("url") and len(image.get("sha256", "")) != 64:
            raise InstallError("firmware.json: %s has no sha256" % name)
    return data


def available_choices(manifest: dict) -> list[str]:
    return [name for name in CHOICES if (manifest.get(name) or {}).get("url")]


# The plan


@dataclass
class Plan:
    root: Path
    prefix: str
    source: str  # git URL or local path
    branch: str
    radios: dict[str, str] = field(default_factory=dict)  # device -> choice
    dongles: list[Dongle] = field(default_factory=list)
    macscan: bool = True
    backbone: str = "en0"

    @property
    def bluetooth_dongle(self) -> str | None:
        for device, choice in self.radios.items():
            if choice in CHOICES and CHOICES[choice]["bluetooth"]:
                return device
        return None

    @property
    def layout(self) -> str:
        return "system" if self.bluetooth_dongle else "session"

    @property
    def flashes(self) -> dict[str, str]:
        return {d: c for d, c in self.radios.items() if c in CHOICES}

    @property
    def apps(self) -> list[str]:
        apps: list[str] = []
        for choice in self.flashes.values():
            for app in CHOICES[choice]["apps"]:
                if app not in apps:
                    apps.append(app)
        return apps

    @property
    def needs_helper(self) -> bool:
        # The system layout loads LaunchDaemons; the border router runs as root.
        return self.layout == "system" or "core_openthread_border_router" in self.apps

    def dongle(self, device: str) -> Dongle:
        return next(d for d in self.dongles if d.device == device)

    def check(self) -> None:
        bluetooth = [d for d, c in self.flashes.items() if CHOICES[c]["bluetooth"]]
        if len(bluetooth) > 1:
            raise InstallError(
                "only one dongle can carry Bluetooth (nimbled drives one controller): "
                + ", ".join(bluetooth)
            )
        threads = [d for d, c in self.flashes.items() if c.startswith("thread")]
        if len(threads) > 1:
            raise InstallError("only one dongle can carry Thread: " + ", ".join(threads))
        for device in self.flashes:
            if not self.dongle(device).mg24:
                raise InstallError("%s is not a SONOFF Dongle Plus MG24" % device)

    def describe(self) -> list[str]:
        lines = [
            "Install into %s (launchd prefix %s)" % (self.root, self.prefix),
            "Supervisor source: %s (%s)" % (self.source, self.branch),
        ]
        for device, choice in self.radios.items():
            what = CHOICES[choice]["title"] if choice in CHOICES else "leave as it is"
            lines.append("Dongle %s: %s" % (self.dongle(device).label, what))
        if self.layout == "system":
            lines.append(
                "Bluetooth on %s: Home Assistant runs as system services and needs "
                "nobody logged in" % self.bluetooth_dongle
            )
            if self.macscan:
                lines.append(
                    "The Mac's own Bluetooth joins as a listen-only extra radio while "
                    "someone is logged in (macscan)"
                )
        else:
            lines.append(
                "Bluetooth: the Mac's own radio; Home Assistant runs as a login item "
                "and stops at logout"
            )
        lines.append("Apps: " + (", ".join(self.apps) or "none"))
        if self.needs_helper:
            lines.append("Root helper: installed with sudo (asks for your password once)")
        return lines


def choose_radios(
    dongles: list[Dongle], manifest: dict, ask, yes: bool, preset: dict[str, str]
) -> dict[str, str]:
    """Return device -> choice for every MG24 dongle (preset, else asked)."""
    offered = available_choices(manifest)
    radios: dict[str, str] = {}
    for dongle in dongles:
        if not dongle.mg24:
            continue
        if dongle.device in preset:
            choice = preset[dongle.device]
            if choice != KEEP and choice not in offered:
                raise InstallError(
                    "%s: %r is not available (choose from %s)"
                    % (dongle.device, choice, ", ".join(offered + [KEEP]))
                )
            radios[dongle.device] = choice
        elif yes:
            radios[dongle.device] = KEEP
        else:
            options = offered + [KEEP]
            titles = [CHOICES[o]["title"] if o in CHOICES else "Leave it as it is" for o in options]
            radios[dongle.device] = options[ask(dongle.label, titles)]
    return radios


# Running things


class Runner:
    """Runs commands, or only prints them with --dry-run."""

    def __init__(self, dry_run: bool, log: Path | None = None) -> None:
        self.dry_run = dry_run
        self.log = log

    def say(self, text: str) -> None:
        print(text, flush=True)
        if self.log and not self.dry_run:
            self.log.parent.mkdir(parents=True, exist_ok=True)
            with self.log.open("a") as file:
                file.write("%s %s\n" % (time.strftime("%F %T"), text))

    def run(self, cmd: list[str], check: bool = True, capture: bool = False, **kwargs):
        self.say("$ " + " ".join(shlex.quote(c) for c in cmd))
        if self.dry_run:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        result = subprocess.run(
            cmd, text=True, capture_output=capture, check=False, **kwargs
        )
        if check and result.returncode != 0:
            detail = (result.stderr or result.stdout or "").strip() if capture else ""
            raise InstallError(
                "%s failed (exit %d)%s" % (cmd[0], result.returncode, ": " + detail if detail else "")
            )
        return result


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as file:
        for chunk in iter(lambda: file.read(1 << 20), b""):
            digest.update(chunk)
    return digest.hexdigest()


def fetch_image(runner: Runner, image: dict, folder: Path) -> Path:
    """Download a firmware image (once) and refuse it unless its SHA-256 matches."""
    name = image["url"].rsplit("/", 1)[-1]
    path = folder / name
    runner.say("firmware %s (sha256 %s...)" % (name, image["sha256"][:12]))
    if runner.dry_run:
        return path
    folder.mkdir(parents=True, exist_ok=True)
    if not path.exists() or sha256(path) != image["sha256"]:
        tmp = path.with_suffix(".part")
        with urllib.request.urlopen(image["url"], timeout=120) as response:  # noqa: S310
            tmp.write_bytes(response.read())
        tmp.rename(path)
    if sha256(path) != image["sha256"]:
        path.unlink()
        raise InstallError("%s: SHA-256 mismatch, refusing to flash it" % name)
    return path


PROBE_TYPES = {
    "GECKO_BOOTLOADER": "bootloader",
    "CPC": "cpc",
    "EZSP": "ezsp",
    "SPINEL": "spinel",
    "ROUTER": "router",
}


def parse_probe(output: str) -> str | None:
    """Return the firmware type universal-silabs-flasher's probe found, or None.

    Its line reads e.g. "Detected ApplicationType.EZSP, version '7.4.5.0 build 0'
    at 115200 baudrate (bootloader baudrate None)".
    """
    found = re.search(r"Detected ApplicationType\.([A-Z_]+)", output)
    return PROBE_TYPES.get(found.group(1)) if found else None


class Installer:
    def __init__(
        self, plan: Plan, runner: Runner, manifest: dict, api_port: int = API_PORT
    ) -> None:
        self.plan = plan
        self.api_port = api_port
        self.runner = runner
        self.manifest = manifest
        root = plan.root
        self.src = root / "src"
        self.venv = root / "supervisor"
        self.tools = root / "tools"
        self.data = root / "data"
        self.logs = root / "logs"
        self.run_dir = root / "run"

    # Steps

    def problems(self) -> list[str]:
        """Return what stops an install on this Mac."""
        problems = []
        if platform.machine() != "arm64":
            problems.append("this needs a Mac with Apple silicon")
        release = tuple(int(x) for x in (platform.mac_ver()[0] or "0").split(".")[:2])
        if release < MIN_MACOS:
            problems.append("this needs macOS %d or later" % MIN_MACOS[0])
        for agents in (
            Path.home() / "Library" / "LaunchAgents",
            Path("/Library/LaunchDaemons"),
        ):
            existing = agents / ("%s.supervisor.plist" % self.plan.prefix)
            if existing.exists():
                problems.append(
                    "%s exists: Home Assistant is already installed with prefix %s"
                    % (existing, self.plan.prefix)
                )
        for port in (self.api_port, CORE_PORT):
            if not port_free(port):
                problems.append("port %d is in use" % port)
        return problems

    def preflight(self) -> None:
        problems = self.problems()
        for problem in problems:
            self.runner.say("problem: " + problem)
        if problems and not self.runner.dry_run:
            raise InstallError("; ".join(problems))
        missing = [] if Path(PYTHON).exists() else ["python@3.14"]
        if not shutil.which("git", path=SYSTEM_PATH):
            missing.append("git")
        if "core_openthread_border_router" in self.plan.apps:
            missing += [f for f in ("cmake", "ninja") if not shutil.which(f, path=SYSTEM_PATH)]
        if missing:
            if not Path(BREW).exists():
                raise InstallError(
                    "Homebrew is needed (https://brew.sh), then: brew install "
                    + " ".join(missing)
                )
            self.runner.run([BREW, "install"] + missing)

    def folders(self) -> None:
        self.runner.say("folders in %s" % self.plan.root)
        if self.runner.dry_run:
            return
        for folder in (self.data, self.logs, self.run_dir):
            folder.mkdir(parents=True, exist_ok=True)
        machine_id = self.plan.root / "machine-id"
        if not machine_id.exists():
            machine_id.write_text(uuid.uuid4().hex + "\n")

    def supervisor_source(self) -> None:
        if (self.src / ".git").exists():
            self.runner.run(["git", "-C", str(self.src), "fetch", "--quiet", "origin"])
            self.runner.run(
                ["git", "-C", str(self.src), "checkout", "--quiet", "origin/" + self.plan.branch]
            )
        else:
            depth = ["--depth", "1"] if "://" in self.plan.source else []
            self.runner.run(
                ["git", "clone", "--quiet", "--branch", self.plan.branch] + depth
                + [self.plan.source, str(self.src)]
            )
        self.runner.run([PYTHON, "-m", "venv", str(self.venv)])
        pip = str(self.venv / "bin" / "pip")
        # The package doesn't declare its dependencies (setup.py passes them
        # under a name setuptools ignores); upstream's Dockerfile installs
        # requirements.txt first, and so does this.
        self.runner.run([pip, "install", "--quiet", "-r", str(self.src / "requirements.txt")])
        self.runner.run([pip, "install", "--quiet", "--no-deps", str(self.src)])
        self.runner.run([str(self.venv / "bin" / "python"), "-c", "import supervisor.native.layout"])

    def flash_tools(self) -> None:
        self.runner.run([PYTHON, "-m", "venv", str(self.tools)])
        self.runner.run(
            [str(self.tools / "bin" / "pip"), "install", "--quiet", FLASHER, ZIGPY_CLI]
        )

    def helper(self) -> None:
        script = self.src / "macos" / "helper" / "install.sh"
        self.runner.say("root helper: sudo %s" % script)
        self.runner.run(["/usr/bin/sudo", str(script)])

    def flash(self, device: str, choice: str) -> None:
        """Probe, back up, flash and probe again one dongle."""
        dongle = self.plan.dongle(device)
        image = fetch_image(self.runner, self.manifest[choice], self.plan.root / "firmware")
        flasher = [str(self.tools / "bin" / "universal-silabs-flasher"), "--device", device]
        probe_methods = ["--probe-methods", CHOICES[choice]["probe"]]
        found = self.runner.run(flasher + probe_methods + ["probe"], check=False, capture=True)
        kind = parse_probe((found.stdout or "") + (found.stderr or ""))
        self.runner.say("%s runs: %s" % (device, kind or "unknown"))
        if kind == "ezsp":
            backup = self.plan.root / "backups" / (
                "zigbee-%s-%s.json" % (dongle.serial or "dongle", time.strftime("%Y%m%d-%H%M%S"))
            )
            if not self.runner.dry_run:
                backup.parent.mkdir(parents=True, exist_ok=True)
            self.runner.run(
                [str(self.tools / "bin" / "zigpy"), "radio", "--baudrate", "115200", "ezsp"]
                + [device, "backup", str(backup)]
            )
            self.runner.say("Zigbee network backup: %s" % backup)
        self.runner.run(
            flasher + probe_methods + ["--bootloader-reset", "rts_dtr", "flash"]
            + ["--firmware", str(image)]
        )
        after = self.runner.run(
            flasher + ["--probe-methods", CHOICES[choice]["probe"], "probe"],
            check=False,
            capture=True,
        )
        kind = parse_probe((after.stdout or "") + (after.stderr or ""))
        if not self.runner.dry_run and kind != CHOICES[choice]["expect"]:
            raise InstallError(
                "%s: after flashing it runs %s, expected %s; the bootloader is untouched, "
                "so flashing again (or the vendor image) recovers it"
                % (device, kind or "nothing that answers", CHOICES[choice]["expect"])
            )

    def environment(self) -> dict[str, str]:
        root = self.plan.root
        return {
            "SUPERVISOR_DATA": str(self.data),
            "SUPERVISOR_SHARE": str(self.data),
            "SUPERVISOR_RUN_DIR": str(self.run_dir),
            "SUPERVISOR_MACHINE_ID_FILE": str(root / "machine-id"),
            "SUPERVISOR_NAME": "%s-supervisor" % self.plan.prefix,
            "SUPERVISOR_MACHINE": "macos-aarch64",
            "SUPERVISOR_API_HOST": "127.0.0.1",
            "SUPERVISOR_API_PORT": str(self.api_port),
            "SUPERVISOR_LAUNCHD_PREFIX": self.plan.prefix,
            "SUPERVISOR_LOG_FILE": str(self.logs / "supervisor.log"),
            "HOME": str(Path.home()),
            "PATH": SYSTEM_PATH,
        }

    def agent_plist(self) -> dict:
        """The Supervisor as a login item (session layout); no secrets in it."""
        log = str(self.logs / "supervisor.log")
        return {
            "Label": "%s.supervisor" % self.plan.prefix,
            "ProgramArguments": [str(self.venv / "bin" / "python"), "-m", "supervisor"],
            "EnvironmentVariables": self.environment(),
            "WorkingDirectory": str(self.plan.root),
            "StandardOutPath": log,
            "StandardErrorPath": log,
            "RunAtLoad": True,
            "KeepAlive": True,
            "ThrottleInterval": 10,
            "ProcessType": "Interactive",
        }

    def start_supervisor(self) -> None:
        path = Path.home() / "Library" / "LaunchAgents" / (
            "%s.supervisor.plist" % self.plan.prefix
        )
        self.runner.say("write %s" % path)
        if not self.runner.dry_run:
            path.parent.mkdir(parents=True, exist_ok=True)
            with path.open("wb") as file:
                plistlib.dump(self.agent_plist(), file)
        self.runner.run(["/bin/launchctl", "bootstrap", "gui/%d" % os.getuid(), str(path)])
        self.wait_http("http://127.0.0.1:%d/supervisor/ping" % self.api_port, 300, "the Supervisor")

    def wait_http(self, url: str, timeout: int, what: str) -> None:
        self.runner.say("waiting for %s (%s)" % (what, url))
        if self.runner.dry_run:
            return
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                with urllib.request.urlopen(url, timeout=5):  # noqa: S310
                    return
            except OSError as err:
                if getattr(err, "code", None) in (200, 401):
                    return
            time.sleep(5)
        raise InstallError("%s did not answer within %d s" % (what, timeout))

    def api(self, method: str, path: str, body: dict | None = None) -> dict:
        """Call the Supervisor API with the CLI token (read from its data, never printed)."""
        self.runner.say("API %s %s" % (method, path))
        if self.runner.dry_run:
            return {}
        token = json.loads((self.data / "cli.json").read_text())["access_token"]
        request = urllib.request.Request(  # noqa: S310
            "http://127.0.0.1:%d%s" % (self.api_port, path),
            method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Authorization": "Bearer " + token, "Content-Type": "application/json"},
        )
        with urllib.request.urlopen(request, timeout=1800) as response:  # noqa: S310
            return json.loads(response.read() or b"{}").get("data") or {}

    def by_id(self, device: str) -> str:
        """Return the Supervisor's stable by-id link for a dongle (apps take that)."""
        if self.runner.dry_run:
            return device
        serial = self.plan.dongle(device).serial
        for item in self.api("GET", "/hardware/info").get("devices", []):
            links = [item.get("dev_path", "")] + list(item.get("by_id") and [item["by_id"]] or [])
            if serial and any(serial in link for link in links if link):
                return item.get("by_id") or item["dev_path"]
        raise InstallError("the Supervisor doesn't list %s" % device)

    def app_options(self, app: str) -> dict:
        radios = {c: d for d, c in self.plan.flashes.items()}
        if app == "core_zigbee_ble_bridge":
            return {"device": self.by_id(radios["zigbee-ble"])}
        if app == "core_openthread_border_router":
            device = radios.get("thread-rcp")
            options = {"backbone_interface": self.plan.backbone}
            if device:
                options["device"] = self.by_id(device)
            return options
        if app == "core_thread_ble_bridge":
            return {"device": self.by_id(radios["thread-ble"])}
        return {}

    def apps(self) -> None:
        self.wait_http("http://127.0.0.1:%d/" % CORE_PORT, 1800, "Home Assistant Core")
        self.api("POST", "/store/reload")
        for app in self.plan.apps:
            self.api("POST", "/store/addons/%s/install" % app)
            options = self.app_options(app)
            if options:
                self.api("POST", "/addons/%s/options" % app, {"options": options})
            self.api("POST", "/addons/%s/start" % app)

    def switch_layout(self) -> None:
        env = dict(os.environ, **self.environment())
        self.runner.run(
            [str(self.venv / "bin" / "python"), "-m", "supervisor.native.layout", "system"]
            + ["--prefix", self.plan.prefix],
            env=env,
        )

    def macscan(self) -> None:
        repo = self.plan.root / "ha-macos-bluetooth"
        if not (repo / ".git").exists():
            self.runner.run(
                ["git", "clone", "--quiet", "--depth", "1"]
                + ["https://github.com/clara-mostly-human/ha-macos-bluetooth", str(repo)]
            )
        self.runner.run(["/bin/bash", str(repo / "macscan" / "install.sh")])

    def steps(self) -> list[tuple[str, str, object]]:
        """Return (name, title, function) for each step, in order."""
        steps: list[tuple[str, str, object]] = [
            ("check", "check the Mac", self.preflight),
            ("folders", "folders", self.folders),
            ("source", "Supervisor source and Python environment", self.supervisor_source),
        ]
        if self.plan.flashes:
            steps.append(("tools", "flash tools", self.flash_tools))
        if self.plan.needs_helper:
            steps.append(("helper", "root helper", self.helper))
        for device, choice in self.plan.flashes.items():
            steps.append(
                ("flash", "flash %s with %s" % (device, CHOICES[choice]["title"]),
                 lambda d=device, c=choice: self.flash(d, c))
            )
        steps.append(("supervisor", "start the Supervisor", self.start_supervisor))
        steps.append(("apps", "apps", self.apps))
        if self.plan.layout == "system":
            steps.append(
                ("layout", "run with nobody logged in (system layout)", self.switch_layout)
            )
            if self.plan.macscan:
                steps.append(
                    ("macscan", "the Mac's radio as an extra source (macscan)", self.macscan)
                )
        return steps

    def install(self, only: set[str] | None = None) -> None:
        for number, (name, title, step) in enumerate(self.steps(), 1):
            if only is not None and name not in only:
                continue
            self.runner.say("== %d. %s" % (number, title))
            step()  # type: ignore[operator]
        if only is not None:
            return
        self.runner.say(
            "Done. Open http://%s.local:%d to set up Home Assistant."
            % (socket.gethostname().split(".")[0], CORE_PORT)
        )


def ask_choice(label: str, titles: list[str]) -> int:
    print("\nWhat should %s run?" % label)
    for number, title in enumerate(titles, 1):
        print("  %d. %s" % (number, title))
    while True:
        answer = input("Choose 1-%d: " % len(titles)).strip()
        if answer.isdigit() and 1 <= int(answer) <= len(titles):
            return int(answer) - 1


def confirm(question: str) -> bool:
    return input("%s [y/N] " % question).strip().lower() in ("y", "yes")


def parse_radio(value: str) -> tuple[str, str]:
    device, sep, choice = value.partition("=")
    if not sep or choice not in list(CHOICES) + [KEEP]:
        raise argparse.ArgumentTypeError(
            "expected DEVICE=CHOICE with CHOICE one of %s" % ", ".join(list(CHOICES) + [KEEP])
        )
    return device, choice


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--dry-run", action="store_true", help="print the plan, change nothing")
    parser.add_argument("--yes", action="store_true", help="don't ask; dongles stay as they are unless --radio says otherwise")
    parser.add_argument("--root", type=Path, default=Path.home() / "HomeAssistant", help="install folder (no spaces)")
    parser.add_argument("--prefix", default=PREFIX, help="launchd prefix (ha-macos or ha-macos-<name>)")
    parser.add_argument("--source", default=FORK_URL, help="Supervisor git URL or local checkout")
    parser.add_argument("--branch", default=FORK_BRANCH)
    parser.add_argument("--radio", action="append", type=parse_radio, default=[], metavar="DEVICE=CHOICE")
    parser.add_argument("--no-macscan", action="store_true", help="don't add the Mac's radio as an extra Bluetooth source")
    parser.add_argument("--api-port", type=int, default=API_PORT, help="Supervisor API port")
    parser.add_argument("--only", help="run only these steps (comma-separated names), for testing")
    args = parser.parse_args(argv)

    if " " in str(args.root):
        print("error: the install folder can't contain spaces (uv splits paths on them)", file=sys.stderr)
        return 2
    try:
        manifest = load_manifest()
        dongles = find_dongles()
        radios = choose_radios(dongles, manifest, ask_choice, args.yes or args.dry_run, dict(args.radio))
        plan = Plan(
            root=args.root.expanduser().resolve(),
            prefix=args.prefix,
            source=args.source,
            branch=args.branch,
            radios=radios,
            dongles=dongles,
            macscan=not args.no_macscan,
            backbone=default_interface(),
        )
        plan.check()
        print("\n".join(["", "Plan:"] + ["  " + line for line in plan.describe()]))
        for dongle in dongles:
            if not dongle.mg24:
                print("  Not handled here (left alone): %s" % dongle.label)
        if plan.layout == "session":
            print("\n" + MAC_BLUETOOTH_WARNING)
        if plan.flashes:
            print(
                "\nFlashing takes the dongle off the air for a minute or two. Its network "
                "settings and bootloader are kept; a Zigbee dongle's network is also "
                "backed up first."
            )
        if not args.dry_run and not args.yes and not confirm("\nGo ahead?"):
            return 1
        runner = Runner(args.dry_run, None if args.dry_run else plan.root / "logs" / "install.log")
        only = set(args.only.split(",")) if args.only else None
        Installer(plan, runner, manifest, args.api_port).install(only)
    except InstallError as err:
        print("error: %s" % err, file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nstopped", file=sys.stderr)
        return 130
    return 0


if __name__ == "__main__":
    sys.exit(main())
