# Core patches for the native macOS install

The native Supervisor applies every `*.patch` in `<data>/core/patches/` to each
Home Assistant Core install (`patch -p1` into the venv's `site-packages`). A
patch that doesn't apply fails the install, so they move with each Core
release. Copy the folder for the Core version you run into `<data>/core/patches/`.

## ha-2026.11.0

- `local-fixes.patch`: `runner.py` drops cached orjson Fragments at exit (the
  interpreter otherwise crashes on exit); `bluetooth` stops its scanner on
  `EVENT_HOMEASSISTANT_STOP`.
- `macos-autorelease.patch`: each CoreBluetooth advertisement gets its own
  autorelease pool, so bleak's callbacks on the event loop thread don't leak.
- `serial-by-id.patch`: macOS has no udev, so the Supervisor keeps
  udev-style serial by-id links in its data folder and passes the folder to
  Core as `SUPERVISOR_SERIAL_BY_ID_DIR`. With it, `usb` names serial ports by
  those links, as serialx does on Linux with `/dev/serial/by-id`, so ZHA keeps
  its radio when the dongle moves to another USB port. A by-id path saved
  elsewhere (another Supervisor data folder, or `/dev/serial/by-id/...` from
  Home Assistant OS) is matched by link name, so a restored ZHA finds its
  radio. Without the variable Core behaves as upstream.
