# Home Assistant OS, natively on macOS

This branch runs the Home Assistant Supervisor natively on a Mac, without a
virtual machine or containers. The aim is the Home Assistant OS experience on
Apple hardware: install once, and get onboarding, one-click updates, apps,
backups and a Mac that keeps running Home Assistant like an appliance.

On Linux, Home Assistant OS is an operating system image with the Supervisor
running in a container. Here macOS is the operating system. The Supervisor
runs as a native process, runs Home Assistant Core from a Python virtual
environment, runs apps as launchd services, and has a small root helper where
macOS needs root.

## What works today

Running in production on a Mac mini since 2026-10-18:

- **Core:** one-click updates with automatic rollback, watchdog, safe mode and
  recovery mode. Core patches the install needs are in `core-patches/`.
- **Apps:** native recipes instead of container images. The repository is
  [ha-macos-apps](https://github.com/clara-mostly-human/ha-macos-apps): Matter
  Server and OpenThread Border Router (built from source, running as root).
  Ingress and discovery work, and so do device dropdowns with stable by-id
  serial links.
- **Backups:** full and partial backup and restore, including root-owned app
  data. A restore onto another install matches serial devices by their by-id
  name.
- **Settings pages:**
  - Host: name, macOS version, boot time.
  - Hardware: USB serial devices with stable by-id links.
  - Network: read only.
  - CPU and memory for Core, apps and the Supervisor.
- **Command line:** the `ha` command.
- **Appliance basics:** the Mac doesn't go to sleep while the Supervisor runs.

## What makes it an OS (the plan)

Each step lands in this branch with tests, and production moves to it in a
planned cutover.

1. **An app that installs and boots like an appliance.**
   - **The app:** `Home Assistant.app`, dragged into Applications. It carries
     the Supervisor, its Python runtime and the root helper. On first launch
     it sets everything up and opens onboarding. A menu bar item shows
     whether Home Assistant is running and opens it.
   - **Starting at power-on with nobody logged in:** the app registers its
     system services with macOS (SMAppService), which the user approves once
     under System Settings › Login Items. macOS power settings restart the
     Mac after a power cut.
   - **A fixed data folder:** `/Library/HomeAssistant`, the same on every Mac,
     as `/mnt/data` is on Home Assistant OS. Backups and device paths then
     move between Macs unchanged. The name has no spaces, because uv splits
     paths on them.
2. **The OS agent.** What Home Assistant OS does through systemd, the root
   helper does here:
   - restart and shut down the host;
   - host name and time zone;
   - host logs from the macOS unified log;
   - disk usage;
   - network storage for backups and media (SMB, NFS).
3. **Updates for everything.** The Supervisor updates itself from this fork's
   releases, with rollback. The macOS layer (root helper, launchd services)
   updates with it, as the Operating System entry does on Home Assistant OS.
4. **Matter on matter.js.** The Matter Server app follows the official app to
   the matter.js server.
5. **More apps.** Native recipes for the apps people ask for. The store only
   shows apps that can run natively.

macOS already provides what the DNS, audio and multicast plugins do on Home
Assistant OS, so those stay off.

## Open questions

- **Bluetooth and boot without login.** Core uses Bluetooth through
  CoreBluetooth. A test on macOS 26.5 (2026-11-13) with a system service
  registered through SMAppService:
  - The service ran as root.
  - Its Bluetooth request was refused: macOS's privacy service (TCC)
    refuses Bluetooth to a process "in background session", and
    CoreBluetooth reports `unauthorized`. No prompt is shown.
  - Core running in the logged-in user's session, as it does today, has
    Bluetooth.
  - Still untested:
    - whether a configuration profile can grant a system service Bluetooth;
    - whether the service starts before anyone logs in (needs a reboot).
  - Bluetooth proxies on the network (ESPHome) don't use the Mac's radio,
    so they work either way.
- **Signing.** A self-signed (ad-hoc) app can register a system service. The
  user approves it once under System Settings › General › Login Items &
  Extensions › App Background Activity, and it then runs. For other people to
  open the app without warnings, it still has to be signed with an Apple
  Developer ID and notarized.
- **FileVault.** With FileVault on, a Mac waits for its password after a power
  cut, before any service starts.

## Layout of this branch

- `supervisor/native/`: the macOS backends:
  - Core install and run;
  - apps as launchd services;
  - hardware, network and host information;
  - logs and stats.
- `macos/helper/`: the root helper, installed once with `sudo`.
- `macos/core-patches/`: patches applied to every Core install.
- `tests/native/`: tests for the macOS backends.
