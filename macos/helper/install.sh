#!/bin/bash
# Install the native Supervisor's root helper (LaunchDaemon ha-macos.helper).
# Run once, from the account the Supervisor runs as:  sudo ./install.sh
# Undo with: sudo ./uninstall.sh
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
USER_UID=${SUDO_UID:?run this with sudo from the Supervisor user\'s account}
HERE=$(cd "$(dirname "$0")" && pwd)
BASE=/usr/local/ha-macos
PLIST=/Library/LaunchDaemons/ha-macos.helper.plist
SOCK=/var/run/ha-macos-helper.sock

install -d -o root -g wheel -m 755 "$BASE"
install -o root -g wheel -m 755 "$HERE/ha_macos_helper.py" "$BASE/ha_macos_helper.py"

cat >"$PLIST" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>ha-macos.helper</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/bin/python3</string>
    <string>$BASE/ha_macos_helper.py</string>
    <string>--uid</string>
    <string>$USER_UID</string>
    <string>--socket</string>
    <string>$SOCK</string>
  </array>
  <key>RunAtLoad</key><true/>
  <key>KeepAlive</key><true/>
  <key>StandardOutPath</key><string>/var/log/ha-macos-helper.log</string>
  <key>StandardErrorPath</key><string>/var/log/ha-macos-helper.log</string>
</dict>
</plist>
PLIST
chown root:wheel "$PLIST"; chmod 644 "$PLIST"

launchctl bootout system/ha-macos.helper 2>/dev/null || true
# bootout returns before launchd has finished removing the job; loading it
# again before then fails with "Bootstrap failed: 5: Input/output error".
for _ in $(seq 40); do
  launchctl print system/ha-macos.helper >/dev/null 2>&1 || break
  sleep 0.25
done
# The old helper's socket file stays behind: remove it, or the wait below
# would see it and report success for a helper that never started.
rm -f "$SOCK"
launchctl bootstrap system "$PLIST"
for _ in $(seq 20); do [ -S "$SOCK" ] && break; sleep 0.5; done
[ -S "$SOCK" ] || { echo "helper did not start; see /var/log/ha-macos-helper.log" >&2; exit 1; }
ls -l "$SOCK"
echo "ha-macos.helper installed for uid $USER_UID"
