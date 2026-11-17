#!/bin/bash
# Remove the root helper and every service it manages: root apps, and in the
# system layout also the Supervisor, Core and apps that run as the user (this
# stops Home Assistant; switch back first with
# `python -m supervisor.native.layout session` to keep it running).
set -euo pipefail
[ "$(id -u)" = 0 ] || { echo "run with sudo" >&2; exit 1; }
for plist in /Library/LaunchDaemons/ha-macos*.app.*.plist \
             /Library/LaunchDaemons/ha-macos*.core.plist \
             /Library/LaunchDaemons/ha-macos*.supervisor.plist; do
  [ -e "$plist" ] || continue
  label=$(basename "$plist" .plist)
  launchctl bootout "system/$label" 2>/dev/null || true
  rm -f "$plist"
done
launchctl bootout system/ha-macos.helper 2>/dev/null || true
rm -f /Library/LaunchDaemons/ha-macos.helper.plist /var/run/ha-macos-helper.sock
rm -rf /usr/local/ha-macos
echo "ha-macos.helper removed"
